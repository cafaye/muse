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
import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, Request
from opentelemetry import context as context_api
from opentelemetry.trace import Span
from pydantic import BaseModel, Field
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from muse.api import TRACE_HEADER, build_router, new_trace_id, register_error_handlers
from muse.auth import TokenVerifier, jwks_url_for
from muse.breaker import (
    DEFAULT_BREAKER_RESET_SECONDS,
    DEFAULT_BREAKER_THRESHOLD,
    BreakerRegistry,
)
from muse.db import Database, PsycopgDatabase
from muse.jwks import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    DEFAULT_TTL_SECONDS,
    JwksClient,
)
from muse.metering import Meter
from muse.providers import LiteLLMProvider, ProviderRegistry
from muse.providers.credentials import CredentialResolver, VaultCredentials
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RouteTable, routes_from_yaml
from muse.telemetry import (
    TRACEPARENT_HEADER,
    Telemetry,
    build_provider,
    parent_context,
    parse_traceparent,
    record,
)
from muse.vault import Vault, load_vault_key

APP_TITLE = "muse"
APP_VERSION = "0.3.0"
APP_DESCRIPTION = "LLM routing, credentials vault, and token metering for cafaye."

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DEFAULT_ROUTES = REPO_ROOT / "config" / "routes.yaml"

#: identity, as core's conventions document it: the only issuer, and the key set at
#: `{issuer}/.well-known/jwks.json`. `MUSE_IDENTITY_ISSUER` overrides it.
DEFAULT_IDENTITY_ISSUER = "https://identity.cafaye.com"

#: The `aud` a token must carry to be for this service — muse's own client id. Guard
#: calls it "guard's own client id" for the same reason: a resource server's audience
#: is its own name, and anything wider would accept a token minted for a sibling.
DEFAULT_IDENTITY_AUDIENCE = "muse"

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
    #: Where spans are exported, or `None` for "create them and export nowhere".
    #: Never defaulted to a collector: a service that ships pointed at somebody's
    #: telemetry backend is a service that phones home, and one that *fails* when the
    #: collector is absent is a service whose observability is now its availability.
    otel_endpoint: str | None = None
    otel_service_name: str = "muse"
    #: Consecutive transient failures before a provider is held back. See
    #: `muse.breaker` for why the default is five and not one.
    breaker_threshold: int = DEFAULT_BREAKER_THRESHOLD
    breaker_reset_seconds: float = DEFAULT_BREAKER_RESET_SECONDS
    #: identity, and what it means to verify one of its tokens. core's documented
    #: defaults (`docs/openapi-conventions.md:128`): identity is the only issuer, and
    #: the key set lives at `{issuer}/.well-known/jwks.json`.
    #:
    #: These *default* rather than being required, which is the opposite of
    #: `MUSE_VAULT_KEY` (rule 10) and deliberately so. A wrong issuer or audience
    #: cannot make a bad token good: verification still needs a signature from the keys
    #: at that URL, and a token addressed to another service fails the `aud` check. The
    #: worst a wrong default can do is refuse every token — fail closed, loudly — where a
    #: vault key's wrong default decrypts everybody's credentials. Rule 18's rule holds:
    #: a setting whose fallback is safe falls back.
    identity_issuer: str = DEFAULT_IDENTITY_ISSUER
    identity_audience: str = DEFAULT_IDENTITY_AUDIENCE
    #: An explicit key-set URL, for an operator pointing at a mirror. `None` means the
    #: issuer's own well-known path, which is what identity serves and what core says.
    jwks_url: str | None = None
    #: How long a fetched key set is reused, and the floor between two forced refreshes
    #: on an unknown `kid`. The second is the amplification control (see `muse.jwks`).
    jwks_ttl_seconds: float = DEFAULT_TTL_SECONDS
    jwks_refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> Settings:
        source = os.environ if environ is None else environ
        url = source.get("MUSE_DATABASE_URL") or None
        return cls(
            env=source.get("MUSE_ENV", "development"),
            database_url=url,
            routes_path=Path(source.get("MUSE_ROUTES_FILE", str(DEFAULT_ROUTES))),
            otel_endpoint=source.get("MUSE_OTEL_EXPORTER_OTLP_ENDPOINT") or None,
            otel_service_name=source.get("OTEL_SERVICE_NAME", "muse"),
            breaker_threshold=_positive_int(
                source, "MUSE_BREAKER_THRESHOLD", DEFAULT_BREAKER_THRESHOLD
            ),
            breaker_reset_seconds=_positive_float(
                source, "MUSE_BREAKER_RESET_SECONDS", DEFAULT_BREAKER_RESET_SECONDS
            ),
            identity_issuer=source.get("MUSE_IDENTITY_ISSUER") or DEFAULT_IDENTITY_ISSUER,
            identity_audience=source.get("MUSE_IDENTITY_AUDIENCE") or DEFAULT_IDENTITY_AUDIENCE,
            jwks_url=source.get("MUSE_JWKS_URL") or None,
            jwks_ttl_seconds=_positive_float(source, "MUSE_JWKS_TTL_SECONDS", DEFAULT_TTL_SECONDS),
            jwks_refresh_interval=_positive_float(
                source, "MUSE_JWKS_REFRESH_SECONDS", DEFAULT_REFRESH_INTERVAL_SECONDS
            ),
        )


