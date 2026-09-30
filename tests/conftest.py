"""Shared fixtures.

Every test drives the ASGI app in-process over httpx's ASGITransport — no
socket, no server, no network. That keeps the suite fast and deterministic
(PLAN §3: "very good tests for everything").
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
import pytest

from muse.main import create_app

BASE_URL = "http://muse.test"


@asynccontextmanager
async def asgi_client(app=None) -> AsyncIterator[httpx.AsyncClient]:
    """Yield a client bound to `app` (a fresh one by default) over ASGI transport.

    A plain async context manager rather than a fixture: the isolation tests need
    several clients alive at once, which nested fixtures express badly.
    """
    target = app if app is not None else create_app()
    # `raise_app_exceptions=False` so an unhandled exception surfaces as the response
    # the client would actually receive. httpx re-raises by default, which would make
    # every error-handler test assert on a traceback instead of on the problem+json
    # body a caller gets.
    transport = httpx.ASGITransport(app=target, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as c:
        yield c


@pytest.fixture
def anyio_backend() -> str:
    """Pin the anyio pytest plugin to asyncio only (single backend, no param matrix)."""
    return "asyncio"


@pytest.fixture
def app():
    """A fresh application per test.

    `create_app()` returns a new object every call; never reuse a module-level
    singleton so tests cannot leak state into one another.
    """
    return create_app()


@pytest.fixture
async def client(app) -> AsyncIterator[httpx.AsyncClient]:
    """HTTP client bound to `app` via ASGI transport."""
    async with asgi_client(app) as c:
        yield c
