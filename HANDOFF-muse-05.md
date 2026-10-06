# HANDOFF — muse-05 (streaming, on every path or none)

**State: three commits on `feat/streaming-01`. Two are finished and green; the third
is NOT finished and must not be merged as though it were.**

The worktree this packet was being built in was deleted mid-run. The work was salvaged
into `ceadd59` by the harness, so the tree is intact, but the harness could not run the
gate over it. **Start by running the gate, not by reading the diff.**

```sh
uv sync --locked
MUSE_CORE_SCHEMAS=/Users/kaka/Code/any/moon/cafaye/core/schemas \
  .venv/bin/python -m pytest -q
```

Redirect to a file and read `$?`. Never pipe a gate into `tail`/`grep` and read the
pipeline's status.

## What is finished, with the run behind it

| Commit | Contents | Last measured |
| --- | --- | --- |
| `9b30e20` | `LiteLLMProvider.stream()`, `StreamChunk`, `Provider` gains a 4th operation | **1077→1057 passed, exit 0, 100% branch**; ruff exit 0; `gate_self_test` exit 0; `gate-check` 0 failures |
| `2c111ef` | `Router.stream`, `StreamResult`, `SettledStream`, the fallback line | **1077 passed, exit 0, 100% branch**; ruff exit 0; `gate_self_test` exit 0; `gate-check` 0 failures |

Both moved `gate.yml`'s `core-parity` floor in the same commit as the suite, measured on
both sides (with a core checkout / `N passed, 4 skipped` without).

## What is unfinished — `ceadd59`

`api.py`'s HTTP surface, `telemetry.detached_span`, the doubles' usage chunks, the
contract additions to `openapi/v1.yaml`, and `tests/test_streaming.py` (23 tests).

**The last full run I saw, before the final three edits, was:**

```
1 failed, 1100 passed — coverage 99.77%
FAILED tests/test_streaming.py::test_a_failed_stream_is_recorded_rather_than_dropped_silently
uncovered: src/muse/api.py 549, 556->exit, 636->exit
```

That failure and those three lines are what my final three edits were **trying** to fix,
and **they were never re-run** — the `.venv` vanished mid-edit. So:

- `test_a_failed_stream_is_recorded_rather_than_dropped_silently` should now pass: the
  `logger.warning` was unreachable because the error branch returned before
  `_meter_stream` ran. It now calls `_record_unmetered(...)` directly, and the inline
  `logger.warning` was extracted into that function so there is one definition of what an
  unmetered stream logs. **Unverified.**
- `src/muse/api.py:549` is `return problem(request, "validation_failed", 422, str(error))`
  — the `ValueError` arm in `stream_completion`, for a bad role or empty message on a
  *streamed* request. **There is no test for it.** Add one: post `stream: true` with
  `{"role": "banana"}` and assert 422. This is the most likely remaining red.
- `556->exit` / `636->exit` are branch partials around `_prepend`. Reaching them may
  need a stream whose settlement arrives with no chunk before it.

### Known gaps, in the order a successor should take them

1. **Run the gate.** Nothing below is worth reading until the actual reds are known.
2. **Add the `ValueError` test** (line 549).
3. **Check the two branch partials** at 556/636.
4. **The contract is not bumped.** `openapi/v1.yaml` still says `version: 1.2.0` and
   still contains the "The one thing 1.2.0 does NOT add is streaming" and "**No
   streaming**, on any path" passages. `stream`/`stream_options` are in the request
   schema, but the version, the changelog entry and those two passages are not updated.
   This is question 4's answer and it is **unwritten**.
5. **`CHANGELOG.md` has no muse-05 entry.** It still starts at muse-04.
6. **`tests/test_alias_delegation.py` has not been extended** for streaming. Its
   `COMPLETION_ENDPOINTS`/`DELEGATED_TO` guards check that both handlers call
   `serve_completion` and neither dispatches or meters directly — which is still true
   and still green. But nothing yet asserts that a *streaming* request goes through the
   same function, and rule 25's whole argument is that the structural guard and the
   behavioural one answer different questions. `tests/test_streaming.py` is the
   behavioural half; the structural half should name `stream_completion` too.
