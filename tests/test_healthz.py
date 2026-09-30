"""GET /healthz — liveness.

Contract: 200 with exactly `{"status": "ok"}`. Liveness must stay trivial and
dependency-free: if this endpoint ever checks a dependency, a dead database
will take the container down with it.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


async def test_healthz_returns_200(client) -> None:
    response = await client.get("/healthz")
    assert response.status_code == 200


async def test_healthz_body_is_exactly_status_ok(client) -> None:
    """Exact-equality pins the payload shape; extra keys break the contract."""
    response = await client.get("/healthz")
    assert response.json() == {"status": "ok"}


async def test_healthz_content_type_is_json(client) -> None:
    response = await client.get("/healthz")
    assert response.headers["content-type"] == "application/json"


async def test_healthz_is_idempotent(client) -> None:
    """Repeat probes must not drift — a restart loop depends on this."""
    first = await client.get("/healthz")
    second = await client.get("/healthz")
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()


async def test_healthz_declares_no_version_and_no_time(client) -> None:
    """The body must be stable: no timestamps, versions, or host-specific data."""
    body = (await client.get("/healthz")).json()
    assert set(body) == {"status"}
