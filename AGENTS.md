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

Packet `muse-06` made the bearer token real. `require_bearer` used to count the
header; it now verifies the signature against identity's published JWKS with the
algorithm **pinned in the call**, checks the issuer, audience, expiry and every
required claim, requires the capability the operation needs, and refuses a token with
no `account_id`. **A valid signature is not an authorization** — a correctly signed
token carrying no scope is a 403.

## Layout

```
src/muse/main.py      the composition root: settings, Container, create_app()
src/muse/api.py       POST /v1/route, the error envelope, the bearer check, trace ids
src/muse/auth.py      the verifier: pinned algorithm, claims, capability, tenancy
src/muse/jwks.py      identity's key set: cached, and refreshed under a bound
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
src/muse/errortype.py the fleet's error classes, and the table onto them
src/muse/telemetry.py W3C traceparent, the span allowlist, and `record_error`
src/muse/breaker.py   the per-provider circuit breaker
src/muse/schemas/     core's telemetry schemas, vendored byte-identically
migrations/           00001_outbox_events, 00002_vault_secrets
config/routes.yaml    the routing table
openapi/v1.yaml       the committed HTTP contract
docker-compose.yml    the local stack: the service, and postgres:17-alpine
tests/                pytest; one module per concern, plus tests/support/
                     (test_compose.py reads the compose file — see rule 23;
                     test_dependencies.py checks the suite's imports against
                     the declared set, and bin/prime against --locked — see
                     Dependencies; test_error_vocabulary.py checks every
                     error class against core's vocabulary — see rule 19;
                     test_auth.py is the bearer contract and test_jwks.py the
                     key-set cache — see rule 20;
                     test_tenant_scoping.py enumerates the 12 account-scoped
                     entry points and holds 3 of them to a tenant axis — see
                     rule 24, and note the `test_*.py` naming it depends on)
bin/prime             the gate: uv sync --locked && ruff && pytest
gate.yml              the gate, declared: the command, the mise task, what it
                      needs from the machine, and the lines it must print
tests/gate_self_test.sh  the proof that gate.yml can fail — 13 breakages
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
   server. Three tests skip unless `MUSE_CORE_SCHEMAS` points at a core checkout;
   they say so rather than passing quietly. (The tests that validate a span
   against core's telemetry schema are not among them: that schema ships inside
   the package, so only its *byte-identity* with core needs a checkout.)
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
19. **An error's class and its name are not the same thing.** What goes on a
    span as `error.type` is core's thirteen-value vocabulary, not
    `type(error).__name__`: `ProviderAuthError` is reported as `provider_auth`.
    The reason is the fleet-wide error view (PLAN §7b) — partition by
    `service.name`, filter on span status `error`, drill down by `error.type` —
    which is a query when every service draws from one list and a mapping table
    somebody maintains by hand when they do not.
    Three rules hold, and each has a test in
    `tests/test_error_vocabulary.py`:
    - **The vocabulary is loaded, not retyped.** `muse.errortype` reads the enum
      out of core's `traces.schema.json`, vendored under `src/muse/schemas/`.
      A Python list of thirteen strings is a second source of truth, and core
      would change the enum while muse carried on emitting a value the schema
      had stopped accepting. The vendored copy is asserted **byte-identical** to
      core's when `MUSE_CORE_SCHEMAS` points at a checkout — a copy with no check
      is a copy that rots, and that lesson has now cost this fleet three times.
    - **`_OTHER` is reachable and is not a default.** `ProviderIndeterminate`
      is mapped onto it deliberately, because it is the case that exists so
      `_OTHER` is not a dumping ground. `dict.get(cls, "_OTHER")` is the wrong
      shape: it makes a forgotten mapping silent. `error_type()` raises
      `UnclassifiedError` instead.
    - **A class and a failed status are one act.** `telemetry.record_error` is
      the only way to put a failure on a span, and it sets both halves, because
      core's schema makes them a biconditional. A span carrying a class while
      claiming to have succeeded is a span two queries read differently, and
      the fleet view filters on the status.
    A class with no mapping must not take a request down with it, so
    `record_error` catches the refusal, records `_OTHER` and logs the class
    name — a symbol, never the message — while the gate is what actually stops
    one shipping.

20. **A valid signature is not an authorization.** `muse.auth.require_bearer` checks
   the header, then the signature, then the claims, then the capability, then the
   tenant — in that order, because each step is cheaper than the next and refusing
   early is what keeps a malformed token from costing a key fetch. Five properties are
   load-bearing, and each has a test named after the failure it prevents:
   - **The algorithm is pinned in the call, not checked afterwards.**
     `joserfc`'s `jwt.decode(..., algorithms=[ALGORITHM])` makes `alg: none`, HS256
     and ES256 *unrepresentable*. An implementation that reads `alg` from the token
     and then decides has already parsed attacker-controlled input to choose a
     verifier. The pin lives in one constant (`muse.auth.ALGORITHM`); RS256 only,
     because the algorithm is a property of the key set identity publishes and
     accepting one no published key uses buys nothing.
   - **A correctly signed token with no scope is a 403.** A verification-only
     implementation passes every other auth test and still authorises anybody
     holding a stale token. The scope gate is not optional plumbing.
   - **An absent scope claim is the empty set, never everything.** A gate that
     treats "no scopes" as "all scopes" is a gate with no gate.
   - **A key set that cannot be fetched is a 503, never a 401.** A 401 tells the
     caller their credential is bad when the problem is that we could not *check*
     it, which sends a caller with a good token to re-authenticate against a
     healthy identity and retry forever. `SigningKeysUnavailable` is the only auth
     failure that is not the caller's fault, and it is the only one `errortype`
     maps off `policy_denied` — an outage on the same dashboard as a fraud signal
     trains everyone to ignore that page.
   - **A token with no `account_id` is refused**, not defaulted to `sub`. muse bills
     per tenant, so an unattributable spend is a request nobody can charge for, and
     `sub` as a tenancy key makes a bug in one service a cross-tenant read. This is
     **stricter than `guard`**, which treats a missing `account_id` as `undefined`
     because its use is a rate-limit key; the two are answering different questions
     and `cafaye.yml` records the divergence for a fleet-wide ruling.
   - **The claim checks are ours, not the library's, because a verifier's errors
     carry its messages.** `joserfc`'s `InvalidClaimError.args[0]` is not the claim
     name — it is the sentence `"invalid_claim: Claim 'sub' must be a StringOrURI
     value"` — and `guard` gives the rule this follows: a library's expected values
     are the platform's internals. So `muse.auth` does its own presence, type and
     value checks, one per rule, and the response says what was wrong in muse's
     vocabulary. The library keeps the two jobs it is better at — verifying the
     signature and pinning the algorithm. Those are crypto; a date comparison is not.
     `test_a_refusal_never_quotes_the_verifiers_library` walks every failure path for
     this, because it is the property that quietly regresses when somebody delegates
     the checks to save twenty lines.
