# AGENTS.md — muse

Conventions for agents and humans working in this repo. `PLAN.md` (§1, §3) is
the binding house document; this file records what is specific to muse.

## What this service is

muse = LLM routing + credentials vault + token metering, for cafaye, Phase 4.
It embeds LiteLLM (`../../refs/litellm`) as a **library** through its Python API.
None of LiteLLM's code is copied; the adapter is `src/muse/providers/`.

**v1 (packet `muse-02`) is the core**: provider adapters, the router, the encrypted
vault, metering into the outbox, and `POST /v1/route`. Streaming, tool calls,
embeddings, images, batches and a real admin UI are *out of scope* — if you find
yourself adding one, you are past the packet boundary without a new brief.

Packet `muse-03` added the resilience and observability layer around that core:
W3C trace propagation, OTel spans, bounded retry budgets, and per-provider circuit
breakers (PLAN §7). Streaming, tool calls, embeddings, images and batches are still
out of scope — and so is `Idempotency-Key`, which PLAN §7 assigned to `muse-02` and
which is **not built**; `muse.errors.ProviderIndeterminate` documents the billing
consequence of its absence.

## Layout

```
src/muse/main.py      the composition root: settings, Container, create_app()
src/muse/api.py       POST /v1/route, the error envelope, auth stub, trace ids
src/muse/router.py    request -> ordered candidates -> completion
src/muse/routes.py    config/routes.yaml and its loader
src/muse/providers/   the Provider protocol, registry, litellm adapter, doubles
src/muse/vault.py     AES-256-GCM, one credential per provider
src/muse/metering.py  the muse.tokens.consumed envelope and its insert
src/muse/outbox.py    the publisher loop
src/muse/db.py        the Database protocol and the psycopg adapter
src/muse/contracts.py core's patterns, copied and checked for parity
src/muse/redaction.py Secret, and the scrubber for provider text
src/muse/errors.py    the error taxonomy and the retry list
src/muse/telemetry.py W3C traceparent, and the span-attribute allowlist
src/muse/breaker.py   the per-provider circuit breaker
migrations/           00001_outbox_events, 00002_vault_secrets
config/routes.yaml    the routing table
openapi/v1.yaml       the committed HTTP contract
tests/                pytest; one module per concern, plus tests/support/
                     (test_dependencies.py checks the suite's imports against
                     the declared set, and bin/prime against --locked — see
                     Dependencies)
bin/prime             the gate: uv sync --locked && ruff && pytest
```

## Rules

1. **The app is a factory, never a module-level singleton.** `create_app()`
   returns a new `FastAPI` per call and is what tests and the ASGI server use.
   A shared global leaks routes and state between tests and makes failures
   order-dependent — `tests/test_app_factory.py` exists to catch exactly that,
   and `Container` is frozen for the same reason: it is read by every concurrent
   request.
2. **Tests first, and they must fail before they pass.** Write the test, run
   it, watch it fail for the right reason, then implement. A test that never
   failed is a test that proves nothing (PLAN §3.1).
3. **No socket in tests.** Drive the app over `httpx.ASGITransport`, the
   database through an in-memory `Database`, litellm through an injected
   stand-in module, and OpenTelemetry through `InMemorySpanExporter` (see
   `tests/support/tracing.py`, which swaps the SDK's exporter in for the OTLP
   one so a test reads the payload a collector would really receive). The suite
   is fast, hermetic, and parallel-safe.
   `TestClient`/live-`uvicorn` is for the rare case that genuinely needs a real
   server. Two tests skip unless `MUSE_CORE_SCHEMAS` points at a core checkout;
   they say so rather than passing quietly.
4. **Assert exact JSON shapes, not field presence.** `assert body == {...}` for
   every response. `in` checks let a stray new key slip through and break the
   contract that `guard` and the generated SDKs depend on.
5. **`/healthz` checks nothing; `/readyz` checks everything.** Liveness must not
   consult a dependency or an outage becomes a restart loop. Readiness reports
   per-dependency status and is the only place allowed to fail on a dependency.
6. **`/readyz` keeps its `checks` map.** The `db` slot is filled in now that the
   vault exists: `ok`, or `error` with status `degraded`. The shape did not
   change, which is what "later packets fill the value in" meant. `degraded`
   and not `unavailable`, because a route whose provider needs no database read
   can still be served and restarting the container would achieve nothing.
7. **Coverage gate is 100%** (`--cov-fail-under=100`, branch coverage on). This
   repo is small enough that "uncovered line" always means "unfinished thought".
   If a genuinely untestable line appears, `# pragma: no cover` plus a comment
   saying why — a lowered threshold is not an option. The one exclusion is
   `PsycopgDatabase.open()`, which dials.
