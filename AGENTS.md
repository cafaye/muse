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
migrations/           00001_outbox_events, 00002_vault_secrets
config/routes.yaml    the routing table
openapi/v1.yaml       the committed HTTP contract
tests/                pytest; one module per concern, plus tests/support/
bin/prime             the gate: uv sync && ruff && pytest
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
   database through an in-memory `Database`, and litellm through an injected
   stand-in module. The suite is fast, hermetic, and parallel-safe.
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
    immediately.
13. **No sleeps, no retry bumps, no loosened assertions** to make a flaky test
    pass (PLAN §3 flake policy). Where a policy genuinely sleeps — the router's
    backoff, the publisher's poll — the sleep is *injected*, so the suite
    asserts on the durations the policy asked for instead of measuring elapsed
    time. Attribute a flake first: failing test, can the diff reach that
    surface, clean-HEAD baseline. Then fix the cause.
14. **The committed contract is checked against the code.**
    `tests/test_openapi.py` asserts `openapi/v1.yaml` and the running app agree
    in both directions, and `cafaye.yml` against core's manifest schema. A
    contract nobody checks is documentation; a contract that is checked is the
    thing the SDKs are generated from.

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

## Toolchain

`mise.toml` pins `python = "3.14"` and `uv = "0.12.20"`; `.python-version`
pins 3.14. If a new interpreter release lands, bump `.python-version` and
`mise.toml` together and re-run the gate.

## Commands

```sh
bin/prime                              # the gate
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
