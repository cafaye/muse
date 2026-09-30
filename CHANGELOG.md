# Changelog

All notable changes to muse. Format follows Keep a Changelog; versioning is
conventional-compat (0.x, so anything may change while pre-1.0).

## [Unreleased]

## [0.2.0] — 2026-09-30

Packet `muse-02`: providers, router, vault, metering, and the HTTP surface. muse can
now serve a completion and bill for it.

### Added

- **Dependencies**, each justified in `README.md`:
  - `litellm` — the routing and metering engine, embedded as a library through its
    Python API (PLAN §2b). No code copied.
  - `cryptography` — AES-256-GCM for the credentials vault.
  - `tenacity` — per-candidate retry with exponential backoff, with an injectable sleep.
  - `psycopg` + `psycopg-pool` — postgres, and a pool rather than a connection so a
    transaction is an isolated handle rather than shared state.
  - `pyyaml` — `config/routes.yaml`.
  - `jsonschema` (dev only) — validates `cafaye.yml` against core's schema when a core
    checkout is available.
- `muse/providers/` — the `Provider` protocol (`complete`, `health`,
  `cost_per_1k_tokens`), a registry that refuses to register a name twice, the LiteLLM
  adapter for openai and anthropic, and `FakeProvider` / `ScriptedProvider`.
  - Prices are integer micro-dollars per 1k tokens. The single place a float touches
    money is the adapter's conversion from litellm's per-token table, which rounds *up*:
    a sub-micro-dollar price must not become zero, which would make a month of usage
    invisible until the invoice did not add up.
  - The adapter scrubs the credential out of provider error text and health details,
    and maps litellm's exception taxonomy onto muse's — with
    `ContextWindowExceededError` checked *before* `BadRequestError`, which it
    subclasses upstream. Reordering them does not crash; it files a too-long prompt as
    a malformed request.
- `muse/routes.py` — `config/routes.yaml` and its loader. A candidate's model is
  resolved at `Route` construction; an unknown key is a load error rather than a
  warning, because a misspelled `max_attemtps` that is silently dropped leaves a route
  retrying once while its author believes it retries three times.
- `muse/router.py` — request to ordered candidates, first success wins, failures
  advance. Real tenacity backoff with an injected sleep, so the suite asserts the
  durations the policy asked for. The price is resolved *before* dispatch, so a model
  muse cannot price is never called.
- `muse/vault.py` — AES-256-GCM under `MUSE_VAULT_KEY`, which must be present, base64
  and exactly 32 bytes or the process does not start. The provider name is the
  additional authenticated data, so a ciphertext copied between rows fails to decrypt
  rather than decrypting under the wrong vendor. Plaintext comes back as a `Secret`.
- `muse/providers/credentials.py` — `VaultCredentials` reads through the vault per
  request, so a rotated key takes effect on the next request rather than at the next
  deploy.
- `muse/metering.py` — one `muse.tokens.consumed` event per served completion, in a
  transaction, with exactly five payload fields. A metering failure is not swallowed:
  the completion was paid for, and returning it without its record would lose the spend
  silently.
- `muse/outbox.py` — the publisher skeleton. core's claim query with
  `for update skip locked`, ack-then-mark, exponential backoff with a cap, and a
  transport interface with no implementation: this packet has no NATS client, and
  `InMemoryTransport` is named for what it is.
- `muse/db.py` — the `Database` protocol and the psycopg adapter.
- `muse/redaction.py` — `Secret`, whose `repr`/`str`/`__format__` are the redaction
  marker, and `redact()` for provider-supplied text.
- `muse/contracts.py` — core's `eventType`, `serviceName` and `subject` patterns, with
  a parity test against a core checkout.
- `migrations/00001_outbox_events.sql` — core's outbox table, column for column.
- `migrations/00002_vault_secrets.sql` — one encrypted key per provider, no plaintext
  column.
- `config/routes.yaml` — the committed routing table: `fast` and `smart`, each with a
  fallback.
- `POST /v1/route` — the completion endpoint. Every non-2xx is
  `application/problem+json` with a cafaye `code`, and `X-Trace-Id` in the header and
  the body.
- `openapi/v1.yaml` — the committed HTTP contract, checked against the running app in
  both directions so it cannot drift from the code.
- `cafaye.yml` — migrated to core's manifest schema, with
  `exposes.events: [muse.tokens.consumed]`.

### Changed

- `/readyz` fills the `db` slot the v0 scaffold reserved: `ok` when the store answers,
  `error` when it does not, and the status is `degraded` rather than `unavailable`
  because a route whose provider needs no database read can still be served. The key
  and the shape are unchanged, which is what "later packets fill the value in" meant.
- 404 and 405 are now `application/problem+json` rather than Starlette's
  `{"detail": ...}`. core says every non-2xx is problem+json, and a client that can
  parse our errors should be able to parse the two it hits while integrating.
- `muse.app` is now the composition root: `create_app(settings, container)` builds a
  `Container` during the lifespan, and accepts one so tests do not need a database.

### Notes

- `muse.tokens.consumed`'s action `consumed` is **not** in core's v0 action
  vocabulary. The packet brief named it, so it is implemented as specified and flagged
  for the manager in `cafaye.yml`: either the vocabulary gains `consumed` or this
  becomes `muse.usage.recorded`. Both are one-line changes; the vocabulary is core's.
- The payload schema for `muse.tokens.consumed` belongs in core, not here — core owns
  every payload schema because a publisher that owns its own is a contract nobody else
  can rely on. It is not written in this packet.
- The outbox publisher has no transport. This packet has no NATS client, and
  `InMemoryTransport` is what the tests and the local stack use.
- Dependency floors are the newest releases satisfying the machine-wide uv
  `exclude-newer = "7 days"` quarantine, so they may trail the true latest. See the
  Dependencies section of `AGENTS.md`.
- 592 tests, 100% branch coverage. The one excluded function is
  `PsycopgDatabase.open()`, which dials.

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

[Unreleased]: https://github.com/cafaye/muse/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cafaye/muse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cafaye/muse/releases/tag/v0.1.0
