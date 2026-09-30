"""GET /readyz — readiness.

Contract: 200 with a top-level `status` and a `checks` map whose `db` entry is
reserved for the Phase 4 credentials-vault store. In v0 there is no database, so
`db` reports the sentinel `"skipped"` — the key exists now so dashboards,
alerts, and contract tests written later keep working when the vault lands.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


async def test_readyz_returns_200(client) -> None:
    response = await client.get("/readyz")
    assert response.status_code == 200


async def test_readyz_body_has_expected_shape(client) -> None:
    """Exact-equality: this is the reserved shape the vault packet will fill in."""
    response = await client.get("/readyz")
    assert response.json() == {"status": "ok", "checks": {"db": "skipped"}}


async def test_readyz_reserves_a_db_check_key(client) -> None:
    """The `db` slot must exist in v0 so later packets only fill it in."""
    body = (await client.get("/readyz")).json()
    assert "db" in body["checks"]


async def test_readyz_status_is_ok_and_not_ready_strings(client) -> None:
    body = (await client.get("/readyz")).json()
    assert body["status"] == "ok"
    assert body["status"] in {"ok", "degraded", "unavailable"}


async def test_readyz_is_independent_of_healthz(client) -> None:
    """Probing readiness must not mutate or depend on liveness state."""
    ready_before = (await client.get("/readyz")).json()
    await client.get("/healthz")
    ready_after = (await client.get("/readyz")).json()
    assert ready_before == ready_after


async def test_readyz_content_type_is_json(client) -> None:
    response = await client.get("/readyz")
    assert response.headers["content-type"] == "application/json"
