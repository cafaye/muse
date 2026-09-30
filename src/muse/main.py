"""muse — FastAPI application factory.

v0 scope: the application factory and the two orchestrator probes. No LLM
logic, no provider adapters, no vault — those are later packets (AGENTS.md).

The app is built by :func:`create_app` rather than living as a module-level
singleton so tests, the ASGI server (``uvicorn --factory muse.main:create_app``),
and future workers each get an independent instance.
"""

from __future__ import annotations

from typing import Literal

from fastapi import FastAPI
from pydantic import BaseModel, Field

APP_TITLE = "muse"
APP_VERSION = "0.1.0"
APP_DESCRIPTION = "LLM routing, credentials vault, and token metering for cafaye."

#: Overall verdict reported by ``/readyz``.
ReadinessStatus = Literal["ok", "degraded", "unavailable"]
#: Per-dependency verdict reported inside ``/readyz``'s ``checks`` map.
CheckState = Literal["ok", "skipped", "error"]

#: v0 runs no database. The ``db`` slot stays in the payload (with a ``skipped``
#: sentinel) so the Phase 4 vault only fills a value in instead of changing the
#: response shape that ``guard`` and dashboards already depend on.
DB_CHECK_V0 = "skipped"


class Health(BaseModel):
    """Liveness payload. Exactly one field: probes must stay trivially stable."""

    status: Literal["ok"]


class ReadinessChecks(BaseModel):
    """Per-dependency readiness. ``db`` is reserved for the vault store."""

    db: CheckState = Field(description="Credentials-vault store; skipped until the vault lands.")


class Readiness(BaseModel):
    """Readiness payload. Status plus the reserved per-dependency checks."""

    status: ReadinessStatus
    checks: ReadinessChecks


def create_app() -> FastAPI:
    """Build a muse application.

    Returns a new instance on every call — callers own it, nothing is shared.
    """
    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
    )

    @app.get(
        "/healthz",
        response_model=Health,
        tags=["ops"],
        summary="Liveness probe",
    )
    async def healthz() -> Health:
        """Liveness. Checks nothing on purpose — a dependency here becomes a restart loop."""
        return Health(status="ok")

    @app.get(
        "/readyz",
        response_model=Readiness,
        tags=["ops"],
        summary="Readiness probe",
    )
    async def readyz() -> Readiness:
        """Readiness. The place where dependency checks belong; ``db`` is reserved."""
        return Readiness(status="ok", checks=ReadinessChecks(db=DB_CHECK_V0))

    return app