21. **The key-set refresh is bounded, and the bound is claimed before the fetch.**
   "Refresh on an unknown `kid`" without a floor is an amplification primitive:
   anyone who can send a request can send one with a random `kid` and make muse fetch
   the key set per request, unauthenticated, aimed at our own identity service. So
   `muse.jwks` has a minimum interval between refreshes, a timestamped negative cache
   of unknown `kid`s, and a cold cache that **fails closed** rather than fetching
   again. Three things follow that are easy to get wrong, and all three were wrong in
   the first draft of this packet — `tests/test_auth.py` and `tests/test_jwks.py` each
   caught one:
   - the interval is recorded on **every attempt, including a failed one**, and
     *before* the `await`, or the bound vanishes during an outage — which is exactly
     when another fetch is most expensive;
   - "no cache" is **not** a reason to always fetch, for the same reason;
   - a negative-cache entry **expires with the interval**, or a key id a rotation has
     since published stays refused for the life of the process.
   The cache is **replaced** on refresh, never merged: a merged cache is a set in
   which a withdrawn signing key never leaves. Rotation therefore works in both
   directions, and `tests/test_auth.py` asserts each one against an injected clock
   rather than a sleep (rule 14).
22. **Nothing about a token reaches a log, a span, or an error message.** The same
   boundary as rule 16, and the credential is the worst case in it: a token in a
   retained log is a credential in a searchable store. So `muse.auth` names the
   *check* that failed and never a claim's value, `ALLOWED_SPAN_ATTRIBUTES` gained
   **no** attribute in this packet, and `tests/test_auth.py` asserts the absence two
   ways — a marker inside the claims, and separately the raw compact JWS, so a service
   that logged `request.headers` passes the first and fails the second. Never at debug
   level, never in an error message, never in a test failure message.
