# Changelog

All notable changes to muse. Format follows Keep a Changelog; versioning is
conventional-compat (0.x, so anything may change while pre-1.0).

## [Unreleased]

### Added

- Packet `muse-02` foundations.
  - Runtime deps: `litellm` (the routing/metering engine muse embeds as a
    library), `cryptography` (AES-256-GCM for the vault), `tenacity` (per-candidate
    retry backoff), `psycopg` + `psycopg-pool` (postgres), `pyyaml`
    (`config/routes.yaml`). Each is justified in `README.md`.
  - `muse/errors.py` — the error taxonomy. Subclasses of `ProviderError` split on
    one question: would the identical call plausibly succeed a moment later?
    `RETRYABLE_PROVIDER_ERRORS` is the list the router retries on, so an unlisted
    error ends the request on the first candidate.
  - `muse/redaction.py` — `Secret` (a `str` whose `repr`/`str`/`__format__` are
    the redaction marker, with a constant-time `__eq__`) and `redact()`, which
    scrubs a credential out of provider-supplied text before it becomes a log line
    or a `problem+json` body.
  - `muse/contracts.py` — core's `eventType`, `serviceName` and `subject`
    patterns, with validators. The patterns are a copy of
    `core/schemas/event-envelope.schema.json`; `tests/test_contracts.py` asserts
    the copy is byte-identical whenever `MUSE_CORE_SCHEMAS` points at a core
    checkout.
  - `muse/db.py` — the `Database` protocol (execute / fetchone / transaction) and
    `PsycopgDatabase` over a *pool*, so a transaction is an isolated handle rather
    than shared state.
  - `migrations/00001_outbox_events.sql` — core's outbox table, column for column,
    with core's own patterns as CHECK constraints and the partial index the
    publisher's claim query needs.
  - `migrations/00002_vault_secrets.sql` — one encrypted key per provider. No
    plaintext column, by design.

## [0.1.0] — 2026-09-30

Initial scaffold (packet `muse-01`). No LLM logic yet — see `AGENTS.md`.

### Added

- `pyproject.toml` managed by uv; runtime deps `fastapi`, `uvicorn`; dev group
  `pytest`, `httpx`, `anyio`, `pytest-cov`, `ruff`. `uv.lock` committed.
- `src/muse/main.py` — `create_app()` application factory (no module-level
  singleton; `uvicorn --factory muse.main:create_app`).
- `GET /healthz` → `{"status":"ok"}`. Liveness checks nothing on purpose.
- `GET /readyz` → `{"status":"ok","checks":{"db":"skipped"}}`. The `checks` map
  and its `db` slot are reserved for the Phase 4 credentials vault, so later
  packets fill in a value instead of reshaping the response.
- `tests/` — 23 tests written before the implementation, driven in-process over
  `httpx.ASGITransport` (no socket, no network). Covers probe status codes and
  exact JSON shapes, app-factory isolation, and the 404/405 surface.
- 100% branch-coverage gate (`--cov-fail-under=100`) plus strict markers and
  strict config.
- `Dockerfile` — multi-stage, `python:3.14-slim`, uv 0.12.20, non-root runtime
  user, `HEALTHCHECK`, frozen lockfile install.
- `docker-compose.yml`, `bin/prime` (the gate), `mise.toml` (python + uv pins),
  `.python-version`, `.gitignore`, `AGENTS.md`, `README.md`, and a `cafaye.yml`
  manifest draft.

### Notes

- Dependency floors are the newest releases that satisfy the machine-wide
  uv `exclude-newer = "7 days"` supply-chain quarantine, so fastapi 0.141.1 and
  uvicorn 0.53.0 may trail the true latest. See the Dependencies section of
  `AGENTS.md`.
- `cafaye.yml` is a draft: `core` owns the schema and does not exist yet.

[Unreleased]: https://github.com/cafaye/muse/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/cafaye/muse/releases/tag/v0.1.0