def _positive_int(source: Mapping[str, str], name: str, fallback: int) -> int:
    """An integer setting, falling back on anything unreadable.

    Falling back rather than raising is the decision the packet asks for: missing or
    malformed configuration must never be the reason the service does not start. A
    resilience knob set to nonsense degrades to the documented default, and the
    default is safe — which is the opposite of what a boot failure would give.
    """
    try:
        value = int(source[name])
    except KeyError, ValueError:
        return fallback
    return value if value >= 1 else fallback


def _positive_float(source: Mapping[str, str], name: str, fallback: float) -> float:
    try:
        value = float(source[name])
    except KeyError, ValueError:
        return fallback
    return value if value > 0 else fallback


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
    #: The token verifier, and with it the key-set cache. Required rather than optional:
    #: a container that could be built without one is a container whose `/v1/route`
    #: either skips verification or invents a permissive default, and both are the bug
    #: this packet exists to remove. Every test that builds a container supplies one, so
    #: there is no path to a served app that does not verify.
    auth: TokenVerifier
    scrub_secrets: tuple[Secret, ...] = ()
    #: The tracer, shared with `router` so the provider spans nest under the request
    #: span. Defaults to a no-op rather than being required, because a container
    #: assembled by a test should not have to know that tracing exists to be valid.
    telemetry: Telemetry = field(default_factory=Telemetry.noop)

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
    auth = _build_auth(settings)
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
    telemetry = Telemetry(
        build_provider(endpoint=settings.otel_endpoint, service_name=settings.otel_service_name)
    )
    return Container(
        settings=settings,
        database=database,
        registry=registry,
        routes=routes,
        router=Router(
            registry,
            routes,
            telemetry=telemetry,
            breakers=BreakerRegistry(
                threshold=settings.breaker_threshold,
                reset_seconds=settings.breaker_reset_seconds,
            ),
        ),
        vault=vault,
        meter=Meter(database),
        credentials=credentials,
        auth=auth,
        telemetry=telemetry,
    )


def _build_auth(settings: Settings) -> TokenVerifier:
    """The production verifier: identity's JWKS, cached, fetched over HTTP.

    One client for the process, which is one cache — two verifiers would each fetch the
    key set and could disagree about whether a rotation has happened, which is exactly
    the failure a single source of truth for "which keys are live" exists to prevent.
    """
    return TokenVerifier(
        issuer=settings.identity_issuer,
        audience=settings.identity_audience,
        jwks=JwksClient(
            settings.jwks_url or jwks_url_for(settings.identity_issuer),
            http_fetch_keys,
            ttl_seconds=settings.jwks_ttl_seconds,
            refresh_interval=settings.jwks_refresh_interval,
            clock=time.monotonic,
        ),
        clock=time.time,
    )


#: How long one fetch of the key set may take. Five seconds, and the same default `guard`
#: uses: long enough that a slow identity is not a failed one, short enough that a hung
#: endpoint does not hold a request open past the point where the caller has given up.
JWKS_TIMEOUT_SECONDS = 5.0


async def http_fetch_keys(url: str) -> dict:
    """Fetch the key set over HTTP.

    Raises on a non-2xx rather than returning the body, so `JwksClient` cannot be handed
    an error page it would try to parse as a key set. The response body is never logged
    or returned: identity's answer is third-party text, and the only thing this needs to
    carry upward is *that* it failed.
    """
    import httpx

    async with httpx.AsyncClient(timeout=JWKS_TIMEOUT_SECONDS) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


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


