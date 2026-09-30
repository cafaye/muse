# Changelog

All notable changes to muse. Format follows Keep a Changelog; versioning is
conventional-compat (0.x, so anything may change while pre-1.0).

## [Unreleased]

Packet `muse-03b`: the gate was red on a clean checkout. A regression found at merge,
fixed at the declarations rather than at the test. While proving the fix, a second
defect of the same class turned up in the gate itself.

### Fixed

- **`bin/prime` could not pass on a fresh clone.** `muse-03` put
  `opentelemetry-exporter-otlp` in the optional `otel` extra, but
  `test_a_configured_endpoint_gets_a_batch_processor` exercises
  `build_provider(endpoint=...)`, which imports that exporter — and `bin/prime` runs
  `uv sync` with no extras. The worktree that developed `muse-03` was green
  because its venv already carried the exporter from an `uv sync --extra otel`; every
  clean clone got
  `ConfigError: MUSE_OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not
  installed`. The worst kind of green: true where it was written, false everywhere
  else. **A test that needs a package makes it a test dependency**, so the dev group
  now asks for the extra (`muse[otel]`) rather than repeating the exporter's floor in
  a second place. The extra stays an extra for the image, where pulling grpcio and
  protobuf into every install is still the wrong default.
- **`bin/prime` asserted nothing about the lockfile.** Its own comment claimed
  `uv sync --frozen` "refuses to re-resolve a stale lock", and the flag on line 12 was
  `--frozen`. It does not: `--frozen` means *do not update the lock*, which is exactly
  what lets a lock that disagrees with `pyproject.toml` install silently; the assertion
  is `--locked`. Demonstrated against a stale lock: `uv sync --frozen` exited 0 with 77
  packages and no exporter, `uv run pytest` exited 0 with 760 passed, and `uv run`
  quietly **rewrote `uv.lock` on the way there**. The gate passed over a lockfile nobody
  had committed — the same "green in one worktree, false elsewhere" shape as the
  exporter defect, and the one a clean-checkout proof cannot catch, because a clean
  checkout has a correct lock. Every `uv sync`/`uv run` in `bin/prime` now carries
  `--locked`, including the `uv run` lines: `uv run` re-resolves by default, so a
  guarded sync followed by a bare `uv run` reopened the hole. The Dockerfile's syncs
  carried the same false comment and are now `--locked` too.
- **`pydantic` and `starlette` are now declared.** `api.py` and `main.py` import both
  at module scope, and both were reaching the venv only as `fastapi`'s transitive
  dependencies — the same accident one fastapi repin away from a broken build.

### Added

- `tests/test_dependencies.py` — a recurrence guard for both halves, asserted from
  the declarations rather than from the venv. Every module `src/` and `tests/` import
  must be provided by the closure of `[project.dependencies]` plus the dependency
  groups as `uv.lock` records it, resolved at **submodule** granularity, because
  `opentelemetry` is declared while `opentelemetry.exporter.otlp` is not. Every
  third-party root must additionally be a *direct* declaration, since transitive
  availability is not a declaration. Ownership is read from the installed RECORD
  files, so the check fails both with the extra installed (the owner is outside the
  closure) and with it absent (the module cannot be found) — which is what makes it
  trustworthy in a dirty venv. A third test names the OTLP exporter specifically, so
  the regression this packet fixes fails with a message that points at itself.
- Four more assertions in that module, each of which closes a way the guard could have
  been decorative:
  - **the lock agrees with `pyproject.toml`** — the fact `--locked` checks, read from
    two files on disk so it also holds in a dirty tree.
  - **`bin/prime` passes `--locked` everywhere** — read from the script, so the gate
    cannot quietly lose the assertion. The whole script is checked, not line 12,
    because the exposure was the *combination* with an unguarded `uv run`.
  - **the walk found something** — every assertion above is a set-difference over a
    helper's result, so an empty result makes all of them pass. Breaking `_imports`,
    `_declared`, `_gate_closure` or `_owners` one at a time left the two general
    guards green every time; only the by-name OTLP test noticed. The floor is now
    asserted before any absence is, which is the canary test's own discipline from
    `test_trace_propagation.py` ("the canary is absent" is also true of an export that
    produced no spans).
  - **the exporter stays out of the image** — `[project.dependencies]` must not name
    the expensive distributions and every Dockerfile `uv sync` must carry `--no-dev`,
    so the fix cannot be "simplified" into shipping grpcio and protobuf everywhere.
- `AGENTS.md` rule 19 and a README section: a green gate is a claim about a clean
  machine, and `rm -rf .venv && bin/prime` is how you check it.

### Deliberately not done

- `test_a_configured_endpoint_gets_a_batch_processor` is unchanged. It tests real
  behavior — an endpoint configured means a batch processor is wired — so the
  dependency moved instead of the test.
- No `pytest.mark.skipif` on the exporter. Skipping would trade a broken gate for a
  gate that quietly tests less, which is the failure mode this packet exists to end.
- Coverage stays at 100%; the added tests are in `tests/`, so they do not dilute it.

### Proof

The salvage was unverified, so it was verified before being trusted: reverting
`pyproject.toml` and `uv.lock` to `f411eb5` and deleting `.venv` reproduces the
merge-red gate (4 failed, 756 passed), with all three original dependency tests firing
on it. With the fix applied, `rm -rf .venv && bin/prime` gives **763 passed, 2 skipped,
coverage 100.00%, exit 0**. The salvage's shape was also checked against the objection
that it makes the dev environment carry a production dependency: it does not. The
Dockerfile's command sequence installs 69 distributions with no
`opentelemetry-exporter-otlp`, no grpcio and no protobuf, against the gate's 85.

