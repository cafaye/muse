"""Unknown routes — the 404 surface.

A scaffold that only advertises its happy paths hides typos in client URLs until
production. Unknown paths must 404 as JSON, not leak an HTML traceback page.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


async def test_unknown_path_returns_404(client) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404


async def test_unknown_path_returns_json_detail(client) -> None:
    response = await client.get("/does-not-exist")
    body = response.json()
    assert body["detail"] == "Not Found"
    assert set(body) == {"detail"}


async def test_wrong_method_on_healthz_returns_405(client) -> None:
    response = await client.post("/healthz")
    assert response.status_code == 405


async def test_probe_paths_need_no_authentication(client) -> None:
    """Probes come from the orchestrator with no credentials; keep them open."""
    for path in ("/healthz", "/readyz"):
        assert (await client.get(path)).status_code == 200
