# HANDOFF — muse-alias-01

OpenAI-compatible alias paths: `POST /v1/chat/completions` and `GET /v1/models`.

**Status: the packet's scope is complete and `bin/prime` is green.** What follows is
what a successor should know before starting the next piece of work here, and one
observation that is not mine to decide.

---

## What is done

| Path | What it is |
| --- | --- |
| `POST /v1/chat/completions` | OpenAI's path and response shape, served by `POST /v1/route`'s machinery |
| `GET /v1/models` | OpenAI's model list, read from `config/routes.yaml` in file order |

Both completion endpoints call one function, `muse.api.serve_completion`, which
authenticates → validates → routes → meters. `/v1/route`'s handler is now a thin
renderer over the same result; its request schema, response schema and every status it
can produce are unchanged.

Commits, oldest first:

```
e034c00  test(muse-alias-01): the alias contract, failing
7489a31  feat(muse-alias-01): the OpenAI-compatible aliases, delegating to one path
9bfc4c2  docs(muse-alias-01): the aliases in the house documents
24bc15e  test(muse-alias-01): the delegation itself, read off the source tree
```

`openapi/v1.yaml` is 1.1.1 → **1.2.0**: additive, so a client generated against 1.1.1
keeps working without regeneration. `tests/test_openapi.py` checks the document against
the running app in both directions and still does.

---

## Verified, with output

`bin/prime` — lint plus the full suite at 100% branch coverage:

```
$ bin/prime
...
Required test coverage of 100% reached. Total coverage: 100.00%
======================= 1017 passed, 4 skipped in 30.12s =======================
```

`gate.yml`'s `core-parity` floor moved 973 → **1021** in the same commit as the tests
that moved it, measured rather than counted (`1017 + 4`).

### Both halves of the guarantee, and what each cannot see

The delegation is a *structural* claim and the behaviour is an *observable* one, so each
is asserted in the file that can actually see it:

- **`tests/test_alias.py`** (32 tests) — the alias behaves like `/v1/route`: both call
  the provider exactly once, send it the same request, emit exactly one
  `muse.tokens.consumed` row, fall through the same chain, and produce identical 401 /
  403 / 404 / 422 / 503. Most of these are written as *comparisons* against `/v1/route`
  rather than as assertions about the alias alone.
- **`tests/test_alias_delegation.py`** (11 tests) — parses `src/muse/api.py` and asserts
  both handlers call `serve_completion`, that **neither** holds a dispatch or a metering
  call of its own, that metering lives in the shared path, and that `require_bearer` is
  ordered before the body is read.

Either file alone is satisfiable by a wrong implementation: the behavioural one passes
against a handler that copied the routing and matched, and the structural one cannot see
work done through a subclass. That limit is stated in the file.

### Guards broken on purpose, and watched go red

Rule 23's discipline, applied to every new guard:

| Breakage | Went red |
| --- | --- |
| `/v1/models` renamed in `openapi/v1.yaml` | 4 tests, `test_openapi.py` |
| the alias given a second private `router.route` call | 3 counting tests, `test_alias.py` |
| alias dispatches inline **and** still delegates | `test_the_completion_endpoints_hold_no_dispatch_of_their_own` |
| alias metering for itself | `test_the_shared_path_is_where_the_metering_happens` |
| model list hardcoded to `("fast", "smart")` | `test_the_model_list_is_sourced_from_the_route_table` |
| model list's bearer check removed | `test_the_model_list_authenticates_the_same_way` |

The second row is the one worth keeping: that breakage leaves the alias's JSON perfectly
correct and still fails.

---

## Decisions a successor should not quietly reverse

**The alias body is OpenAI's six fields and nothing else.** `provider`, `route`,
`cost_micros` and `trace_id` are absent. A compatibility surface that grows its own
extras is one every caller has to be taught about, and the generated clients this path
exists for are the strictest readers of it. The trace id still comes back in the
**`X-Trace-Id` header**, which middleware sets on every response regardless of body
shape — so support correlation survives the field's removal from the body.

**`GET /v1/models` ids are route names, not vendor model ids.** `fast` and `smart`, never
`gpt-4o-mini`. This is the same indirection the fallback chain depends on: a caller who
could learn a vendor's model id from the listing could pin a request to that vendor, and
the chain stops being possible.

**`created` is `0` on every entry.** muse has no model-creation timestamp, and inventing
one would be a lie in a machine-readable field. Some clients display it; none may act on
it.

**`GET /v1/models` requires `completions:write`, the same string, unchanged.** A read
tempts `models:read`. That is a new scope namespace and therefore a platform decision —
recorded in `cafaye.yml`, and **not** one a compatibility packet makes. Independently of
the naming: a model list names every route and through them every vendor the deployment
talks to, so it is reconnaissance rather than harmless public information. It also
matches core's convention of `security` applied globally, so an open listing would need
a per-operation override to exist at all.

