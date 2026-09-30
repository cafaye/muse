"""Boot: settings, the production container, and the refusal to start.

The production wiring cannot be exercised against a real postgres and a real provider
in a suite that may not open a socket, so what is tested here is everything *around*
it: the settings a developer machine and a container each get, the order boot happens
in, and — most of it — the failures that must stop the process rather than surface on
someone's first request.

The two rules under test are both "refuse to boot":

- **No vault key, no start.** A vault that starts with a default key is a vault whose
  keys are readable by anyone who can read the source.
- **No valid routes file, no start.** A container that starts and then 503s on its
  first request is a deploy that looks healthy and is not.

`build_container` is async because it opens the connection pool, and it is the only
function in the service that dials. Everything it does before the dial is testable
without one, which is most of it.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from muse.errors import RouteConfigError, VaultKeyError
from muse.main import (
    DEFAULT_ROUTES,
    VENDORS,
    Settings,
    _UnavailableDatabase,
    build_container,
    create_app,
)
from muse.providers import LiteLLMProvider, ProviderRegistry
from muse.routes import routes_from_yaml

from .conftest import write_routes
from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

KEY = base64.b64encode(bytes(range(32))).decode()


def env(**overrides: str) -> dict[str, str]:
    return {"MUSE_VAULT_KEY": KEY, **overrides}


def routes_file(tmp_path: Path, document: dict | None = None) -> Path:
    path = tmp_path / "routes.yaml"
    path.write_text(
        write_routes(
            document
            or {
                "version": 1,
                "routes": [{"model": "fast", "candidates": [{"provider": "openai"}]}],
            }
        ),
        encoding="utf-8",
    )
    return path


# --- settings --------------------------------------------------------------


def test_settings_default_to_a_developer_machine() -> None:
    """Every field has a default that works with no configuration, so a new
    contributor can run the probes without reading the README first."""
    settings = Settings()
    assert settings.env == "development"
    assert settings.database_url is None
    assert settings.routes_path == DEFAULT_ROUTES


def test_settings_read_the_environment() -> None:
    settings = Settings.from_env(
        env(MUSE_ENV="production", MUSE_DATABASE_URL="postgres://localhost/muse")
    )
    assert settings.env == "production"
    assert settings.database_url == "postgres://localhost/muse"


def test_an_unset_database_url_is_none_rather_than_empty() -> None:
    """`None` and `""` behave the same in a boolean check but not in a log line, and
    `MUSE_DATABASE_URL=` in a compose file is a real mistake worth seeing."""
    assert Settings.from_env(env(MUSE_DATABASE_URL="")).database_url is None


def test_a_routes_file_path_can_be_overridden() -> None:
    """So a deploy can mount its own routing table without editing the image."""
    settings = Settings.from_env(env(MUSE_ROUTES_FILE="/etc/muse/routes.yaml"))
    assert settings.routes_path == Path("/etc/muse/routes.yaml")


def test_settings_do_not_read_the_vault_key() -> None:
    """The key is read by `build_container`, not by `Settings`. Reading it here would
    make constructing a settings object a boot failure, which is a much larger blast
    radius than the one call site that needs it."""
    assert not hasattr(Settings(), "vault_key")


def test_settings_from_env_works_without_a_vault_key() -> None:
    assert Settings.from_env({}).env == "development"


# --- the committed routes file ---------------------------------------------


def test_the_committed_routes_file_is_where_the_default_says() -> None:
    """A default pointing at a file that is not in the repository is a default that
    fails on a fresh clone, which is the first thing a reviewer does."""
    assert DEFAULT_ROUTES.is_file()
    assert DEFAULT_ROUTES.name == "routes.yaml"


def test_the_committed_routes_file_validates_against_the_vendors_this_build_ships() -> None:
    """The two halves of the boot check, as one test: the file names providers, and
    the build ships adapters for exactly those. A file naming a fourth vendor fails
    here rather than at the first request."""
    table = routes_from_yaml(DEFAULT_ROUTES.read_text(encoding="utf-8"))
    registry = ProviderRegistry()
    for vendor in VENDORS:
        registry.register(LiteLLMProvider(name=vendor, api_key="sk-test"))
    table.validate(registry)


def test_the_build_ships_adapters_for_exactly_two_vendors() -> None:
    """Pinned so a vendor added here without a matching entry in the committed
    routes file — or a route added naming a vendor with no adapter — is a test
    failure rather than a boot failure in production."""
    assert VENDORS == ("openai", "anthropic")


# --- boot refuses to continue ----------------------------------------------


async def test_boot_without_a_vault_key_fails(tmp_path: Path, monkeypatch) -> None:
    """The headline rule. A vault that starts with a default key is a vault whose keys
    are readable by anyone who has read this repository."""
    monkeypatch.delenv("MUSE_VAULT_KEY", raising=False)
    with pytest.raises(VaultKeyError, match="MUSE_VAULT_KEY"):
        await build_container(Settings(routes_path=routes_file(tmp_path)))


async def test_boot_with_an_unreadable_routes_file_fails(tmp_path: Path, monkeypatch) -> None:
    """A missing routing file is a mount that did not happen. Found at boot it names
    itself; found on the first request it is a 503 that looks like an outage."""
    monkeypatch.setenv("MUSE_VAULT_KEY", KEY)
    with pytest.raises(FileNotFoundError):
        await build_container(Settings(routes_path=tmp_path / "absent.yaml"))


async def test_boot_with_an_invalid_routes_file_fails(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("MUSE_VAULT_KEY", KEY)
    path = tmp_path / "routes.yaml"
    path.write_text(write_routes({"version": 1, "routes": []}), encoding="utf-8")
    with pytest.raises(RouteConfigError):
        await build_container(Settings(routes_path=path))


async def test_a_route_naming_an_unknown_vendor_fails_at_boot(tmp_path: Path, monkeypatch) -> None:
    """The check the loader cannot do, and the reason it runs at boot: a route naming
    a vendor muse has no adapter for is a container that refuses to start rather than
    a 503 on a customer's first request."""
    monkeypatch.setenv("MUSE_VAULT_KEY", KEY)
    path = routes_file(
        tmp_path,
        {"version": 1, "routes": [{"model": "fast", "candidates": [{"provider": "bedrock"}]}]},
    )
    with pytest.raises(RouteConfigError, match="bedrock"):
        await build_container(Settings(routes_path=path))