7. **`tests/test_tenant_scoping.py` / `test_openapi.py` path enumeration** were not
   re-checked for a new schema component (`StreamOptions` is defined but may be
   unreferenced — `RouteRequest.stream_options` inlines its own `properties` rather than
   `$ref`-ing it, which may be intentional or may be a duplication worth resolving).

## The four design questions, as answered in the two finished commits

1. **The fallback line is the first chunk, and it is structural.** Two try blocks: the
   first chunk is pulled outside the walk guard so a failure there propagates to the
   chain; everything after it cannot. After the first `yield` there is no code path to
   a second provider — absent, not forbidden. Verified by mutation: collapsing the two
   guards into one turns the fallback tests red.
2. **A stream is metered only if it finished.** `SettledStream` carries a `Completion`
   *or* an `error`, never both. A failed stream's tokens are known and deliberately
   dropped: a completion assembled from partial chunks is a guess, and a guess that gets
   metered gets billed. A caller that stops reading is not a failure — the settlement is
   never reached, the breaker is not told, nothing is billed. An incomplete stream emits
   one `muse.stream.not_metered` warning carrying the error **class**, the route and the
   trace id, never the message or the prompt.
3. **After the headers, an error frame in OpenAI's shape, then `[DONE]`.** An SSE
   response cannot carry a 503 once begun, so the failure goes on the wire as a `data:`
   frame with `error.message`/`error.type`/`error.param`/`error.code` plus `trace_id`,
   followed by `data: [DONE]` so the client ends the stream rather than hanging. The
   **breaker is still told**, so the *next* request avoids the candidate — a provider
   that fails reliably mid-stream is a common way to be unhealthy. Both halves of
   `trips_breaker` are tested on both sides of the line.
4. **Unwritten.** See item 4 above.

## Two defects worth carrying forward

- **Holding an OTel context manager across a `yield` fails.** Starlette iterates a
  streaming body from a different task, so `start_as_current_span`'s detach raises
  `ValueError: <Token ...> was created in a different Context` and turns a successful
  stream into a 500. Hence `Telemetry.detached_span` — a child of the current span that
  never becomes current. The provider *call* is opened inside an ordinary attached span
  (`muse.provider.connect`) because only an attached span is visible to
  `outbound_traceparent`; it closes before the first yield. **Trace propagation on the
  streaming path is therefore asserted only through `muse.provider.connect`; a test that
  the walk's span is a child of it does not exist yet.**
- **`StopAsyncIteration` cannot cross an async generator's boundary** — it becomes
  `RuntimeError: async generator raised StopAsyncIteration`. A provider whose stream
  yields nothing therefore has to be translated to `ProviderUnavailable` *inside* the
  attempt, or the chain 500s instead of falling back.

## What a client can do now

Send `stream: true` on either `POST /v1/route` or `POST /v1/chat/completions` and get
`text/event-stream`: `data:` frames of `chat.completion.chunk`, terminated by
`data: [DONE]`, with a usage chunk before the terminator when
`stream_options.include_usage` is true. A stock OpenAI client consumes it unchanged. The
non-streaming path is byte-for-byte what it was.

## What was deliberately not done

No capability scope, no event type, nothing in `core`. The non-streaming path was not
changed. The retry loop was deliberately *not* reused for streams — one attempt per
candidate, with the chain as the retry — because retrying after a chunk exists would
splice two answers, and `Idempotency-Key` (PLAN §7, still unbuilt) is what would make it
safe. No repo-wide refactor; `test_providers.py`'s `Minimal` gained a `stream` and one
test was renamed, both because the `Provider` protocol grew.