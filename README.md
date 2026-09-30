# muse

LLM routing, a credentials vault, and token metering for
[cafaye](https://github.com/cafaye) — Phase 4.

**Why Python:** LiteLLM, the routing/metering engine muse embeds, is a Python
library — the FastAPI/Pydantic ecosystem is the only place where the same
types (OpenAI, Anthropic, Bedrock request/response shapes) exist in both the
embedding library and the service around it, so there is one model of the
domain instead of a hand-written port. See `PLAN.md` §2b.

**Status: v0 scaffold — no LLM logic yet.** This repo is the service skeleton:
an app factory, health probes, a test harness, and a container build. Provider
adapters, the encrypted vault, and metering are later packets.

## Requirements

Python 3.14 and [uv](https://docs.astral.sh/uv/). Both are pinned in this repo:

```sh
mise install          # reads mise.toml -> python 3.14, uv 0.12.20
```

## Quick start

```sh
bin/prime             # uv sync --frozen + ruff + pytest (the gate)
uv run uvicorn --factory muse.main:create_app --reload
```

Then:

```sh
curl localhost:8000/healthz   # {"status":"ok"}
curl localhost:8000/readyz    # {"status":"ok","checks":{"db":"skipped"}}
```

## Endpoints

| Method | Path       | Purpose                                              |
| ------ | ---------- | ---------------------------------------------------- |
| GET    | `/healthz` | Liveness. Always `{"status":"ok"}`; no dependencies. |
| GET    | `/readyz`  | Readiness. `checks` is reserved for `db` (the vault). |

`/healthz` deliberately checks nothing. If liveness ever consults a dependency,
a database outage restarts every muse container and turns one fault into an
outage. Readiness is where dependency checks belong — see `AGENTS.md`.

## Layout

```
src/muse/main.py     app factory (create_app) + the two probe routes
tests/               pytest suite, httpx ASGI transport, no socket
bin/prime            uv sync --frozen + ruff + pytest — the gate
Dockerfile           multi-stage, python:3.14-slim + uv, non-root
docker-compose.yml   local run of the service
cafaye.yml           cafaye manifest draft (core owns the schema)
```

## Tests

```sh
uv run pytest                       # gate: 100% branch coverage required
uv run pytest -m unit               # skip anything marked integration
uv run pytest --cov-report=html     # htmlcov/index.html
uv run ruff check . && uv run ruff format --check .
```

Tests drive the ASGI app in-process over `httpx.ASGITransport` — no server, no
network, no port. See `AGENTS.md` for the conventions.

## Docker

```sh
docker compose up --build           # http://localhost:8000
```

## License

AGPL-3.0-only — see the cafaye org for the platform's licensing rationale.
