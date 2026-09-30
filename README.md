# muse

LLM routing, an encrypted credentials vault, and token metering for
[cafaye](https://github.com/cafaye) — Phase 4.

A client asks for a *model*. muse decides which vendor serves it, falls through to the
next one when the first is rate limited or down, keeps provider API keys encrypted at
rest, and emits a `muse.tokens.consumed` event for every completion it serves so
`billing` can turn it into an invoice.

**Why Python:** LiteLLM, the routing/metering engine muse embeds, is a Python library
(PLAN §2b). It is used through its Python API — the adapter is about 200 lines around
`acompletion` and `get_model_info` — and none of its code is copied.

## Status: v1 core

Routing, the vault, metering and the HTTP surface are done and tested. Not in this
version: streaming, tool calls, embeddings, images, batches, and a real admin UI. Auth
is a stub — the bearer header's presence is checked and the token is not verified; real
JWT verification arrives with the guard contract.

## Requirements

Python 3.14 and [uv](https://docs.astral.sh/uv/), both pinned in this repo. Postgres
for the vault and the outbox. An OpenAI and/or an Anthropic API key, if you want
anything other than the probes to answer.

```sh
mise install          # reads mise.toml -> python 3.14, uv 0.12.20
```

## Quick start

```sh
# 1. A vault key. There is no default and no fallback: muse refuses to start
#    without one, because a vault that boots with a default key is a vault whose
#    keys are readable by anyone who has read this repository.
uv run python -m muse.vault

# 2. Point it at a database and apply the migrations.
createdb muse
psql muse -f migrations/00001_outbox_events.sql
psql muse -f migrations/00002_vault_secrets.sql

# 3. Store a credential. `muse` has no admin API in v1; this is a psql session.
MUSE_VAULT_KEY=<the key from step 1> uv run python - <<'PY'
import asyncio, os
from muse.db import PsycopgDatabase
from muse.redaction import Secret
from muse.vault import Vault, load_vault_key

async def main():
    db = await PsycopgDatabase.open(os.environ["MUSE_DATABASE_URL"])
    vault = Vault(db, load_vault_key())
    await vault.put("openai", Secret(input("openai key: ")))
    print("stored:", await vault.providers())

asyncio.run(main())
PY

# 4. Run it.
MUSE_VAULT_KEY=<key> MUSE_DATABASE_URL=postgres://localhost/muse \
  uv run uvicorn --factory muse.main:create_app --reload
```

Or with compose, which does all of the above except the API key:

```sh
MUSE_VAULT_KEY=<key> docker compose up --build
```

## Endpoints

| Method | Path         | Purpose                                                    |
| ------ | ------------ | ---------------------------------------------------------- |
| POST   | `/v1/route`   | Serve a chat completion from the first candidate that works. |
| GET    | `/healthz`   | Liveness. `{"status":"ok"}`; consults nothing.              |
| GET    | `/readyz`    | Readiness. Reports the `db` slot.                           |

The probe endpoints are deliberately absent from `openapi/v1.yaml`: they are
infrastructure, not contract, and documenting them would put `/healthz` under the `/v1`
prefix rule it does not belong under.

`/healthz` checks nothing on purpose. If liveness ever consults a dependency, a
database outage restarts every muse container and turns one fault into an outage.
`/readyz` is where dependency checks belong, and it reports `degraded` rather than
`unavailable` for an unreachable store — a route whose provider needs no database read
can still be served, and telling an orchestrator the service is down restarts
containers that could have answered.

### Calling it

```sh
curl -s localhost:8000/v1/route \
  -H 'Authorization: Bearer <any non-empty token>' \
  -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}]}'
```

The token is not verified. The header's presence is what is checked.

Every non-2xx is `application/problem+json`:

| Code                | Status | When                                            |
| ------------------- | ------ | ----------------------------------------------- |
| `unauthorized`      | 401    | No bearer credential supplied.                  |
| `not_found`         | 404    | No route for the requested model.               |
| `validation_failed` | 422    | The body is not a valid route request.          |
| `unavailable`       | 503    | Every candidate provider failed.                |
| `internal`          | 500    | muse failed. `trace_id` is the handle.          |

A 503 means muse is up and the models are not — the one case where retrying the same
request is worthwhile.

## Routing

`config/routes.yaml` maps a client-facing model name to an ordered list of candidates.
The first that succeeds wins; a failure advances to the next. A client never learns
which vendor answered and cannot ask for one, which is what makes the chain work.

```yaml
routes:
  - model: fast
    candidates:
      - provider: openai
        model: gpt-4o-mini
      - provider: anthropic
        model: claude-haiku-4-5
```

Every field is documented in the file itself, and an unknown key is a **load error**
rather than a warning: a misspelled `max_attemtps` that is silently dropped leaves a
route retrying once while its author believes it retries three times.

A candidate's `model` is optional and defaults to the route's. `max_attempts` defaults
to 1 — no retry — because a retry that has not been measured against a real incident
is latency added to every failure. Only transient failures are retried (timeouts, rate
limits, 5xx, connection resets); a rejected credential or a malformed request advances
immediately.

The committed file is loaded, validated against the provider registry and asserted to
give every route a fallback by the suite, so the file that ships is the file that was
tested.

## The vault

AES-256-GCM, one row per provider in `vault_secrets`, key from `MUSE_VAULT_KEY` (32
bytes, base64). muse refuses to boot if it is missing, not base64, or the wrong length.

- **The provider name is the additional authenticated data.** A ciphertext copied from
  one row to another fails to decrypt rather than decrypting under the wrong vendor.
- **A fresh nonce per seal**, prepended to the ciphertext so a row is self-contained.
- **Plaintext comes back as a `Secret`**, whose `repr`, `str` and `__format__` are the
  redaction marker — so an accidental `%s` in a log line cannot leak one.
- **Keys are read per request**, not cached at boot, so a rotation takes effect on the
  next request rather than at the next deploy.

## Metering

Every served completion inserts one `muse.tokens.consumed` event into `outbox_events`
in a transaction, and then that transaction commits or rolls back with the row. The
envelope is core's; `data` carries exactly five fields:

```json
{"model": "gpt-4o-mini", "provider": "openai", "tokens_in": 12, "tokens_out": 8, "cost_micros": 7}
```

Money is integer micro-dollars everywhere. A float that rounds differently on two
machines is a reconciliation bug nobody can reproduce. The `muse.tokens.consumed`
action is **not** in core's v0 action vocabulary; the packet brief named it, so it is
implemented as specified and flagged for the manager (see `cafaye.yml`).

A metering failure is *not* swallowed. The completion was already paid for, so
returning it without its record would lose the spend silently; the request fails
instead and the loss is visible.

## Dependencies

Each is a runtime dependency this packet added, and the reason it is not avoidable:

| Package         | Why                                                                          |
| --------------- | ---------------------------------------------------------------------------- |
| `litellm`       | The router and metering engine, embedded as a library (PLAN §2b).             |
| `cryptography`  | AES-256-GCM for the vault. The only audited AEAD implementation for Python.   |
| `tenacity`      | Per-candidate retry with exponential backoff, with an injectable sleep.       |
| `psycopg`       | Postgres. `vault_secrets` and `outbox_events` both live there.               |
| `psycopg-pool`  | A pool, not a connection, so a transaction is an isolated handle.             |
| `pyyaml`        | `config/routes.yaml`.                                                        |
| `opentelemetry-api` | W3C trace context and the `Tracer`/`Span` types (PLAN §7 "adopt from first deploy"). |
| `opentelemetry-sdk`  | The `TracerProvider` and span processors. Used rather than a hand-rolled span type, so traces are the shape every other OpenTelemetry tool expects. |
| `opentelemetry-exporter-otlp` | **Optional extra** (`muse[otel]`), imported only when `MUSE_OTEL_EXPORTER_OTLP_ENDPOINT` is set. As a hard dependency it would pull grpcio and protobuf into every install to serve a path most deployments never reach. The dev group pulls the extra in, because the suite tests that path. |
| `pydantic`        | The request and response models in `api.py` and `main.py`. Imported at module scope, so declared rather than inherited from fastapi's pin. |
| `starlette`       | The ASGI types, the header types and `HTTPException` — muse is an ASGI app, and its pure-ASGI middleware imports these directly. Declared for the same reason as `pydantic`. |
| `jsonschema`    | Dev only: validates `cafaye.yml` against core's schema when a checkout is available. |

Floors are the newest releases satisfying the machine-wide uv
`exclude-newer = "7 days"` supply-chain quarantine, so they may trail the true latest.
`uv lock` is the check, not PyPI — see `AGENTS.md`.

### Tracing

Traces are **off by default**: with no endpoint configured muse creates a tracer
provider with no span processor, so spans are built, nested and closed correctly and
then discarded. Nothing leaves the process, which is what keeps the suite hermetic
(AGENTS.md rule 3) and means a default install phones nobody.

| Variable | Default | Effect |
| --- | --- | --- |
| `MUSE_OTEL_EXPORTER_OTLP_ENDPOINT` | unset | OTLP/HTTP endpoint. Unset means export nowhere. Setting it without `muse[otel]` installed is a boot error naming both fixes, not a silent no-op. |
| `OTEL_SERVICE_NAME` | `muse` | `service.name` on the exported resource. |

The container image installs the locked dependency set without the `otel` extra and
without the dev group, so adding an endpoint to a compose deployment needs the extra
built in — or the boot error, which is the honest outcome.

```sh
uv sync --extra otel    # to export traces
```

A local checkout already has it: the dev group pulls `muse[otel]` in, because the
test suite exercises the endpoint path and a test that imports a package makes it a
test dependency. That costs the image nothing — `tests/test_dependencies.py` asserts
`[project.dependencies]` never names the exporter and that every Dockerfile `uv sync`
carries `--no-dev`, so the group cannot leak into a deployment. See `AGENTS.md`
rule 19.

## Layout

```
src/muse/
  main.py            the composition root: settings, the container, create_app()
  api.py             POST /v1/route, the error envelope, auth stub, trace ids
  router.py          request -> ordered candidates -> completion, with fallback
  routes.py          config/routes.yaml and its loader
  providers/         the Provider protocol, the registry, the litellm adapter
  vault.py           AES-256-GCM, one key per provider
  metering.py        the muse.tokens.consumed envelope and its insert
  outbox.py          the publisher skeleton (core's contract, muse's SQL)
  db.py              the Database protocol and the psycopg adapter
  contracts.py       core's patterns, copied and checked for parity
  redaction.py       Secret, and the scrubber for provider text
  errors.py          the error taxonomy and the retry list
  telemetry.py       W3C traceparent, and the span-attribute allowlist
  breaker.py         the per-provider circuit breaker
migrations/          outbox_events, then vault_secrets
config/routes.yaml   the routing table
openapi/v1.yaml      the committed HTTP contract
```

## Tests

```sh
bin/prime                       # the gate: sync, lint, 100% branch coverage
uv run pytest                   # the suite and the coverage gate
uv run pytest -m unit           # skip anything marked integration
uv run pytest -k vault -vv       # one concern
uv run pytest --cov-report=html  # htmlcov/index.html
```

562 tests, 100% branch coverage. The suite never opens a socket: HTTP is driven in
process over `httpx.ASGITransport`, the database is an in-memory store behind the same
`Database` seam production uses, and litellm is a stand-in module — so the real
adapter's exception mapping is the code under test, not a mock of it. The one
excluded function is `PsycopgDatabase.open()`, which dials.

Two tests skip unless pointed at a core checkout:

```sh
MUSE_CORE_SCHEMAS=../core/schemas uv run pytest
```

They assert that the event patterns copied into `muse/contracts.py` are byte-identical
to core's schema and that `cafaye.yml` validates against core's manifest schema. A
copy is a drift risk, and this is the check that catches it.

### A green gate is a claim about a clean machine

`bin/prime` runs `uv sync --locked` with no extras, so it is only green if the
declared dependency set covers everything the suite imports. A package that is
reachable only through an extra, or only because something else happened to pin it,
makes the suite pass in the venv it was written in and fail on every fresh clone —
which is how `muse-03`'s gate turned red at merge.

So a change to the suite's imports has to be accompanied by a change to
`pyproject.toml` **and** a `uv lock`, and the honest way to check it is to delete the
venv:

```sh
rm -rf .venv && bin/prime
```

`tests/test_dependencies.py` enforces it on every gate run instead, from the
declarations rather than from the venv: every module `src/` and `tests/` import must
be provided by the closure of `[project.dependencies]` plus the dependency groups, and
every third-party root must be a direct declaration. It reads `pyproject.toml`,
`uv.lock` and the installed RECORD files — no socket, no network.

### `--locked`, not `--frozen`

The two flags sound interchangeable and are not. `--frozen` means *do not update
`uv.lock`*, so a lock that disagrees with `pyproject.toml` installs silently.
`--locked` means *assert `uv.lock` would not change*, which is what a gate wants.

The gap is not academic. With a lock that predates a `pyproject.toml` edit, `uv sync
--frozen` exited 0 with a partial dependency set, `uv run pytest` exited 0, and
`uv run` quietly **rewrote `uv.lock` on the way there** — a green gate over a lockfile
nobody had committed. So `--locked` goes on every `uv sync` *and* every `uv run` in
`bin/prime` and the Dockerfile; `uv run` re-resolves by default, and one bare `uv run`
undoes a guarded sync.

## License

AGPL-3.0-only — see the cafaye org for the platform's licensing rationale.