8. **Mark tests** `unit` or `integration` (`pytestmark`). Markers are
   `--strict-markers`, so a typo fails the run rather than silently doing nothing.
9. **Money is integer micro-dollars, everywhere.** No floats past
   `LiteLLMProvider.cost_per_1k_tokens`, which converts once on the way in and
   rounds *up* so a sub-micro-dollar price never becomes zero. A float that
   rounds differently on two machines is a reconciliation bug nobody can
   reproduce.
10. **A secret is a `Secret`, from the vault to the provider call.** Not a `str`
    with a comment next to it. `repr`, `str` and `__format__` are the marker, so
    an accidental `%s` cannot leak a key, and provider error text is scrubbed
    with `redact()` before it becomes an exception a caller and a log both see.
    `MUSE_VAULT_KEY` has no default anywhere, and a missing one is a boot
    failure — a vault that starts with a fallback key is a vault whose keys are
    readable by anyone who has read this repository.
11. **The price is resolved before the call is dispatched.** A model muse cannot
    price is never called, because a completion that cannot be metered is one
    whose spend is never billed. And a metering failure is not swallowed: the
    completion was paid for, so returning it without its record would lose the
    spend silently.
12. **Only transient failures are retried.** `RETRYABLE_PROVIDER_ERRORS` is an
    explicit tuple, not a walk of the class hierarchy, so adding a
    `ProviderError` subclass does not silently change retry behaviour. A
    rejected credential or a malformed request advances to the next candidate
    immediately. `is_retryable` tests the **exact type**, so `CircuitOpen` — a
    `ProviderUnavailable` subclass — is not retried: a held-back provider is the
    one case where a retry is least useful.
13. **An ambiguous outcome is not a transient failure.** `ProviderIndeterminate`
    means the request was dispatched and the answer did not arrive, so the
    vendor may already have billed it. It is not in the retry tuple and a route
    must opt in with `retry_indeterminate: true`. A 408 *is* retryable — that
    is the server saying the request never arrived complete. Until muse has an
    `Idempotency-Key` (PLAN §7 assigned it to muse; it is not built), one
    attempt is the only answer that cannot bill a customer twice.
14. **No sleeps, no retry bumps, no loosened assertions** to make a flaky test
    pass (PLAN §3 flake policy). Where a policy genuinely sleeps — the router's
    backoff, the publisher's poll, the breaker's reset window, the retry budget
    — the sleep, the **clock** and the **jitter source** are all injected, so
    the suite asserts on the schedule the policy asked for instead of measuring
    elapsed time. `Router(clock=..., unit=...)` is the whole technique.
    Attribute a flake first: failing test, can the diff reach that
    surface, clean-HEAD baseline. Then fix the cause.
15. **The committed contract is checked against the code.**
    `tests/test_openapi.py` asserts `openapi/v1.yaml` and the running app agree
    in both directions, and `cafaye.yml` against core's manifest schema. A
    contract nobody checks is documentation; a contract that is checked is the
    thing the SDKs are generated from. For *headers* that means driving the real
    app and reading the real response: comparing the document to the code cannot
    catch a header the middleware forgets to send.
16. **A span may not carry a caller's words.** `muse.telemetry.record()` is the
    only path from a value to a span, and it refuses anything outside
    `ALLOWED_SPAN_ATTRIBUTES` — model, provider, token counts, cost, latency,
    breaker state, and the error **class**. Never a prompt, a completion, a
    header value, or caller-supplied text. `error.message` is not on the list and
    must not be added: a vendor's content-policy rejection quotes the offending
    content back. A `Secret` is refused on its *type*, never compared by value,
    because a filter with a bypass is what `redaction.py` exists to argue
    against. This is an allowlist rather than discipline at each call site
    because the realistic leak is a well-meaning `muse.prompt` added in six
    months, not an attacker — and `tests/test_trace_propagation.py`'s canary test
    is the thing that catches it.
17. **An open circuit breaker is per provider, not per request.** It refuses the
    provider before any I/O — a refusal that never becomes a socket, which
    `tests/test_breaker.py` asserts at the litellm seam rather than on the state.
    The request still succeeds via the fallback. Only *transient* failures trip
    it: a 400 or a 401 is muse sending something the provider is right to
    refuse, and counting it would let one bad caller take the route down for
    everybody. A success resets the count, so the threshold is over consecutive
    failures.
18. **Resilience config degrades, it does not refuse to boot.** Missing or
    nonsense values fall back to the documented default
    (`MUSE_BREAKER_THRESHOLD`, `MUSE_BREAKER_RESET_SECONDS`). This is the
    opposite of `MUSE_VAULT_KEY` in rule 10, and the difference is the point: a
    resilience knob's default is *safe*, so degrading costs a little load during
    an incident, whereas a vault key's default is *dangerous*, so there is no
    default to degrade to. A setting whose fallback is safe falls back; one
    whose fallback is not refuses to start.

