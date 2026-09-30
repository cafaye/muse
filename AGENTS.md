# AGENTS.md — muse

Conventions for agents and humans working in this repo. `PLAN.md` (§1, §3) is
the binding house document; this file records what is specific to muse.

## What this service is

muse = LLM routing + credentials vault + token metering, for cafaye, Phase 4.
It embeds LiteLLM (`../../refs/litellm`) as a **library**. The router, vault,
and metering are separate packets — do not start them here.

**v0 scope is the scaffold only**: app factory, two probe routes, tests,
container build. If you find yourself adding a provider adapter, an LLM call, or
a credential store, you are past the packet boundary.

## Layout

```
src/muse/main.py   create_app() + /healthz + /readyz. Nothing else yet.
tests/             pytest; one module per concern
bin/prime          the gate: uv sync && uv run pytest
```

## Rules

1. **The app is a factory, never a module-level singleton.** `create_app()`
   returns a new `FastAPI` per call and is what tests and the ASGI server use.
   A shared global leaks routes and state between tests and makes failures
   order-dependent — `tests/test_app_factory.py` exists to catch exactly that.
2. **Tests first, and they must fail before they pass.** Write the test, run
   it, watch it fail for the right reason, then implement. A test that never
   failed is a test that proves nothing (PLAN §3.1).
3. **No socket in tests.** Drive the app over `httpx.ASGITransport`. The suite
   is fast, hermetic, and parallel-safe. `TestClient`/live-`uvicorn` is for the
   rare case that genuinely needs a real server.
4. **Assert exact JSON shapes, not field presence.** `assert body == {...}` for
   probe payloads. `in` checks let a stray new key slip through and break the
   contract that `guard` and the orchestrator depend on.
5. **`/healthz` checks nothing; `/readyz` checks everything.** Liveness must not
   consult a dependency or an outage becomes a restart loop. Readiness reports
   per-dependency status and is the only place allowed to fail on a dependency.
6. **`/readyz` keeps its `checks` map.** The `db` slot is reserved for the
   vault store. Later packets fill the value in; they do not reshape the
   response. The shape is a contract with `guard` and with dashboards.
7. **Coverage gate is 100%** (`--cov-fail-under=100`, branch coverage on). This
   repo is small enough that "uncovered line" always means "unfinished thought".
   If a genuinely untestable line appears, `# pragma: no cover` plus a comment
   saying why — a lowered threshold is not an option.
8. **Mark tests** `unit` or `integration` (`pytestmark`). Markers are
   `--strict-markers`, so a typo fails the run rather than silently doing nothing.
9. **No LLM calls, no provider adapters, no vault, no heavy deps.** No
   langchain, no torch. LiteLLM arrives with the router packet, not before.
10. **No sleeps, no retry bumps, no loosened assertions** to make a flaky test
    pass (PLAN §3 flake policy). Attribute the flake first: failing test, can
    the diff reach that surface, clean-HEAD baseline. Then fix the cause.

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
- Only `fastapi` + `uvicorn` at runtime; dev group is pytest, httpx, anyio,
  pytest-cov. Each addition needs a reason in the commit message.

## Toolchain

`mise.toml` pins `python = "3.14"` and `uv = "0.12.20"`; `.python-version`
pins 3.14. If a new interpreter release lands, bump `.python-version` and
`mise.toml` together and re-run the gate.

## Commands

```sh
bin/prime                              # the gate
uv run pytest                          # tests + coverage gate
uv run pytest -m unit                  # only unit-marked
uv run pytest -k readyz -vv            # one concern
uv run ruff check . && uv run ruff format .
uv run uvicorn --factory muse.main:create_app --reload   # local dev
docker compose up --build
```

Long commands get a `timeout`. Never push — the manager merges to `master`.
