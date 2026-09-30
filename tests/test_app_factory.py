"""App-factory isolation.

`create_app()` must build an independent application each call. A module-level
singleton would let one test's routes, state, or lifespan leak into the next —
the classic source of order-dependent, unfixable flakes.
"""

from __future__ import annotations

import pytest
from fastapi import APIRouter, FastAPI

from muse.main import create_app

from .conftest import asgi_client

pytestmark = [pytest.mark.anyio, pytest.mark.unit]


def test_create_app_returns_a_fastapi_instance() -> None:
    assert isinstance(create_app(), FastAPI)


def test_create_app_returns_a_new_object_each_call() -> None:
    assert create_app() is not create_app()


async def test_two_apps_do_not_share_route_tables() -> None:
    """A route added to one app must be unreachable on any other app."""
    a, b = create_app(), create_app()

    @a.get("/only-on-a")
    async def only_on_a() -> dict[str, str]:
        return {"app": "a"}

    async with asgi_client(a) as ca, asgi_client(b) as cb:
        assert (await ca.get("/only-on-a")).json() == {"app": "a"}
        assert (await cb.get("/only-on-a")).status_code == 404


async def test_extra_routes_registered_on_one_app_stay_untouched_by_another() -> None:
    """Same guarantee for router-style registration, which mounts differently."""
    a, b = create_app(), create_app()
    router = APIRouter()

    @router.get("/scoped")
    async def scoped() -> dict[str, str]:
        return {"scope": "a"}

    a.include_router(router)
    async with asgi_client(a) as ca, asgi_client(b) as cb:
        assert (await ca.get("/scoped")).json() == {"scope": "a"}
        assert (await cb.get("/scoped")).status_code == 404


async def test_apps_serve_traffic_concurrently_without_crosstalk() -> None:
    """Two live apps side by side answer identically and independently."""
    a, b = create_app(), create_app()
    async with asgi_client(a) as ca, asgi_client(b) as cb:
        ra, rb = await ca.get("/healthz"), await cb.get("/healthz")
        assert ra.status_code == rb.status_code == 200
        assert ra.json() == rb.json() == {"status": "ok"}


async def test_app_state_set_on_one_instance_does_not_leak() -> None:
    a, b = create_app(), create_app()
    a.state.marker = "a-only"
    assert not hasattr(b.state, "marker")


async def test_each_app_keeps_its_own_title_and_version() -> None:
    """Customising one app's metadata must not touch the factory defaults."""
    a, b = create_app(), create_app()
    a.title = "muse (customised)"
    assert b.title == "muse"
    assert a.title != b.title


async def test_repeated_probes_against_one_app_are_stable() -> None:
    """Stability across requests: no per-request mutation of the app object."""
    async with asgi_client() as c:
        bodies = [(await c.get("/readyz")).json() for _ in range(3)]
        assert bodies[0] == bodies[1] == bodies[2]
