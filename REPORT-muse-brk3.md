# REPORT-muse-brk3.md — muse relay: the publisher, and the tenancy declaration it must update

**Packet:** BRK-3R (`broker/muse-brk3`)
**Verdict:** accomplished. `mise run prime` exits 0 (1061 passed, 4 skipped, 100%
branch coverage), `tenancy-check .` exits 0 (0 failures, 2 warnings), `gate-check .`
exits 0 (the same two `gate.requirement-unproven` warnings AGENTS.md records as
expected).

---

## 1. What was wrong, and what was actually wrong

The brief is precise about the visible symptom, and the visible symptom was the
smaller half of the defect. Restated because the distinction is the whole packet:

**The visible failure** (`tenancy.honest-zero`, 2 places at `errors.py:289`) had
this cause: `e051a76` added `UnpublishableEvent` to `src/muse/errors.py`, which
pushed `MissingAccount`'s docstring — and its two `account_id` mentions — from
lines 269/271 down to 289/291. `tenancy.yml` vouches for its key-carrying sites
**by line number**. The pins no longer matched. Two sites were now undeclared,
and an undeclared site is an omission, so the honest zero read as a lie.

**The invisible failure** is the one worth a packet's attention: **nothing in this
repository read `tenancy.yml`.** The declaration was audited only from outside the
repository that owns it. A worker could add a class to `errors.py`, shift the pins,
run a green gate, commit, and push — and learn otherwise from a checker that runs
after the merge, in another repository, by someone else. That is not a test
strategy; it is an audit trail with a delay measured in hours. Fixing the two line
numbers without fixing that would have been the fifth time this fleet learned the
same lesson from the same side of it.

So the packet has two halves, and the second is the reason the first will not
happen again.

---

## 2. The tenancy fix: option 2, named

**Option 2.** The `account_id` in the new code is incidental.

The evidence, stated rather than asserted, because a vouch is only as good as the
claim underneath it:

- **The publisher does not scope by account.** `outbox_events` has no tenant
  column. `test_no_migration_carries_a_tenant_column` and
  `test_no_query_filters_or_writes_by_a_tenant` in
  `tests/test_tenant_scoping.py` hold both halves, and the second parses every
  string literal in `src` rather than matching one line, so a predicate written
  across two lines cannot pass.
- **The claim query is core's own.** `for update skip locked`, `where published_at
  is null`, ordered by `(created_at, id)`. No `account_id`, no `where` on a caller.
- **The payload is closed at five fields.** `envelope_for` builds exactly
  model/provider/tokens_in/tokens_out/cost_micros, and
  `test_a_metered_event_names_no_account` asserts the account is *absent* from the
  row, from the payload, and from the envelope's field list. The D9 gap is still a
  gap and is still named, not closed.
- **The two new sites are prose.** `errors.py:289` is `MissingAccount`'s
  docstring first line; `:291` is its second. Both describe a 401 for a token with
  no usable account. They are the same *kind* of site as the fifteen already
  vouched — a claim check, a docstring describing one, or core's vendored schema —
  and they moved rather than appeared.

Option 1 was available and was rejected: declaring the publisher account-scoped
would have required inventing a tenant axis the table does not have, and writing
tests that prove a scope which does not exist. That is the "isolation suite that
claims a surface the service does not have" failure rule 24 names as worse than
none.

`tenancy.yml` now declares:

```yaml
- file: src/muse/errors.py
  line: 289
  reason: MissingAccount docstring — a verified token carrying no usable account is refused with a 401; an authorization rule about the token, and no row is read or scoped
- file: src/muse/errors.py
  line: 291
  reason: MissingAccount docstring — gives the billing reason for that refusal; prose describing the claim check, not a predicate on any table
