# HANDOFF-muse-tgc3 — the redaction canary, fleet-wide and at execution time

**Status: muse is DONE and verified. identity, courier and billing are NOT touched
(not my repositories). Everything a successor needs to dispatch them is below —
measurement, exact file, exact missing assertion.**

## What the packet was for

One shared deployment means one tenant's prompt is a cross-tenant leak. No collector is
deployed anywhere, so the boundary depends on each service's discipline. The standard
shape: a unique string in BOTH the prompt and the completion, the app driven end to end,
the assertion on the RENDERED span payload as one string, asserted both ways (absent
value AND no content-named attribute), plus the second barrier (a provider echoing a key
in its error text is scrubbed and the span records only `error.type`).

## What is done in muse

`tests/test_trace_propagation.py`:

| line | what |
|---|---|
| 74 | `CONTENT_WORDS` — the words that name content rather than shape |
| 653 | `assert_no_content_reached_a_span(exporter, records)` — the shared assertion |
| 713 | `test_no_span_attribute_carries_prompt_or_completion_text` — `/v1/route` |
| 819 | `test_the_openai_alias_carries_no_prompt_or_completion_text` — `/v1/chat/completions` |
| 860 | `test_a_content_policy_rejection_records_the_class_and_not_the_quoted_text` |

Measured gaps it closed (all three were real, none was invented):

1. **The attribute-name half covered two span names by hand.** The old loop read
   `by_name(exporter, "muse.provider.call") + by_name(exporter, "muse.route")`, so
   `muse.request` — the span the middleware opens at `src/muse/main.py:440` before any
   route is chosen — had names nobody checked. Now every span in the export is walked.
   *Proved load-bearing:* `span.set_attribute("http.request.body.size", 3)` in the
   middleware makes both canaries fail, and the old two-span loop passes over it.
2. **Only `/v1/route` was driven.** The alias reaches the same `serve_completion`
   (`src/muse/api.py:391`), but a guarantee asserted on one path is a guarantee about one
   path. A second canary drives `POST /v1/chat/completions` through the same helper, so
   the two entry points hold to one assertion rather than two that happen to agree.
3. **Nothing held prompt or completion text out of a log record.** The credential canary
   at `tests/test_auth.py:877` already did; content is no different in kind.

The second barrier already existed and was left alone:
`test_a_provider_echoing_the_credential_is_not_recorded_on_a_span`
(`tests/test_trace_propagation.py:748`) drives the real `LiteLLMProvider` against the
litellm stub, asserts the vendor's echo of `sk-live-XYZ` is scrubbed from the response
body and from `rendered(exporter)`, and that the span carries only
`error.type == "provider_auth"`.

Both halves were broken on purpose and watched go red (AGENTS.md rule 23), then
reverted — the tree carries no source change:

- `span.set_attribute("http.request.body.size", 3)` in `main.py` → both canaries red on
  the NAME half; the pre-change two-span loop stays green.
- `span.set_attribute("muse.prompt", ...)` on the route span, bypassing `record()` the
  way a debugging change would → both canaries red on the VALUE half, which is the leak
  the allowlist cannot see.

`gate.yml`'s `core-parity` floor was raised 1037 → 1039 in the same commit, measured on
both sides rather than counted: `uv run pytest` → `1035 passed, 4 skipped`;
`MUSE_CORE_SCHEMAS=<core>/schemas uv run pytest` → `1039 passed`, exit 0.

## Verification commands and their real output

```
$ uv run pytest tests/test_trace_propagation.py -q --no-cov
.....................                                                    [100%]
21 passed in 0.24s
exit: 0

$ mise run prime
...
TOTAL                                1851      0    382      0   100%
Required test coverage of 100% reached. Total coverage: 100.00%
======================= 1035 passed, 4 skipped in 33.64s =======================
exit: 0

$ MUSE_CORE_SCHEMAS=<core-tgb1>/schemas uv run pytest -q --no-cov -rs
1039 passed in 26.02s
exit: 0

$ bash /Users/kaka/Code/any/moon/cafaye/core/harness/bin/tenancy-check .
OK /Users/kaka/Code/any/moon/wt/muse-tgc3: 0 failure(s), 2 warning(s) — warnings do not move the exit code

$ bash <core>/harness/bin/gate-check .
OK /Users/kaka/Code/any/moon/wt/muse-tgc3: 0 failure(s), 2 warning(s) — warnings do not move the exit code
```