**`temperature` as a JSON number is a 422 on the alias**, exactly as on `/v1/route`.
OpenAI's own clients send that field as a number, so this is the field most likely to
surprise somebody. It is refused rather than coerced for the reason `openapi/v1.yaml`
states at length: a float cannot be round-tripped, and `0.1 + 0.2` reaches the provider
as `0.30000000000000004`. Coercing on the compatibility path would put that exact defect
back where it is least likely to be noticed. **If this proves to be the alias's biggest
usability cost in practice, the fix is a decision to revisit 1.1.1 fleet-wide, not a
local coercion here.**

**The alias is `ACCOUNT` in the tenant enumeration, not `PLATFORM`.** It reaches the
same `Meter.record`, so it inherits the tenancy establishment and the D9 gap. A second
body is not a second surface — but the path *is* new, so `test_tenant_scoping.py`'s
enumeration moved 12 → 14 and its tenant axis 3 → 4, next to a negative test for it. A
future packet should not collapse that distinction by filing a body-only variant
`PLATFORM`.

---

## Deliberately not done

**No streaming.** Out of scope by the brief, and a separate packet. A body carrying
`stream` is a **422 naming the field**, not a silently non-streaming answer —
`RouteRequestBody`'s `extra="forbid"` already did this and the packet says it out loud
rather than changing it. A caller who asked for a stream and received a whole response
has no way to tell what happened, which is the whole reason it is worth a 422.

**No `/v1/audio/*`.** Blocked on the platform-wide ruling about capability scope names.
Touching them would create exactly the cross-repo drift this fleet has been bitten by.

**No tool calls, embeddings, images or batches.** All still out of scope per the README,
and none of them follow from an alias path.

**No changes to authentication semantics, the scope string, the metering payload, or any
existing behaviour.** The packet adds reach and changes no rule. Verified by the fact
that `/v1/route`'s handler still returns byte-identical bodies for the same inputs —
the existing suite asserts exact JSON shapes (rule 4) and did not need a single loosened
assertion.

**`gate-check` was not run.** There is no `cafaye/core` checkout at `../core` on this
machine, so `../core/harness/bin/gate-check .` and `--prove .` could not run, and
neither could `bash tests/gate_self_test.sh` in its full form (it reads `gate.yml`'s
floor, which I did change — see below). The `core-parity` floor is asserted against the
measured suite size and nothing else was touched in `gate.yml`.

---

## What a successor should run first

```sh
rm -rf .venv && bin/prime
```

That is the honest next command, and the `rm -rf` is not optional: the README is explicit
that a green gate is a claim about a *clean* machine, and this worktree's venv was built
by me rather than by a cold checkout. The alias packet adds no dependencies, so
`test_dependencies.py` should be indifferent — but it is the check that would say so, and
it reads the declarations rather than the venv.

(The suite grew by one test after I first ran the gate — a check that core's sync rule
makes 1.2.0 the right bump for an additive change — so the committed floor is **1022**
against a final `1018 passed, 4 skipped`. Both are in the tree; the arithmetic is
`1018 + 4`.)

Then, on a machine with a core checkout:

```sh
MUSE_CORE_SCHEMAS=../core/schemas uv run pytest
bash tests/gate_self_test.sh
../core/harness/bin/gate-check .
../core/harness/bin/gate-check --prove .
```

**The `core-parity` floor needs verifying against a real core tier.** I raised it
973 → 1021 from `1017 passed, 4 skipped` plus the four known skips, which is the
arithmetic every previous raise used. It has *not* been observed green with
`MUSE_CORE_SCHEMAS` set, and one pre-existing failure is expected there:
`test_the_vendored_schema_is_byte_identical_to_core`, which fails on clean `master`
too (reported in `REPORT-gateway-rename-muse-01.md`, and re-vendoring core's schema is
core's call rather than a rename's side effect). If the core tier reports a different
total, that floor is the number to correct.

`tests/gate_self_test.sh` also derives a stub count from the suite, so it may need the
same adjustment — I could not run it to find out.

---

## If work continues here, the next pieces are

1. **Streaming** (`stream: true` → SSE on the alias). A real packet: it touches the
   router's response type, the metering timing (a completion's token counts are only
   known at the end of a stream, so a mid-stream failure is `ProviderIndeterminate`
   again), and the breaker. Worth reading `muse.errors.ProviderIndeterminate` first —
   a stream that dies halfway is the case that class exists for, and billing it wrongly
   in either direction is worse than the current 422.
2. **`n > 1`** (multiple choices). Small, and the router already returns one candidate's
   completion, so it is a dispatch question rather than a contract one. `n` is currently
   a 422 naming the field, which is the honest interim state.
3. **Deciding whether `temperature` should accept a number.** Deliberately left alone
   here; see above. It is a fleet-wide question about 1.1.1, not a muse-local one.