```

The reasons name the class, the check, and why the check is not a row predicate.
They are not "incidental mention" — a reason that vague is the laundering failure
the brief warns about, and it is what the checker is right to reject.

---

## 3. The durability fix: Guard 3

`tests/test_tenant_scoping.py` gains a third guard, appended to the file that
already holds the other two. It mirrors core's `scan_file` over the declared
`scope.sources` and asserts the declared pins **are** the tree's pins, in both
directions:

- **A key-carrying line the declaration does not name is a failure.** Core's
  `tenancy.honest-zero`, reproduced where it can fail a packet instead of a merge.
- **A declared line that no longer carries the key is a failure too.** A pin
  pointing at prose that moved is a declaration that reads as checked and is not —
  rule 23's "a guard that can pass is indistinguishable from a guard that was never
  looking", one level up.

It also asserts a floor of 15 sites, so a scan that read the wrong directory and
found zero cannot agree with any declaration.

### The mirror is stated as a mirror

The guard re-implements core's scan locally rather than importing it, and the
reason is recorded in the code and here: core is another repository, a service that
vendored the checker would be a service that could edit it, and
`tests/test_dependencies.py` would refuse the import anyway. The stated cost is
that this guard can go stale against a change to core's scan. The stated benefit is
that the failure it prevents is a failure of *this repository's declaration against
this repository's tree*, and it now fails in the hour the line moves.

### Three breakages, applied by hand, each watched go red

Rule 23's discipline, applied to a guard that had only ever passed. Each was
reverted after it went red:

1. **A key-carrying line added to `errors.py`** — the `e051a76` failure, exactly:
   *"the scan found 16 key-carrying sites and 15 were expected"*.
2. **A declared pin dropped from `tenancy.yml`** —
   *"Undeclared: ['src/muse/errors.py:291']"*.
3. **The scan pointed at a directory that is not there** — the canary for the
   guard that can pass by finding nothing: *"the scan found 0 key-carrying sites and
   15 were expected"*.

The third is the one that would have been missing otherwise, and it is the reason
`EXPECTED_SITES` exists as a named constant with its own assertion rather than as a
comment.

The guard was also run against the *pre-fix* tree, where it failed for the real
reason rather than an artificial one: `Undeclared: ['src/muse/errors.py:289',
'src/muse/errors.py:291']; declared but absent: ['src/muse/errors.py:269',
'src/muse/errors.py:271']` — which is core's finding, reproduced in the suite, at
the moment the lines moved.

---

## 4. The publisher, rebuilt

The substrate is the previous attempt's three commits, cherry-picked with `-x` so
the original hashes stay in the log. Its substance is unchanged, because the brief
is to rebuild it correctly and not to redesign it. What it delivers:

**Against identity's documented design** (`publisher.go`, `nats.go`,
`cmd/identity/main.go:474-485`), matched rather than re-derived:

| identity | muse |
| --- | --- |
| `Publisher` one method wide | `Transport` one method, `publish` |
| `Claim` so the loop is testable without a database | `publish_batch()` returns a count; `_claim(tx)` is the seam, and `tests/support/fake_database.py` is the `Database` |
| batch 100 | `DEFAULT_BATCH_SIZE = 100` |
| fixed one-second poll, not exponential backoff | `_idle_seconds() -> 1.0`, and the docstring says why |
| loop tied to the process's context | `asyncio.create_task(publisher.run(stop=stop))` in the lifespan; `stop.set()` then `loop.cancel()` |
| `NoopPublisher` never wired | **no no-op transport at all** — `outbox_transport()` returns `None` and no loop starts |

Muse keeps the "no no-op publisher" property more strongly than identity does: the
only thing `outbox_transport` returns when there is no bus is `None`. A transport
that marked every event published and drained the outbox into nowhere would keep
the rows' promise by discarding it.

**Kept, deliberately — the redaction guard.** `refuse_caller_content` sits at the
one point every published event passes through — `OutboxRow.envelope()` — rather
than at each call site, where it would be a convention. It refuses on the field
*name* and on a `Secret`'s *type*, never by comparing values. A row muse refuses is
not dropped: the loop treats the refusal as a transport failure, so it stays
unpublished with `attempts` climbing, which is an alert. `tests/test_outbox.py`
holds nine tests over it, unweakened.

**Kept, deliberately — the `consumed` vocabulary.** core's DECISIONS.md D9 rules
that `consumed` joins the action vocabulary, and `muse.usage.recorded` was
rejected as a naming collision. The event type stands; `contracts.py` and
`cafaye.yml` record where the earlier comment was wrong rather than deleting it
quietly.

**The transport is JetStream, not core NATS**, because `published_at` is set from
the broker's *acknowledgement* and `js.publish` is a request that blocks until the
broker acks. The dial is lazy (a broker down at boot is a rows-accumulate
condition, not a boot failure), a publish is bounded by a timeout passed to the
client, and a failed publish drops the session so the next pass redials.

### What is asserted, and what is not

Both Done-means are proved by a test that asserts *delivery*, not that a function
was called. `test_the_lifespan_starts_the_loop_and_a_row_reaches_the_bus` drives
the real lifespan and asserts the subject (`muse.tokens.consumed`, inside the
`muse.>` filter the stack's stream must cover) **and the exact payload bytes** the
transport handed the broker client, and that `published_at` was set from that ack.
The stand-in is a broker *client*, not a mocked-out function — a transport that
published the wrong subject or the wrong bytes fails there.

`test_the_lifespan_does_not_start_a_loop_without_a_broker` asserts the second
Done-means, and the half that matters: not only that no loop starts, but that **no
row is claimed and none is marked**. A row marked by a loop that published nowhere
is precisely what identity's `NoopPublisher` comment warns about.

The boundary is stated in the test file rather than glossed: muse's suite opens no
socket (rule 3), so what is *not* asserted is a server storing the message.
identity proves the equivalent wiring against a real broker in
`cmd/identity/outbox_wiring_test.go`.

---

## 5. Two defects in the previous attempt, fixed

Both were in the packet's own files and both were caught by reading rather than by
the gate:

1. **`main.py` declared `logger = logging.getLogger("muse")` and the "the broker
   dial" comment block twice** — the previous worker's two edits landed on the same
   lines. Removed the duplicate; `logger` is declared once at module scope.

2. **`outbox.py` defined `_CAP_EXPONENT` twice under one name** — a value derived
   from `MAX_BACKOFF_SECONDS` (9, for a 300s cap) and a literal (64), with two
   docstrings describing two different constants. The second assignment silently
   won. **This was pre-existing on `4c5b741` and is not a live bug** — 64 happens to
   be the safe choice, and `backoff_seconds` returns the same answer for every
   input either way. It is a readability defect: a reader has to work out which
   definition is live. The survivor is renamed `_CAP_ATTEMPTS` and its docstring
   says why it is 64 and not the derived 9 (the derived exponent is where `2 ** n`
   first *reaches* the cap, and using it as the comparison would make the cap apply
   one attempt earlier than the arithmetic does). `import math` went with it.

`gate.yml`'s `core-parity` floor moves 1063 → 1065, **measured on both sides rather
than counted**: `uv run pytest` with no checkout reads `1061 passed, 4 skipped`;
`MUSE_CORE_SCHEMAS=<core>/schemas uv run pytest` reads `1065 passed, exit 0`.
1061 + 4 = 1065 exactly, with no compensating failure. Two of those are the new
guards.

---

## 6. What I deliberately left alone

- **The D9 tenancy gap.** `Meter` knows the caller's `account_id` and cannot write
  it, because core owns the payload schema and closes `data` at five fields. Still
  a gap, still named, still asserted as a gap by
  `test_a_metered_event_names_no_account`. Closing it is core's change.
- **Streaming, tool calls, embeddings, images, batches.** Out of scope for this
  packet and every packet up to it. Not touched.
- **`Idempotency-Key`.** PLAN §7 assigned it to muse and it is not built;
  `muse.errors.ProviderIndeterminate` documents the billing consequence. Not
  touched.
- **The broker's compose service name.** `NATS_URL` is documented in the README's
  variable table and left commented out in `docker-compose.yml`. The URL's host is
  kit's to name, and a URL pointing at a host that does not exist is worse than one
  nobody set. The consequence is written in the file: **the stack must define a
  JetStream stream whose filter covers `muse.>`**, or every publish fails and no
  event ever leaves the table — loudly, with `attempts` climbing.
- **The other repositories.** Nothing outside muse was edited. core's vocabulary is
  settled and D9 is not being relitigated.
- **`tenancy.yml`'s warning shape.** It still reports
  `tenancy.enumeration-partial` over the same fifteen sites and
  `tenancy.scope-key-unused`. Both are correct — a warning never moves the exit
  code, and a Python service scanned by a SQL reader *should* report "I cannot see
  this" rather than a confident zero.

---

## 7. Commands, and their outputs

```
$ mise run prime
==> syncing (locked)
==> lint
All checks passed!
==> tests
======================= 1061 passed, 4 skipped in 22.36s =======================
exit 0