The two warnings on each are the documented expected ones (tenancy's two
`enumeration-partial`/`scope-key-unused` on 15 sites; gate's two
`requirement-unproven` for `mise install` and `git clone`, which the checker refuses
to run by design).

## The fleet measurement — per exporting service

All four services export spans. All four have a canary. **All four assert on the
rendered payload.** The differences are in the name half, the log half and the
directions the canary is planted in.

### muse — `tests/test_trace_propagation.py`
- Canary exists: **yes** (`:713`, plus `:819` alias, `:860` failure path)
- Rendered payload as one string: **yes** — `rendered()` is `repr(payloads(...))`
  (`tests/support/tracing.py:82`), asserted at `:687`
- Attribute-name half: **yes, and it is the only one asserted over *exported spans*
  rather than over the allowlist constant** (`:697-704`). muse's allowlist-level twin is
  `tests/test_telemetry.py:156`.
- Floor (absence cannot mean "nothing exported"): **yes** — `names(exporter)` at `:685`,
  plus each caller asserting the canary came back in the response body first.
- Log records: **yes** — `caplog` at `:706-710`.
- Directions planted: prompt AND completion (a fake provider echoes it back).
- Entry points driven: `/v1/route` AND `/v1/chat/completions`.

### identity — `wt/identity-tga1/internal/telemetry/canary_test.go`
(identical in `wt/identity-tgc2`; verified `diff -q`)
- Canary exists: **yes** — `assertNoCanary` at `canary_test.go:101`
- Rendered payload: **yes** — `renderExports(recorder)` then `strings.Contains(exported,
  canary)` at `canary_test.go:104`
- Attribute-name half: **PARTIAL.** Present against the allowlist constant
  (`internal/telemetry/telemetry_test.go:49`, words at `:51-57`) and against a planted
  *request* (`canary_test.go:137`), which proves content-bearing NAMES are dropped — but
  no assertion walks the **exported** spans' attribute names. A `span.set_attribute`
  (or any recorder that bypasses `Record`) would carry a content-named attribute past
  both.
- Floor: **yes** — `assertExported` at `canary_test.go:120`, called by every test.
- Log records: **PARTIAL** — `RecordFailure`'s message deliberately stays in the log by
  design (`canary_test.go:330-334`); there is no log-record search in this file. The
  MFA surface has its own separate canary at `internal/httpapi/mfa_logging_canary_test.go`.
- Directions planted: caller-controlled request data only (paths, queries, bodies,
  headers). identity has no LLM provider, so there is no completion to echo.
- Entry points driven: several (`/v1/accounts`, `/v1/introspections`, `/v1/session`).

**Exact missing assertion for identity:** in `canary_test.go`, extend `assertNoCanary`
to also walk `recorder.Ended()`'s span attributes and refuse any NAME containing one of
the words already listed at `telemetry_test.go:51-57`, so a bypass of `Record` cannot
carry a content-named attribute out. One loop, same words, no new fixture.

### courier — `wt/courier-tga2/test/courier/telemetry_canary_test.exs`
- Canary exists: **yes** — `assert_no_canary` at `:336`
- Rendered payload: **yes** — `exported =~ @canary` at `:339`, over
  `rendered_request_spans()`
- Attribute-name half: **PARTIAL, allowlist only** — `test/courier/observability_test.exs:27`
  "no name contains a word that names content", forbidden list at `:29-36`. Nothing
  walks the exported spans' names.
