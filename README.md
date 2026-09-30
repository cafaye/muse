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

Routing, the vault, metering, the HTTP surface and **real JWT verification** are done and
tested. Not in this version: streaming, tool calls, embeddings, images, batches, and a
real admin UI.

## Auth

`POST /v1/route` verifies the bearer token against the key set `identity` publishes.
**A valid signature is not an authorization** — the token must also carry the
capability the operation needs and the tenant the request runs as.

| Check | Failure |
| --- | --- |
| RS256 signature, from identity's cached JWKS | 401 |
| `alg: none`, HS256, ES256, or any token naming a key identity does not publish | 401 |
| `iss`, `aud`, `exp`, `nbf` | 401 |
| `sub`, `iat`, `jti`, `account_id` present | 401 |
| `completions:write` in the capability set | **403** |
| `identity`'s key set reachable | **503** |

Two of those deserve their own line.

**A correctly signed token with no scope is a 403.** That is the whole point: "the
signature is valid" and "the caller may do this" are different questions, and an
implementation that checks only the first authorises anybody holding a stale token.

**An unreachable key set is a 503, never a 401.** A 401 tells the caller their
credential is bad, and the problem is that we could not *check* it — so a 401 sends a
caller with a perfectly good token to re-authenticate against a healthy identity and
then retry forever.

> **muse does not serve unauthenticated traffic when identity is down.** The request is
> refused rather than admitted on an unverified credential. The alternative is a service
> whose only protection against a forged token is whether the token's issuer happened to
> be reachable at that moment.

### Two open platform questions

Neither is settled, and both are recorded in `cafaye.yml` for whoever decides.

**The claim's name.** `core/docs/openapi-conventions.md` requires `scopes`; `guard` reads
a space-separated `scope`. `identity` mints **both**, byte-identical, precisely so the
decision can land without breaking an issued token. muse accepts either name, and
**refuses a token carrying both where they disagree** — not merged, not preferred, not
unioned. A token whose two authorisation claims contradict each other is a token muse
does not understand, and guessing which one the issuer meant is how an escalation about
a claim name becomes a cross-tenant read.

**The required scope.** `completions:write` follows core's documented
`resource:action` shape and names muse's own surface, but the exact string is
provisional and this packet adds no scope namespace to core's document.

A token with **no `account_id`** is refused, which is stricter than `guard`: muse bills
and meters per tenant, so a request whose cost cannot be attributed is a request nobody
can charge for. Defaulting to `sub` would make a user id a tenancy key.

### Configuration

| Variable | Default | What it is |
| --- | --- | --- |
| `MUSE_IDENTITY_ISSUER` | `https://identity.cafaye.com` | The only issuer whose tokens are accepted. |
| `MUSE_IDENTITY_AUDIENCE` | `muse` | The `aud` a token must carry to be for this service. |
| `MUSE_JWKS_URL` | `{issuer}/.well-known/jwks.json` | An explicit key-set URL, for a mirror. |
| `MUSE_JWKS_TTL_SECONDS` | `300` | How long a fetched key set is reused. |
| `MUSE_JWKS_REFRESH_SECONDS` | `30` | Floor between two forced refreshes. |

These **default** rather than being required, which is the opposite of `MUSE_VAULT_KEY`
and deliberate: a wrong issuer or audience cannot make a bad token good, so the worst a
wrong default can do is refuse every token. Fail closed, loudly.

### The key set is cached, and the refresh is bounded

Keys are cached by `kid` with a bounded TTL, so `identity` is never on the hot path. An
unknown `kid` triggers **one** refresh — bounded by a minimum interval, because
"refresh on an unknown `kid`" without a bound is an amplification primitive: anyone who
can send a request can send one with a random `kid` and make muse fetch the key set per
request, by an unauthenticated caller, aimed at our own identity service.
`tests/test_auth.py` sends five hundred unknown `kid`s and asserts the fetch count does
not track the request count.

Rotation works in both directions: a token signed by a key published *after* this
process cached the old set is accepted within the refresh interval, and a key withdrawn
from the published set stops being accepted once the TTL expires — the cache is
replaced, never merged, because a merged cache is a set where a withdrawn key never
leaves.

### Nothing about the token is logged