23. **A version pin nobody has executed is a comment.** `docker-compose.yml` is a file
   whose only job is to start a database, and for its whole life nothing started it:
   no test read it, CI never invoked compose, and rule 3 means the suite opens no
   socket. So it carried two defects that any reader's eye passed over. It pinned
   `postgres:18-alpine` while five of six services sat on 17. And it did not parse at
   all — `MUSE_VAULT_KEY: ${MUSE_VAULT_KEY:?… or run: uv run …}` was an unquoted YAML
   scalar containing `: `, which is a nested mapping, so `docker compose up` died with
   "mapping values are not allowed in this context" before it looked at a container.
   `muse-08` booted the stack, which is the only reason either was found, and left
   `tests/test_compose.py` behind so neither can come back: the file is parsed, the
   database is asserted to be the platform standard's exact tag, the downgrade note is
   asserted to still name `pg_dump`/`pg_restore` and `down -v`, and the vault key is
   asserted to be a `${…:?…}` refusal rather than a literal. Nine breakages were
   applied by hand to confirm each assertion can go red — including the first version
   of the credential regex, which passed a committed `sk-proj-…` because it stopped
   at the hyphen. Rule 14's discipline and the canary's apply here unchanged: **break
   the guard on purpose and watch it go red, because a guard that passes is
   indistinguishable from a guard that was never looking.**
   The lesson generalises past compose: *every* file in this repository that only runs
   outside the suite — `bin/prime` (asserted by `test_dependencies.py`), `gate.yml`
   (asserted by `gate-check` and `gate_self_test.sh`) — is guarded for the same
   reason, and `muse-08` is the argument for why that pattern exists.