$ MUSE_CORE_SCHEMAS=/Users/kaka/Code/any/moon/cafaye/core/schemas uv run pytest -q
1065 passed in 23.64s
exit 0

$ /Users/kaka/Code/any/moon/cafaye/core/harness/bin/tenancy-check .
OK /Users/kaka/Code/any/moon/wt/muse-brk3: 0 failure(s), 2 warning(s) — warnings do not move the exit code
exit 0

$ /Users/kaka/Code/any/moon/cafaye/core/harness/bin/gate-check .
OK /Users/kaka/Code/any/moon/wt/muse-brk3: 0 failure(s), 2 warning(s) — warnings do not move the exit code
exit 0
```

The 4 skips are the four tests that need `MUSE_CORE_SCHEMAS`
(`test_contracts.py`, `test_error_vocabulary.py`, and two in `test_openapi.py`),
each of which says so rather than passing quietly. The 2 tenancy warnings and the 2
gate warnings are the expected ones AGENTS.md records.

---

## 8. The successor's first move

There isn't an obvious one, which is the point of the packet. If a follow-up is
dispatched, the useful work is:

1. **Run the stack.** `KIT_COMPOSE_DIR=<kit checkout>/templates/compose` with
   `NATS_URL: nats://nats:4222` uncommented, a stream whose filter covers `muse.>`,
   and one metered completion. This packet proved delivery against a stand-in
   broker *client*; the next proof is a real broker storing the message, which is
   the one thing a no-socket suite cannot assert. identity has the equivalent test
   and muse does not, for the same reason.
2. **Add a `stream` assertion or a stream-existence check at boot.** Right now a
   broker with no covering stream fails every publish loudly, which is correct. A
   `stream` parameter on the JetStream publish would let the broker refuse on a
   no-such-stream directly, but naming a stream is a deployment decision, not a
   service one.
3. **D9.** If core's payload schema gains `account_id`, `metering.py` writes it and
   the fourteen `PLATFORM` rows in `test_tenant_scoping.py` become ten non-zero
   ones, which is the count changing on purpose next to the negative tests for what
  changed.

---

## 9. Commit log

```
835a8b1 tenancy(muse-brk3): the declaration's pins are checked, and they moved - option 2
8034c13 outbox(muse-brk3): the guards break, and the floor is the number that was measured
6aee403 outbox(muse-brk3): the publisher runs, and it is conditional on NATS_URL
3554eb5 outbox(muse-brk3): the publisher refuses a payload that carries caller text
4c5b741 licence(muse): BSL, and the reversal from MIT is said out loud
```

The three `outbox(muse-brk3)` commits are cherry-picks with `-x`; their messages
are the previous attempt's, preserved because the substance was right. `835a8b1`
is this attempt's own work and carries the tenancy fix.
