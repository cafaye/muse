"""GET /readyz — readiness.

Contract: 200 with a top-level `status` and a `checks` map whose `db` entry reports
the vault and outbox store. The `skipped` sentinel this reserved for the Phase 4 vault
is gone: this packet *is* the vault packet, so a container with a database reports
`ok` and one whose database is unreachable reports `error`. The key and the shape are
unchanged, which is what "later packets fill the value in, they do not reshape the
response" meant.

`degraded` rather than `unavailable` for a database failure: a route whose provider
needs no database read can still be served, and telling an orchestrator the service is
down restarts containers that could have answered.
"""

from __future__ import annotations

import pytest

from muse.main import Container, Settings, create_app
from muse.providers import ProviderRegistry
from muse.routes import RouteTable
from muse.vault import Vault

from .conftest import asgi_client
from .support.fake_database import FakeDatabase
from .support.test_app import TEST_KEY

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


def container_for(database: FakeDatabase) -> Container:
    """A container with an empty registry and no routes.

    Readiness does not route anything, so a registry with no providers and a table with
    no routes is the honest minimum: the point of these tests is the `db` slot, and a
    fixture that registered a provider would be asserting about something else.
    """
    from muse.metering import Meter
    from muse.providers.credentials import StaticCredentials
    from muse.router import Router
    from muse.routes import RetryPolicy

    registry = ProviderRegistry()
    table = RouteTable(version=1, defaults=RetryPolicy(), routes=())
    return Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table),
        vault=Vault(database, TEST_KEY),
        meter=Meter(database),
        credentials=StaticCredentials({}),
    )


async def test_readyz_returns_200(client) -> None:
    """A 200 even when a dependency is failing. Readiness answers "should traffic
    come here", and a non-200 would be read by some orchestrators as "not listening"."""
    response = await client.get("/readyz")
    assert response.status_code == 200


async def test_readyz_body_has_the_reserved_shape(client) -> None:
    """Exact equality. The `db` key was reserved for this packet; a second key would be
    a shape change that every dashboard and contract test would have to absorb."""
    body = (await client.get("/readyz")).json()
    assert set(body) == {"status", "checks"}
    assert set(body["checks"]) == {"db"}
    assert body["status"] in {"ok", "degraded", "unavailable"}
    assert body["checks"]["db"] in {"ok", "skipped", "error"}


async def test_a_reachable_database_reports_ok() -> None:
    database = FakeDatabase()
    async with asgi_client(create_app(container=container_for(database))) as client:
        body = (await client.get("/readyz")).json()
    assert body == {"status": "ok", "checks": {"db": "ok"}}


async def test_an_unreachable_database_reports_degraded() -> None:
    """`degraded`, not `unavailable`: the service is up and a route whose provider
    needs no database read can still be served. The distinction is what an
    orchestrator acts on."""
    database = FakeDatabase()
    database.fail_next = RuntimeError("connection refused")
    async with asgi_client(create_app(container=container_for(database))) as client:
        body = (await client.get("/readyz")).json()
    assert body == {"status": "degraded", "checks": {"db": "error"}}


async def test_a_missing_container_reports_degraded() -> None:
    """An app whose lifespan has not run has no container. Reporting `degraded` is
    honest; reporting `ok` would be a service claiming to be ready with nothing
    behind it."""
    async with asgi_client(create_app()) as client:
        body = (await client.get("/readyz")).json()
    assert body["status"] == "degraded"


async def test_readyz_is_independent_of_healthz(client) -> None:
    """Probing readiness must not mutate or depend on liveness state."""
    before = (await client.get("/readyz")).json()
    await client.get("/healthz")
    assert (await client.get("/readyz")).json() == before


async def test_readyz_is_stable_across_repeated_probes() -> None:
    """No per-request mutation, so three probes in a row are byte-identical."""
    database = FakeDatabase()
    async with asgi_client(create_app(container=container_for(database))) as client:
        bodies = [(await client.get("/readyz")).json() for _ in range(3)]
    assert bodies[0] == bodies[1] == bodies[2]


async def test_readyz_content_type_is_json(client) -> None:
    response = await client.get("/readyz")
    assert response.headers["content-type"] == "application/json"