## Dependencies

**Machine-wide supply-chain quarantine.** `~/.config/uv/uv.toml` sets
`exclude-newer = "7 days"`, so uv refuses any release published in the last
week. Do not edit that file — it is deliberate and global.

Consequence: `pyproject.toml` floors are the newest releases that *satisfy the
quarantine*, which is often a version or two behind the true latest. So:

- To add a dependency, ask for the newest version older than 7 days, not the
  newest version on PyPI. `uv lock` will tell you when you guessed wrong.
- `uv.lock` is committed. Run `uv lock && uv sync`; never hand-edit it.
- Bumping past the quarantine window is the user's call, not the worker's.
- Each addition needs a reason in the commit message and a row in the README's
  dependency table.
- **A test may only import what the dev group installs.** `bin/prime` is
  `uv sync --locked` with no extras, so the set of packages the gate installs is
  the closure of `[project.dependencies]` plus every dependency group — and
  nothing else. An import from an extra is green in the venv that once ran
  `uv sync --extra otel` and red on every fresh clone; that is exactly how
  `muse-03`'s gate turned red at merge, because the suite tested
  `build_provider(endpoint=...)` and the OTLP exporter lived in the `otel` extra.
  A test that needs a package makes it a test dependency: the dev group asks for
  the extra (`muse[otel]`) rather than repeating its floor, because two floors in
  two places is a floor that drifts. Transitive availability is not a declaration
  either — `pydantic` and `starlette` were imported at module scope while
  arriving only through fastapi's pins.
  The flip side is that the dev group is *not* the image's dependency set: the
  Dockerfile installs with `--no-dev`, so what the suite needs and what a
  deployment ships are different sets on purpose, and neither test covers the
  other.
  `tests/test_dependencies.py` enforces all of it on every gate run, from
  `pyproject.toml`, `uv.lock` and the installed RECORD files rather than from the
  current venv, which is what lets it fail in a dirty environment. When it fires,
  fix the declaration; do not skip the test that needed the package.
- **`--locked`, not `--frozen`, everywhere in the gate and the Dockerfile.**
  They sound interchangeable and are not. `--frozen` means *do not update the
  lock*, which is exactly what lets a lockfile that disagrees with
  `pyproject.toml` install silently; `--locked` means *assert the lock would not
  change*. `muse-03b` found `bin/prime` running `--frozen` under a comment
  claiming the opposite, and the consequence was worse than a red gate: with a
  stale lock, `uv sync` exited 0, `uv run pytest` exited 0, and `uv run` quietly
  **rewrote `uv.lock` on the way there** — a green gate over a lockfile nobody
  had committed. Because `uv run` re-resolves by default, `--locked` belongs on
  every `uv run` too: one bare `uv run` undoes a guarded `uv sync`.
  This is the failure mode that `rm -rf .venv && bin/prime` *cannot* catch,
  because a clean checkout has a correct lock. `tests/test_dependencies.py` asserts
  the script itself, so the gate cannot quietly lose the assertion.
- **A guard that can pass by finding nothing is not a guard.** Every assertion in
  `tests/test_dependencies.py` is a set-difference over a helper's result, so a
  helper returning an empty set makes all of them pass — and breaking the import
  walk, the declared set, the closure or the RECORD walk in turn did exactly that,
  leaving two of the guards green. This is the canary test's own discipline from
  `tests/test_trace_propagation.py`: "the canary is absent" is also true of an
  export that produced no spans, which is why it asserts `names(exporter)` first.
  Assert the floor before asserting an absence, and when adding a guard, break it
  on purpose and watch it go red.

## Toolchain

`mise.toml` pins `python = "3.14"` and `uv = "0.12.20"`; `.python-version`
pins 3.14. If a new interpreter release lands, bump `.python-version` and
`mise.toml` together and re-run the gate.

## Commands

```sh
bin/prime                              # the gate
rm -rf .venv && bin/prime              # the gate on a clean machine — see Dependencies
uv run pytest                          # tests + coverage gate
uv run pytest -m unit                  # only unit-marked
uv run pytest -k vault -vv             # one concern
uv run ruff check . && uv run ruff format .
uv run uvicorn --factory muse.main:create_app --reload   # local dev
docker compose up --build

# The two contract tests that need a core checkout:
MUSE_CORE_SCHEMAS=../core/schemas uv run pytest
```

Long commands get a `timeout`. Never push — the manager merges to `master`.
