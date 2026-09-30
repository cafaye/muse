# syntax=docker/dockerfile:1
#
# muse — multi-stage, python slim, uv-managed.
# Stage 1 resolves and installs into /opt/venv; stage 2 copies only the venv and
# the app, so no build toolchain or uv cache ends up in the runtime image.

# --- stage 1: build the venv -------------------------------------------------
FROM python:3.14-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:0.12.20 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv

WORKDIR /app

# Dependencies first: this layer is cached until pyproject.toml/uv.lock change.
# --locked refuses to re-resolve, so a lock that no longer matches pyproject.toml
# fails the build instead of silently installing something different from what CI
# tested. (--frozen would not: it only means "do not update the lock".)
#
# --no-dev on every sync is load-bearing rather than tidiness. The dev group carries
# the OTLP exporter because the suite imports it, and the image must not inherit it.
# Asserted by tests/test_dependencies.py::test_the_exporter_stays_out_of_the_image.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-install-project --no-dev

COPY src/ ./src/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev

# --- stage 2: runtime --------------------------------------------------------
FROM python:3.14-slim AS runtime

# Non-root: nothing in this service writes to the filesystem.
RUN groupadd --system --gid 1001 muse \
 && useradd --system --uid 1001 --gid muse --no-create-home muse

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONFAULTHANDLER=1

COPY --from=builder --chown=muse:muse /opt/venv /opt/venv

WORKDIR /app
COPY --chown=muse:muse src/ ./src/

USER muse
EXPOSE 8000

# Probes match the compose healthcheck; /healthz is liveness, /readyz readiness.
HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD ["/opt/venv/bin/python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/healthz', timeout=2).status == 200 else 1)"]

# --factory: the app comes from create_app(), never a module-level singleton.
CMD ["/opt/venv/bin/uvicorn", "--factory", "muse.main:create_app", \
     "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]