Packet `muse-03`: OpenTelemetry, trace propagation, and bounded retry budgets. PLAN §7
adopts W3C `traceparent` with traces in the platform collector, and bounded retry
budgets with circuit breakers ("briefs forbid naive retries").

### Added

- **Dependencies**, each justified in `README.md`:
  - `opentelemetry-api`, `opentelemetry-sdk` — PLAN §7's "adopt from first deploy".
    The SDK's own provider and span types rather than a hand-rolled span, so traces
    are the shape every other OpenTelemetry tool expects.
  - `opentelemetry-exporter-otlp` as the **optional `otel` extra**, imported only
    when an endpoint is configured. As a hard dependency it would pull grpcio and
    protobuf into every install to serve a path most deployments never reach.
- `muse/telemetry.py` — `TraceParent` and `parse_traceparent`, plus
  `ALLOWED_SPAN_ATTRIBUTES` and `record()`: the redaction boundary for telemetry.
- `TraceMiddleware` in `muse/main.py` — continues a well-formed inbound
  `traceparent`, starts a new trace when it is absent or malformed, and echoes both
  `traceparent` and the existing `X-Trace-Id`. **Pure ASGI**, not
  `@app.middleware("http")`: the decorator runs the downstream app in a separate
  task, so the router would have seen no trace and every provider span would have
  been orphaned.
- Outbound propagation: `LiteLLMProvider` reads the ambient OpenTelemetry context
  and sends `traceparent` as `extra_headers`, so the `Provider` protocol does not
  grow an argument every future adapter would have to remember to honour.
- Three spans: `muse.request`, `muse.route` (the routing decision), and
  `muse.provider.call`.
- `muse/breaker.py` — a circuit breaker per provider. Opens after N consecutive
  *transient* failures, admits one half-open probe, closes on success. Consulted
  before the provider is touched, so a held-back provider is never dialled.
- `budget_seconds` and `backoff_jitter` in `config/routes.yaml`; a per-route
  `retry_indeterminate` flag.
- `MUSE_BREAKER_THRESHOLD`, `MUSE_BREAKER_RESET_SECONDS`,
  `MUSE_OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`.
- `traceparent` in `openapi/v1.yaml`, as an optional request parameter and on every
  response, with a test that drives the running app and reads the real headers.
- Tests: `test_telemetry.py`, `test_breaker.py`, `test_retry_budget.py`,
  `test_trace_propagation.py`, `test_resilience_config.py`, plus a
  `tests/support/tracing.py` that swaps the SDK's in-memory exporter in for the OTLP
  one.

### Changed

- **A read timeout is no longer retried by default.** litellm's `Timeout` with no
  HTTP status behind it now maps to the new `ProviderIndeterminate` rather than a
  retryable `ProviderTimeout`. The request was in flight when the deadline passed, so
  the vendor may have completed and billed it, and without an idempotency key there
  is no way to ask which happened — retrying can charge a customer twice for one
  request. A route opts in with `retry_indeterminate: true`. A **408** stays
  retryable: that is the server saying the request never arrived complete.
  Two adapter tests changed with this and say why.
- **The backoff is jittered.** Without it, every muse that failed at the same instant
  retried at the same instant, so a provider recovering from an outage was hit by a
  synchronised wave. The window is `[ceiling * (1 - jitter), ceiling]`, so the floor
  rises with the exponential (the backoff cannot go backwards) and jitter can never
  push a delay past the cap. `jitter: 0` restores the exact previous schedule.
- The retry stop condition is now muse's own rather than tenacity's, so the clock is
  injectable. A count alone did not bound time: six attempts against a 4s ceiling is
  20s of waiting before the last attempt is sent. The deadline is checked before each
  wait, so muse never begins a sleep that would carry the request past it.

### Security

- **No span attribute may carry prompt, completion or credential text.** Enforced by
  an allowlist at one choke point (`muse.telemetry.record`), not by discipline at each
  call site: the realistic leak is a well-meaning `muse.prompt` added in six months,
  not an attacker. A `Secret` is refused on its *type*, never compared by value.
  `error.message` is deliberately **not** allowlisted — a vendor's content-policy
  rejection quotes the offending content back, so the class name is recorded and the
  message is not. The canary test drives a completion through the real app with a
  unique string in both prompt and completion and asserts it appears nowhere in the
  rendered span payload; mutation testing confirms it fails when a prompt is recorded.

### Notes

- **muse has no `Idempotency-Key`.** PLAN §7 assigns one to muse and it is not built,
  which is the direct reason `ProviderIndeterminate` defaults to un-retried. Flagged
  for the manager: it is the one thing standing between muse and safely retrying
  ambiguous timeouts, and it also matters for the caller-visible double-billing risk.
- **`timeout_seconds` in `routes.yaml` is still not enforced.** It is declared and
  documented but nothing applies it, so a single hung provider call is bounded only by
  the caller's own timeout. Pre-existing from muse-02 and left alone deliberately: the
  packet is about retry budgets, and the only way to test a real `asyncio.timeout`
  expiry is a real sleep, which this repo's flake policy forbids. Follow-up packet.
- A held-back provider does not fail the request — the fallback still serves it. A
  breaker that failed the whole request would be strictly worse than none, since one
  vendor being down would take out every route with a healthy fallback.
- Only transient failures count against the breaker. A 400 or a 401 is muse sending
  something the provider is right to refuse, and counting it would let one caller with
  a malformed request take the route down for everybody.
- `error.type` uses its OpenTelemetry dotted spelling (set through `record`'s mapping
  form, since a dotted name is not a legal keyword argument); muse's own attributes
  are underscored. Both conventions are documented on the allowlist.
- 757 tests, 100% branch coverage. Dependency floors satisfy the machine-wide uv
  `exclude-newer = "7 days"` quarantine and so may trail the true latest.

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
