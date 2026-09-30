"""Unknown routes and wrong methods — the 404/405 surface.

A service that only advertises its happy paths hides typos in client URLs until
production, and the shape of these two responses is worth pinning for a second reason:
they are what a client hits *while integrating*, before it knows anything about muse's
happy path.

The body changed in packet `muse-02`. It used to be Starlette's
`{"detail": "Not Found"}` as `application/json`; it is now
`application/problem+json` with a cafaye `code`, because core's openapi conventions
say *every* non-2xx is problem+json. A client written to parse one shape and handed
the other fails on the two responses it is most likely to see first.
"""

from __future__ import annotations

import pytest

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


async def test_unknown_path_returns_404(client) -> None:
    response = await client.get("/does-not-exist")
    assert response.status_code == 404


async def test_unknown_path_returns_problem_json(client) -> None:
    """Not `{"detail": "Not Found"}`. The exact shape is asserted so a future change
    to the handler is a failing test rather than a client that stops parsing."""
    response = await client.get("/does-not-exist")

    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json() == {
        "type": "https://errors.cafaye.com/not_found",
        "title": "Not found",
        "status": 404,
        "detail": "Not Found",
        "instance": "/does-not-exist",
        "code": "not_found",
        "trace_id": response.headers["X-Trace-Id"],
    }


async def test_wrong_method_on_healthz_returns_405(client) -> None:
    response = await client.post("/healthz")
    assert response.status_code == 405


async def test_a_wrong_method_also_returns_problem_json(client) -> None:
    """Even the ops surface. One error shape means a client writes one parser."""
    response = await client.post("/healthz")
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["status"] == 405
    assert response.json()["code"] == "not_found"


async def test_an_unknown_path_carries_a_trace_id(client) -> None:
    """A 404 is exactly when someone needs to correlate a report with a log."""
    response = await client.get("/does-not-exist")
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


async def test_probe_paths_need_no_authentication(client) -> None:
    """Probes come from the orchestrator with no credentials; keep them open."""
    for path in ("/healthz", "/readyz"):
        assert (await client.get(path)).status_code == 200