async def test_the_vault_key_is_read_before_the_database_is_touched(
    tmp_path: Path, monkeypatch
) -> None:
    """Order is the point. With no key and no database, the failure must be about the
    key — a service that reported "no database" first would send an operator to fix
    the wrong thing."""
    monkeypatch.delenv("MUSE_VAULT_KEY", raising=False)
    with pytest.raises(VaultKeyError):
        await build_container(Settings(routes_path=routes_file(tmp_path), database_url=None))


# --- the no-database stand-in ----------------------------------------------


async def test_the_stand_in_database_refuses_to_execute() -> None:
    """A developer running the probes should not have to stand up postgres first.
    Refusing loudly is the point: a service that quietly had no database would answer
    `/readyz: ok` and then 500 on its first real request."""
    database = _UnavailableDatabase()
    with pytest.raises(RuntimeError, match="MUSE_DATABASE_URL"):
        await database.execute("insert into outbox_events (id) values ('x')")


async def test_the_stand_in_database_refuses_to_read() -> None:
    """Including the readiness probe's own `select 1`, which is why `/readyz` reports
    `error` rather than a false `ok` when no database is configured."""
    with pytest.raises(RuntimeError, match="select"):
        await _UnavailableDatabase().fetchone("select 1 as ok")


def test_the_stand_in_database_refuses_to_transact() -> None:
    with pytest.raises(RuntimeError, match="nothing to transact"):
        _UnavailableDatabase().transaction()


async def test_the_stand_in_database_names_the_statement_it_refused() -> None:
    """The verb is in the message, so "it says MUSE_DATABASE_URL" points at the
    configuration rather than at whichever query happened to run first."""
    with pytest.raises(RuntimeError, match="insert"):
        await _UnavailableDatabase().execute("insert into outbox_events (id) values (1)")


# --- the factory ------------------------------------------------------------


def test_the_factory_returns_a_new_app_each_call() -> None:
    assert create_app() is not create_app()


def test_the_factory_accepts_a_container_without_reading_the_environment() -> None:
    """A supplied container means nothing is read, so a test never has to set an
    environment variable that another test is also setting."""
    from .support.test_app import build_test_app

    app = build_test_app(registry=ProviderRegistry(), database=FakeDatabase(), table=_empty_table())
    assert app.state.container is not None


def _empty_table():
    from muse.routes import RetryPolicy, RouteTable

    return RouteTable(version=1, defaults=RetryPolicy(), routes=())


def test_the_app_is_titled_and_versioned() -> None:
    app = create_app()
    assert app.title == "muse"
    assert app.version == "0.2.0"


def test_the_openapi_document_the_app_generates_declares_the_endpoint() -> None:
    """FastAPI generates a document from the code; `openapi/v1.yaml` is the committed
    contract. The generated one has to contain the path, or the committed file is
    describing an endpoint that does not exist."""
    paths = create_app().openapi()["paths"]
    assert "/v1/route" in paths
    assert "post" in paths["/v1/route"]