24. **An isolation suite that claims a surface the service does not have is worse than
    none.** Rule 20 established that muse refuses a token with no `account_id`, and it
    is tempting to read that as "muse is tenant-safe". It is not, and the difference is
    the whole of `muse-09`: muse stores **no tenant-owned rows**, so most of its surface
    has no tenant axis to get wrong. `vault_secrets` is keyed by `provider` (one
    platform credential per vendor, by design) and `outbox_events` has no tenant column
    at all, because core's payload schema still pins `subject: platform` until D9.
    `tests/test_tenant_scoping.py` therefore enumerates **12 account-scoped entry
    points of which exactly three carry a tenant axis**, and asserts the *absence* of one
    for the other nine — which is the only thing assertable about a discriminator that
    does not exist. The count is deliberately not inflated; a reader who believes 12
    surfaces are tenant-checked when 3 are is worse off than one who knows.
    Three properties hold, and each has a test named after the failure it prevents:
    - **Absence, never 403, on the tenant axis.** A 403 says "this exists and is not
      yours", which is strictly more information than a 404 for a resource that never
      existed — it is an enumeration oracle, and it is the difference between a caller
      who learns nothing and one who can walk a namespace. muse's single 403 is
      `InsufficientScope`, on the **capability** axis, and it is safe only because its
      body cannot vary by tenant; that is asserted, not assumed. `MissingAccount` is a
      401, because a 403 there would tell a caller holding a valid signature that the
      token is real and only its tenant is wrong.
    - **The enumeration is checked against the code, in both directions.** Every route
      the running app serves (read off its generated OpenAPI document) must be in the
      table, *and* the table must not claim a route the app does not serve; every SQL
      string literal in `src/muse/` must be attributed to an entry point, **count
      included**. `TENANT_SCOPED_COUNT` is pinned, so a packet that grows a fourth tenant
      surface changes that number on purpose next to the negative test for whatever it
      added. Read the HTTP surface off `app.openapi()["paths"]`, not `app.routes` — this
      FastAPI version represents an included router as an opaque `_IncludedRouter` with
      no `.routes`, so a route walk finds none of `POST /v1/route` and passes.
    - **A tenant surface cannot grow quietly.** No query may mention a tenant column, no
      migration may add one, and no function may take an account argument. Each is a
      commit that would require rewriting the `PLATFORM` rows, because from that commit
      a 404 is no longer the only correct answer to another account's data.
    Two of these guards were wrong before they were right, and both were found by
    breaking them rather than reading them (rule 23's discipline, unchanged):
    - The SQL guard checked that every *enumerated* query was present but never that a
      file held no *more* than the enumeration named, so a second tenant-free `select`
      appended to `metering.py` went green. A set-difference guard is only as good as the
      assertion saying which side is meant to be bigger.
    - **24 of the 26 tests in this packet were never collected.** The file was first
      written as `tests/tenant_scoping.py`, mirroring `darkroom-09`'s Rust layout where
      every `.rs` under `tests/` is a target by convention. `pyproject.toml` sets
      `testpaths` and **not** `python_files`, so pytest's default applies and the file
      was skipped — the gate read `904 passed, 3 skipped` *before and after* the commit
      that added it. Hence `test_no_test_file_is_left_uncollected`, which walks `tests/`
      for a module defining `def test_` without matching the collection pattern, plus
      `test_this_file_is_itself_collected`, because a guard that can pass because *it* is
      not running is the worst version of this failure.
    The D9 gap is **asserted, not papered over**: `Meter` knows the caller's `account_id`
    and cannot write it, so the test checks the metered payload is exactly core's five
    fields and the subject is core's reserved literal. A test implying the gap was closed
    would be the defect, not the coverage.

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

## The gate is declared, not discovered

`gate.yml` at the root states what gates this repository, against core's
`schemas/gate.schema.json`. Read it before changing `bin/prime`, `mise.toml`
or `.github/workflows/ci.yml`: the checker reads all three, and it is what
turns "run the gate" from a thing a human has to get right into a file.

Three things in it are load-bearing here and are easy to undo by accident:

- **`proof[].core-parity` must not match a line that says `skipped`.** That
  regex is the only thing standing between `bin/prime`'s exit code and the
  three core-parity tests that do not run without `MUSE_CORE_SCHEMAS`.
  Delete it and the gate is green again over `903 passed, 3 skipped` — which
  is the identity defect this format exists to remove. `tests/gate_self_test.sh`
  breakage 13 asserts the red, and its companion asserts the green that
  deleting the proof would restore.
- **`proof[].minimum` is a ratchet, and the `core-parity` floor is exact.**
  `890` is below the suite's 903 so a new test does not need the floor raised
  first; `906` is the exact count when the core tier runs, and dropping one of
  the three drift guards takes it under. **So adding tests means raising
  `core-parity`'s floor in the same commit.** `muse-08` learned this the
  expensive way: it added 8 hermetic compose tests, and a floor left at 898
  would have let a run with one drift guard deleted (905) still clear it —
  the ratchet quietly disarmed by a commit that only ever made the suite
  bigger. `tests/test_compose.py`'s pin cannot be loosened without the same
  commit re-raising the floor.
- **`external.selfContained: false`** because `bin/prime` starts with
  `uv sync --locked`, which needs PyPI on a cold checkout and a pinned
  toolchain the machine does not carry until mise installs it.

Check it with core's checker, which this repository does not vendor:

```sh
../core/harness/bin/gate-check .              # the declaration against the tree
../core/harness/bin/gate-check --prove .      # and the gate itself
MUSE_CORE_SCHEMAS=../core/schemas ../core/harness/bin/gate-check --prove .
bash tests/gate_self_test.sh                  # the declaration can fail
```

Two warnings are expected and correct, both `gate.requirement-unproven`: the
checker refuses to run `mise install` or `git clone` to see whether a
requirement is met, because an answer that depended on what happened to be on
PATH would be red on a laptop and green on CI. It is reported and never acted
on, which is the tri-state contract.

## Commands

```sh
bin/prime                              # the gate
mise run prime                         # the same gate; mise.toml declares it
rm -rf .venv && bin/prime              # the gate on a clean machine — see Dependencies
uv run pytest                          # tests + coverage gate
uv run pytest -m unit                  # only unit-marked
uv run pytest -k vault -vv             # one concern
uv run ruff check . && uv run ruff format .
uv run uvicorn --factory muse.main:create_app --reload   # local dev
docker compose up --build

# The three contract tests that need a core checkout:
MUSE_CORE_SCHEMAS=../core/schemas uv run pytest
```

Long commands get a `timeout`. Never push — the manager merges to `master`.

## Toolchain

`mise.toml` pins `python = "3.14"` and `uv = "0.12.20"`; `.python-version`
pins 3.14. If a new interpreter release lands, bump `.python-version` and
`mise.toml` together and re-run the gate.