- Floor: **yes, and it is the best-documented in the fleet** — the watermark guard at
  `:375-400`, whose comment records the measurement that all TEN tests in the file
  passed while the exporter was mutated to drop every span.
- Log records: **ABSENT.** No `capture_log` in the canary file (the one hit is
  `observability_test.exs:186`, a different claim).
- Directions planted: caller-controlled request data, plus headers
  (`authorization`, `cookie`, `x-api-key`, `user-agent`) and a malformed `traceparent`.
- Entry points driven: many (`notification_preferences`, `webhook_endpoints`,
  `unsubscribe`, `accounts`, `healthz`, a 404).

**Exact missing assertion for courier:** add the exported-span attribute-NAME walk to
`assert_no_canary/0` (`telemetry_canary_test.exs:336`) using the forbidden list already
written at `observability_test.exs:29-36`, and wrap the request helper so the canary
string is asserted absent from `ExUnit.CaptureLog.capture_log` output for the same
requests.

### billing — `wt/billing-tga4/test/observability/canary_test.rb`
- Canary exists: **yes** — `assert_no_canary` at `:231`
- Rendered payload: **yes** — `exported.include?(CANARY)` at `:234`
- Attribute-name half: **PARTIAL, allowlist only** — `test/observability/allowlist_test.rb:35`,
  words at `:20-30`.
- Floor: **yes** — `TestSupport::TestSpans.rendered!` raises on an empty export
  (`test/support/test_spans.rb:31-41`), so absence cannot mean "nothing exported".
- Log records: **ABSENT** from the canary (`capture_log` appears only in
  `observability/telemetry_test.rb:84`, for startup logging).
- Directions planted: caller-controlled request data, and the strongest of the three on
  the request side — an **entire webhook body** at `:149-163`.
- Entry points driven: many (`customers`, `plans`, `subscriptions`, Stripe webhook,
  `healthz`, a 404).

**Exact missing assertion for billing:** add to `assert_no_canary` (`canary_test.rb:231`)
a walk of each exported span's `attributes.keys` against `FORBIDDEN_WORDS` (already at
`allowlist_test.rb:20-30`, currently private to that file — lift it to a shared
constant), plus a `capture_log` wrapper asserting `CANARY` reaches no log record.

### The one-line summary a successor should keep

muse was the only service asserting attribute names over **exported** spans; all three
others assert only over their allowlist constants. muse's `record()` choke point is the
same shape as theirs (`Record`, `Kit::Telemetry.record/2`), so the gap is identical in
kind: a recorder that bypasses the choke point — a raw `span.set_attribute` — puts a
content-named attribute into the export and nothing notices. That is the assertion all
three need, and it is about six lines each.

## What I deliberately did NOT do, and why

- **No collector, no telemetry wiring change.** The brief forbids it and the assertions
  need none — they read the in-memory exporter a collector would have received.
- **No edits to identity, courier or billing.** Another worker's surface; a defect there
  is a report line, not my patch. Everything above is measurement plus a named file and
  a named assertion.
- **`tests/gate_self_test.sh` was not run.** It applies 13 breakages, each running the
  full gate — roughly ten minutes. My `gate.yml` change is one integer, and it was
  verified the cheap way that actually measures the thing: the floor is exactly the
  count a real run reports (1039), so deleting any one test puts the run under it. The
  script's own discipline (break the guard, watch it go red) was applied to the two
  canary halves instead, which is the part of this packet that can fail.
- **No test was renamed.** `pytest -k canary` collects nothing in muse, because muse's
  canary tests are named for the *property* they hold. Renaming would move line numbers
  three of this packet's cross-references depend on, and a `-k` spelling is a
  discoverability problem, not a correctness one. Recorded here so the next reader does
  not conclude the canary is missing from an empty `-k` run.
- **AGENTS.md's stale suite counts (969 / 973) were left alone.** They predate this
  packet — `gate.yml` carries 1035 / 1039 — and correcting a house document's history is
  not this packet's surface. Noted for the manager.