class TraceMiddleware:
    """Continue the caller's trace, stamp a `traceparent`, and open the request span.

    A **pure ASGI** middleware rather than `@app.middleware("http")`, and that is not
    a style preference. The decorator's implementation runs the downstream app in a
    *separate task*, so a context variable set in the middleware is not visible to
    the route handler — which would mean the provider spans were never children of the
    request span, the router would see no trace at all, and `outbound_traceparent()`
    would return `None` on every call. The routing decision is the whole reason this
    packet exists, so the middleware has to run in the same task as the handler.

    Three behaviours, in order:

    1. **An inbound `traceparent` is continued.** A well-formed one becomes the
       parent context, so muse joins the caller's trace rather than starting a
       parallel one with a number that looks similar.
    2. **A malformed or absent one starts a new trace.** Never a 400 and never an
       exception: `traceparent` is an observability affordance, and an affordance
       that can take a customer's request down is a denial-of-service vector aimed at
       our own probe. The only thing a broken header may cost is correlation.
    3. **The response carries both correlation headers.** `traceparent`, so the
       caller can join the trace it started; and `X-Trace-Id`, which packet `muse-02`
       published in the committed OpenAPI document and the generated SDKs. Two ids on
       one response is a deliberate cost: the old one is a published contract and
       removing it is breaking.

    The trace id on `scope["state"]` is the legacy opaque id, untouched by all of
    this. `traceparent` trace ids are 32 hex characters and `X-Trace-Id` ids are
    `uuid4().hex`, so they are the same length and the same alphabet by coincidence
    — but only the second is the one in the `problem+json` body, which is a
    published contract.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":  # pragma: no cover - lifespan/websocket reach here
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        legacy = new_trace_id(headers.get(TRACE_HEADER))
        incoming = parse_traceparent(headers.get(TRACEPARENT_HEADER))

        # The legacy opaque id the `problem+json` body and the `X-Trace-Id` header
        # carry. Untouched by the tracing work: it is a published contract.
        scope.setdefault("state", {})["trace_id"] = legacy

        telemetry = getattr(scope["app"].state, "telemetry", None) or Telemetry.noop()
        # Parent attached *before* the span opens, so the span is a child of the
        # caller's rather than a sibling. Attaching afterwards would produce a
        # correctly-named span with the wrong parent, which is worse than no span: it
        # looks right in a trace viewer.
        parent = parent_context(incoming) if incoming else context_api.get_current()
        with context_api.attach(parent), telemetry.span("muse.request") as span:
            context = span.get_span_context()
            record(
                span,
                {
                    "http.request.method": scope.get("method", ""),
                    "url.path": scope.get("path", ""),
                    "muse_trace_id": f"{context.trace_id:032x}",
                    "muse_parent_span_id": incoming.span_id if incoming else "",
                },
            )
            await self.app(
                scope,
                receive,
                _correlating(send, _span_traceparent(context), legacy, span),
            )


def _span_traceparent(context: object) -> str | None:
    """The `traceparent` naming this request's own span, or `None` if nothing is tracing.

    Built from the span rather than from the inbound header so the caller gets a
    header they can *continue* — the same trace id at a child span. Echoing the
    inbound header verbatim would name a span that belongs to the caller, and a
    caller that started a second request from the first would correlate the two.

    `None` for an invalid context, which is the no-op tracer's. Formatting an invalid
    context would produce an all-zero traceparent — the exact value W3C reserves and
    `muse.telemetry.parse_traceparent` refuses on the way in, so the service would be
    emitting a header it would itself reject.
    """
    if not context.is_valid:
        return None
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-01"


def _correlating(send: Send, traceparent: str | None, legacy: str, span: Span) -> Send:
    """Wrap `send` to add the correlation headers and the status to the request span.

    Wrapping rather than mutating afterwards because at ASGI level the response
    headers are one `http.response.start` message that has already been written by
    the time the body arrives — there is no "after" to mutate.

    The status is recorded here, on the request span, because that is the span that
    means "a request arrived". A 503 on it is the first thing an operator filters
    on; a status on the provider span would be a category error, since a provider
    call has no HTTP response of its own.
    """
    done = False

    async def send_with_headers(message: Message) -> None:
        nonlocal done
        if message["type"] == "http.response.start" and not done:
            done = True
            headers = MutableHeaders(scope=message)
            if traceparent is not None:
                headers[TRACEPARENT_HEADER] = traceparent
            headers[TRACE_HEADER] = legacy
            record(span, {"http.response.status_code": message.get("status", 0)})
        await send(message)

    return send_with_headers


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
        # Replaced rather than set once, because the container built above is where
        # the real tracer lives. Reading it from the container is what guarantees the
        # request span and the router's spans come from the same tracer — two
        # tracers would produce two disjoint traces under one trace id.
        app.state.telemetry = app.state.container.telemetry
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
    # Always present, so the middleware never has to decide what a missing tracer
    # means. Replaced by the lifespan with the container's real one.
    app.state.telemetry = container.telemetry if container is not None else Telemetry.noop()

    # Outermost, so the probe routes are traced too: a 401 is the response an operator
    # is most likely to be looking at, and a trace id only on success is a service you
    # cannot debug at 3am.
    app.add_middleware(TraceMiddleware)

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
