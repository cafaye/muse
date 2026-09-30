"""The composition root: settings, the container, and the app factory.

`create_app()` is a factory and never a module-level singleton (AGENTS.md rule 1). It
takes two optional arguments and each exists for a reason:

- `settings` — so a test can point the service at a different routes file or a
  different database without touching the process environment, which is shared state
  between tests and the reason order-dependent suites exist.
- `container` — so a test can hand the app a registry of fakes. The alternative is a
  test that constructs a real pool and a real vault to assert that a JSON body comes
  back, which is slower, needs a server, and tests the plumbing instead of the
  handler.

Boot order matters and is worth stating: the vault key is read first, because a service
that cannot decrypt its credentials has nothing useful to serve, and failing at
construction is louder than failing on the first request. The routes file is read
second and validated against the registry third, so a route naming a provider muse has
no adapter for is a container that will not start rather than a 503 on a customer's
first request.
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from muse.api import TRACE_HEADER, build_router, new_trace_id, register_error_handlers
from muse.db import Database, PsycopgDatabase
from muse.metering import Meter
from muse.providers import LiteLLMProvider, ProviderRegistry
from muse.providers.credentials import CredentialResolver, VaultCredentials
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RouteTable, routes_from_yaml
from muse.vault import Vault, load_vault_key

APP_TITLE = "muse"
APP_VERSION = "0.2.0"
APP_DESCRIPTION = "LLM routing, credentials vault, and token metering for cafaye."

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROUTES = REPO_ROOT / "config" / "routes.yaml"

#: The vendors this build ships adapters for. Named in one place because the routes
#: file is validated against the registry at boot, and a file naming a fourth vendor
#: must fail there rather than at the first request.
VENDORS = ("openai", "anthropic")

#: Overall verdict reported by ``/readyz``.
ReadinessStatus = Literal["ok", "degraded", "unavailable"]
#: Per-dependency verdict reported inside ``/readyz``'s ``checks`` map.
#: ``skipped`` is v0's sentinel for "no database configured"; the Phase 4 vault fills
#: the slot in rather than reshaping the response.
CheckState = Literal["ok", "skipped", "error"]


class Health(BaseModel):
    """Liveness payload. Exactly one field: probes must stay trivially stable."""

    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    db: CheckState = Field(description="The vault and outbox store.")


class Readiness(BaseModel):
    status: ReadinessStatus
    checks: ReadinessChecks


@dataclass(frozen=True, slots=True)
class Settings:
    """What the service is configured with. Every field has a default a developer
    machine works with, and every field is overridable by one environment variable."""

    env: str = "development"
    database_url: str | None = None
    routes_path: Path = DEFAULT_ROUTES
    pool_min_size: int = 1
    pool_max_size: int = 8

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        url = source.get("MUSE_DATABASE_URL") or None
        return cls(
            env=source.get("MUSE_ENV", "development"),
            database_url=url,
            routes_path=Path(source.get("MUSE_ROUTES_FILE", str(DEFAULT_ROUTES))),
        )


@dataclass(frozen=True, slots=True)
class Container:
    """Everything the request path needs, assembled once per app.

    Frozen: it is built at boot and read by every concurrent request. A mutable
    container is a config change that half the fleet sees and half does not.

    `scrub_secrets` is what the endpoint hands the router to remove from provider
    text. It is empty in production, and that is not an oversight: each
    `LiteLLMProvider` holds its own credential and scrubs its own errors, so the router
    has nothing left to do. It exists for a provider that does *not* hold its own key —
    a test double, or a future in-house model behind someone else's key — and it is a
    field rather than a per-request vault read on purpose: scrubbing must never be the
    reason a request touches the vault.
    """

    settings: Settings
    database: Database
    registry: ProviderRegistry
    routes: RouteTable
    router: Router
    vault: Vault
    meter: Meter
    credentials: CredentialResolver
    scrub_secrets: tuple[Secret, ...] = ()

    def credentials_held(self) -> list[Secret]:
        """The plaintext of every configured credential, for scrubbing only.

        A list rather than a tuple because the router takes a sequence it may extend,
        and copying a zero- or one-element tuple on every request is the cheaper
        mistake.
        """
        return list(self.scrub_secrets)


async def build_container(settings: Settings) -> Container:
    """Assemble the production container.

    The vault key is read first and a missing one stops the boot. Everything else
    here is a failure an operator can see in a log at startup rather than discover
    through a customer's 503.
    """
    key = load_vault_key()
    routes = routes_from_yaml(settings.routes_path.read_text(encoding="utf-8"))
    database: Database = (
        await PsycopgDatabase.open(
            settings.database_url,
            min_size=settings.pool_min_size,
            max_size=settings.pool_max_size,
        )
        if settings.database_url
        else _UnavailableDatabase()
    )
    vault = Vault(database, key)
    # One resolver, shared by the adapters and by `Container.credentials`. Building the
    # adapters against a *different* vault — even one that is otherwise equivalent — is
    # how a container ends up validating its routes file against one registry and
    # serving requests through another, and the only symptom is a 503 on every call
    # from a service that booted cleanly.
    credentials = VaultCredentials(vault)
    registry = ProviderRegistry()
    for vendor in VENDORS:
        registry.register(LiteLLMProvider(name=vendor, credentials=credentials))
    routes.validate(registry)
    return Container(
        settings=settings,
        database=database,
        registry=registry,
        routes=routes,
        router=Router(registry, routes),
        vault=vault,
        meter=Meter(database),
        credentials=credentials,
    )


class _UnavailableDatabase:
    """Stands in when no `MUSE_DATABASE_URL` is configured.

    Every call raises, and `/readyz` reports `db: error` because of it. A developer
    running the service to poke at the probes should not have to stand up postgres
    first, and a container that silently had no database would be a service that
    answers `/readyz: ok` and then 500s on its first real request.

    Takes no arguments. It has nothing to configure — the absence of a database *is*
    its configuration — and an argument it never reads would be a lie about its shape.
    """

    async def execute(self, sql: str, params: object = ()) -> None:
        raise RuntimeError(f"no MUSE_DATABASE_URL is configured, so `{sql.split()[0]}` cannot run")

    async def fetchone(self, sql: str, params: object = ()) -> object:
        raise RuntimeError(f"no MUSE_DATABASE_URL is configured, so `{sql.split()[0]}` cannot run")

    def transaction(self):  # pragma: no cover - the readiness probe fails first
        raise RuntimeError("no MUSE_DATABASE_URL is configured, so there is nothing to transact")


def create_app(settings: Settings | None = None, container: Container | None = None) -> FastAPI:
    """Build a muse application.

    Returns a new instance on every call — callers own it, nothing is shared. Pass
    `container` to supply the collaborators; pass `settings` to have them built during
    the lifespan. With neither, the service reads its configuration from the
    environment and refuses to start without a vault key.
    """
    resolved = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        if getattr(app.state, "container", None) is None:
            app.state.container = await build_container(resolved)
        yield

    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        lifespan=lifespan,
    )
    if container is not None:
        # Set here as well as in the lifespan. A caller that supplies a container has
        # nothing to build at startup, and making the app depend on a lifespan event
        # before it is usable would mean the test path and the production path reach
        # the same state by different routes.
        app.state.container = container

    @app.middleware("http")
    async def trace(request: Request, call_next):
        """Stamp a trace id on every request, and echo it on every response.

        Middleware rather than a dependency so the probe routes get one too: a 401 is
        the response an operator is most likely to be looking at, and a trace id only
        on success is a service you cannot debug at 3am.
        """
        request.state.trace_id = new_trace_id(request.headers.get(TRACE_HEADER))
        response = await call_next(request)
        response.headers[TRACE_HEADER] = request.state.trace_id
        return response

    @app.get("/healthz", response_model=Health, tags=["ops"], summary="Liveness probe")
    async def healthz() -> Health:
        """Liveness. Checks nothing on purpose — a dependency here becomes a restart loop."""
        return Health(status="ok")

    @app.get("/readyz", response_model=Readiness, tags=["ops"], summary="Readiness probe")
    async def readyz(request: Request) -> Readiness:
        """Readiness. The place where dependency checks belong.

        A database that cannot be reached makes the service `degraded`, not
        `unavailable`: a route whose provider needs no database read can still be
        served, and telling an orchestrator the service is down would restart
        containers that could have answered.
        """
        state = getattr(request.app.state, "container", None)
        if state is None:
            return Readiness(status="degraded", checks=ReadinessChecks(db="error"))
        try:
            await state.database.fetchone("select 1 as ok")
        except Exception:
            return Readiness(status="degraded", checks=ReadinessChecks(db="error"))
        return Readiness(status="ok", checks=ReadinessChecks(db="ok"))

    app.include_router(build_router())
    register_error_handlers(app)
    return app