The token is a credential, and a token in a retained log is a credential in a searchable
store. No claim is recorded on a span, no error message carries one, and
`tests/test_auth.py` asserts it with a marker placed inside the token — separately
against the raw compact JWS, so a service that logged `request.headers` fails the
second check.

## Requirements

Python 3.14 and [uv](https://docs.astral.sh/uv/), both pinned in this repo. Postgres
for the vault and the outbox. An OpenAI and/or an Anthropic API key, if you want
anything other than the probes to answer.

```sh
mise install          # reads mise.toml -> python 3.14, uv 0.12.20
```

`identity` is required to serve anything but the probes: muse verifies tokens against a
JWKS it fetches over the network, so `/v1/route` answers 503 without an issuer. The
probes consult nothing and answer either way.

```sh
# If identity is not running on the compose network, the probes still work and the
# endpoint 503s. This is the honest local state, not a gap.
docker compose up --build
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
  -H "Authorization: Bearer $MUSE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}]}'
```

`$MUSE_TOKEN` is a real JWT from `identity`: RS256, carrying `completions:write` and an
`account_id`. Any non-empty string is a 401 now — see [Auth](#auth).

Every non-2xx is `application/problem+json`:

| Code                | Status | When                                                              |
| ------------------- | ------ | ----------------------------------------------------------------- |
| `unauthorized`      | 401    | The credential is absent, malformed, unverified, expired, or has no `account_id`. |
| `forbidden`         | 403    | The token verified and lacks `completions:write`.                  |
| `not_found`         | 404    | No route for the requested model.                                  |
| `validation_failed` | 422    | The body is not a valid route request.                             |
| `unavailable`       | 503    | Every candidate provider failed, **or** identity's keys are unreachable. |
| `internal`          | 500    | muse failed. `trace_id` is the handle.                             |

A 503 means muse is up and something it depends on is not — the models, or `identity` —
which is the one case where retrying the same request is worthwhile.

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
| `joserfc`       | JWT verification against identity's JWKS (packet muse-06). BSD-3-Clause, and its **only** dependency is `cryptography`, which muse already declared — so the whole crypto layer adds one pure-Python package and no tree. Chosen over `PyJWT[crypto]` and `python-jose` for the reason that matters here: `algorithms=[...]` is a parameter of its `decode`, so RS256 is pinned in the call and `alg: none` / HS256 / ES256 are *unrepresentable* rather than unlisted. `python-jose` was rejected as effectively unmaintained. |
| `httpx`         | The JWKS fetch. Was dev-only before muse-06; it was already in the runtime closure through `litellm`, so the image gains nothing, and `tests/test_dependencies.py` would (correctly) refuse an undeclared import. Also the suite's ASGI client. |
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

### Auth

| Variable | Default | What it is |
| --- | --- | --- |
| `MUSE_IDENTITY_ISSUER` | `https://identity.cafaye.com` | The only issuer whose tokens are accepted. |
| `MUSE_IDENTITY_AUDIENCE` | `muse` | The `aud` a token must carry to be for this service. |
| `MUSE_JWKS_URL` | `{issuer}/.well-known/jwks.json` | An explicit key-set URL, for a mirror. |
| `MUSE_JWKS_TTL_SECONDS` | `300` | How long a fetched key set is reused. |
| `MUSE_JWKS_REFRESH_SECONDS` | `30` | Floor between two forced refreshes — the amplification control. |

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
carries `--no-dev`, so the group cannot leak into a deployment. See the `Dependencies`
section of `AGENTS.md`.

## Layout

```
src/muse/
  main.py            the composition root: settings, the container, create_app()
  api.py             POST /v1/route, the error envelope, the bearer check, trace ids
  auth.py            the verifier: pinned algorithm, claims, capability, tenancy
  jwks.py            identity's key set: cached, and refreshed under a bound
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
  errortype.py       the fleet's error classes, and the table onto them
  telemetry.py       W3C traceparent, the span allowlist, and `record_error`
  breaker.py         the per-provider circuit breaker
  schemas/telemetry/ core's traces schema, vendored byte-identically
migrations/          outbox_events, then vault_secrets
config/routes.yaml   the routing table
openapi/v1.yaml      the committed HTTP contract
```

## Errors, and the vocabulary they are reported under

`error.type` is the class a span failed with, and it is **core's** thirteen-value
vocabulary rather than muse's class names — `ProviderAuthError` goes on the wire as
`provider_auth`, so a fleet-wide error view (PLAN §7b: partition by `service.name`,
filter on span status `error`, drill down by `error.type`) is a query rather than a
mapping table somebody maintains by hand.

`muse/errortype.py` reads that vocabulary out of core's `traces.schema.json` rather
than restating it, and holds the one table that maps every class in `muse.errors` onto
it. `muse/telemetry.record_error` is the only path that puts a failure on a span, and
it sets the span's status at the same time — core's schema makes the class and the
failed status a biconditional, and a span carrying one without the other is a span two
queries read differently.

Three things follow, and each has a test:

- **A class with no mapping is refused, not defaulted.** `_OTHER` is a member of the
  vocabulary and `ProviderIndeterminate` is mapped onto it on purpose, but it is never
  a fallback: `dict.get(cls, "_OTHER")` makes forgetting a mapping silent.
- **The request does not fail because telemetry could not classify it.** The refusal is
  caught at the one place where the alternative is a 500 in place of the caller's own
  error, and becomes `_OTHER` plus a log line naming the class. The gate is
  `tests/test_error_vocabulary.py`, which fails on a class in `muse.errors` with no
  entry.
- **`error.message` is still not on the allowlist.** A provider's content-policy
  rejection quotes the offending content back, so a span that records only the class
  cannot carry the text even if redaction were removed.

## Tests

```sh
bin/prime                       # the gate: sync, lint, 100% branch coverage
uv run pytest                   # the suite and the coverage gate
uv run pytest -m unit           # skip anything marked integration
uv run pytest -k vault -vv       # one concern
uv run pytest --cov-report=html  # htmlcov/index.html
```

765 tests, 100% branch coverage. The suite never opens a socket: HTTP is driven in
process over `httpx.ASGITransport`, the database is an in-memory store behind the same
`Database` seam production uses, and litellm is a stand-in module — so the real
adapter's exception mapping is the code under test, not a mock of it. The one
excluded function is `PsycopgDatabase.open()`, which dials.

Three tests skip unless pointed at a core checkout:

```sh
MUSE_CORE_SCHEMAS=../core/schemas uv run pytest
```

They assert that the event patterns copied into `muse/contracts.py` are byte-identical
to core's schema, that `cafaye.yml` validates against core's manifest schema, and that
the vendored `src/muse/schemas/telemetry/traces.schema.json` — the file
`muse/errortype.py` reads the error vocabulary out of — is still byte-identical to
core's. A copy is a drift risk, and this is the check that catches it. The tests that
*use* that schema to validate a span muse actually exported are not among them: the copy
ships with the package, so they run on the default gate.

So the counts are not the same number twice: `765` with a core checkout, `763 passed,
2 skipped` without one, both at 100% coverage and both green. Nothing is proved by the
second one.

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

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) calls
[kit](https://github.com/cafaye/kit)'s reusable workflow rather than copying it, and adds
the two jobs kit cannot own:

| Job | What it is | Why it is not in kit |
|-----|-----------|---------------------|
| `python (kit)` | `uses: cafaye/kit/.github/workflows/ci.reusable.yml@master` — install, ruff, the suite, coverage at 100 | Nothing. This is the shared half, and it is a `uses:` and nothing else. |
| `gate + core parity` | `bin/prime` with `MUSE_CORE_SCHEMAS` pointed at a checked-out `cafaye/core`, then a lockfile guard and a no-skip guard | It knows muse's env var and reaches into another cafaye repository. kit cannot own a step that needs either. |
| `pins` | Asserts the runtime pins in the workflow are the ones the repo declares | The pin is written twice, because GitHub exposes no file context to a reusable workflow call's `with:`. |

The second job exists because of the count above. A caller cannot inject `env:` into a
called reusable workflow, so kit's python job runs those two cross-repo drift guards as
**skipped** — and that is why the job that does run them **fails on any skip at all**,
with no allowlist of known-good skips. A CI run that quietly drops the only check that
catches drift with core is worse than no CI, because a green badge is a claim.

That job also asserts `git diff --exit-code -- uv.lock`, and then proves the drift guard
can actually go red: it copies core's schemas, mutates `eventType.pattern` in the copy,
and fails if the parity test still passes. A gate nobody has watched fail is a gate
nobody knows is a gate.

## License

AGPL-3.0-only — see the cafaye org for the platform's licensing rationale.
