# HANDOFF — muse-04, the vendored traces schema drift

**Branch:** `fix/schema-drift-01` · **Base:** `dfc6d55` · **State: complete.**
Nothing below is unfinished work; §4 is a report and §5 is a report to core.

## 1. The answer the brief asked for

**A span carrying `service.namespace` was silently unvalidated.** Not dropped, not
rejected. Two measurements, in this order:

**The stale schema would have refused it.** With `src/muse/schemas/telemetry/traces.schema.json`
reverted to the `dfc6d55` bytes and the resource muse actually builds:

```
jsonschema.exceptions.ValidationError: Additional properties are not allowed
('service.namespace', 'telemetry.sdk.language', 'telemetry.sdk.name',
'telemetry.sdk.version' were unexpected)
```

**Nothing ever asked it to.** Four independent reasons, each verified in the tree:

| # | Where | What it does |
|---|---|---|
| 1 | `tests/test_trace_propagation.py::_core_document` | projects the span onto a four-key document (`name`, `kind`, `status`, `attributes`) — **no `resource` key**, so `$.resource` in core's schema is unreachable |
| 2 | `tests/support/tracing.py::payloads` | reads `name`, `kind`, `status`, `attributes` off `ReadableSpan` and never reads `span.resource` |
| 3 | `tests/support/tracing.py::recording_telemetry` | `TracerProvider()` with **no resource**, so spans under test carry the SDK default, not `telemetry._resource`'s |
| 4 | `src/muse/` | `jsonschema` appears **nowhere**. It is a dev-group dependency. The vendored schema's only production reader is `muse.errortype`, which takes the `error.type` enum out of it at import |

So "rejected" was available and "unvalidated" was what happened. That distinction
decided the fix: nothing in muse's code was wrong. `_resource` was correct; the
vendored copy was behind it. The fix is core's four lines.

A corollary worth keeping: a **closed** list (`additionalProperties: false`) that
no component reads is indistinguishable from a correct one. The closure gave no
protection here, and `test_a_mapped_span_validates_against_core_schema` — which
reads as though it validates a whole span — never reached the half that was wrong.

## 2. Which sync tool, and why not the sanctioned one

**vendir is the fleet's mechanism and I did not use it.** `kit/core/vendir/vendir.yml.muse`
is muse's config; `kit/core/vendir/README.md` records that vendir 0.46.2 run
against `cafaye/core` produces bytes byte-identical to what muse commits. Three
reasons, in order of weight:

1. **It copies from the wrong byte stream.** The config pins
   `git: {url: https://github.com/cafaye/core.git, ref: master}`. The gate compares
   against the **local checkout** at `MUSE_CORE_SCHEMAS=…/cafaye/core/schemas`.
   Those are different commits whenever the checkout is ahead of or behind the
   remote, and a vendir sync would make the drift guard red for a reason that has
   nothing to do with this fix.
2. **vendir is not installed** on this machine (`which vendir` → not found), and
   installing a carvel toolchain is outside a one-hour packet.
3. **Repointing it is forbidden.** A local directory source means editing
   `kit/`, which is another repository. The brief says do not touch another
   repository, and that instruction outranks a nicer mechanism.

**What I used instead: `cp`, plus a hash.** That is the operation vendir performs
for a `git:` + `includePaths` + `newRootPath` config — the `newRootPath` in that
config exists only to strip `schemas/telemetry` so the file lands at
`src/muse/schemas/telemetry/`, which is where a plain copy puts it. Proof the
bytes are right:

```
shasum -a 256  →  101132a6792318670c352a8f61a3231387748eb53ec86d0a23dbac55a71ed576
cmp            →  byte-identical
```

That sha is also the one `caf/caf.lock` already records for
`internal/contract/schemas/telemetry/traces.schema.json`, so core's current file,
muse's copy, and the copy another fleet consumer holds are one document. That
agreement is independent of anything I did.

**A successor with vendir installed should still run it** — not to change the
bytes, but because a tool-driven sync is what the fleet standardised on and a
hand copy is the step that rots. Also note `ref: master` contradicts its own
README's "must be changed to a `vX.Y.Z` tag"; core publishes no tags yet, so
`ref: master` is unavoidable today and is a real fragility, not a muse problem.

## 3. Gates, with the real exit status, measured not inferred

Every run redirected to a file; `echo $?` read immediately after. Nothing piped
through `tail` or `grep`.

| gate | before (`dfc6d55`) | after (branch head) |
|---|---|---|
| `MUSE_CORE_SCHEMAS=<core>/schemas pytest -q` | `1 failed, 1035 passed`, **exit 1** | `1037 passed`, **exit 0** |
| `pytest -q` (no checkout) | `1032 passed, 4 skipped` | `1033 passed, 4 skipped`, exit 0 |
| branch coverage | 100% | 100% |
| `core/harness/bin/gate-check .` | — | `0 failure(s), 2 warning(s)`, exit 0 (both the expected `gate.requirement-unproven`) |

**The drift guard, watched both ways.** Before: exit 1,
`FAILED tests/test_error_vocabulary.py::test_the_vendored_schema_is_byte_identical_to_core`.
After: `1 passed`.

**The new test, watched both ways.** Before (schema reverted to the stale bytes):
exit 1, `ValidationError … ('service.namespace', 'telemetry.sdk.language',
'telemetry.sdk.name', 'telemetry.sdk.version' were unexpected)` — red for the right
reason, naming all four. After: passes. Both halves confirmed non-vacuous
independently: the good document validates, the bad one does not, and the floor
assertion sees `service.namespace == "cafaye"`.

`gate.yml`'s `core-parity` floor moved **1036 → 1037** in the same commit, per
AGENTS.md. Measured on both sides, not by arithmetic — and for the first time on
that clause the floor is an integer with **no compensating failure**, because the
`+ 1` every comment above it carries stopped existing when the drift guard went
green. I also discharged the `NOT YET OBSERVED WITH A CORE CHECKOUT` note that had
been sitting in that file, keeping the original paragraph rather than deleting it:
the reasoning was sound, the world changed.

## 4. Pre-existing reds I did not cause, and did not fix

Attribution first, per rule 14: clean-`HEAD` baseline, then "can my diff reach
that surface". Both of these were reproduced on stashed-clean `dfc6d55`.

### 4.1 `bin/prime` is red at its **lint** stage, on 4 E501s

```
$ git stash && uv run --locked ruff check .   # clean dfc6d55
CLEAN_RUFF_EXIT=1 ; 4 × E501 in tests/test_quickstart.py
```

All four are lines 116, 131, 182 and 263 of `tests/test_quickstart.py` — a file my
diff cannot reach (`git diff --name-only dfc6d55 -- tests/test_quickstart.py` → 0
files). `ruff format --check` wants the same four wrapped. Inherited from muse-03
(`6d1da13`). I left it: it is not this packet's surface, and folding an unrelated
file's reformat into a one-file diff makes the schema change harder to review.

### 4.2 `tests/gate_self_test.sh` is 19 of 20, on both sides

```
FAIL gate_self_test: a gate that proves 1036 tests where the declaration promised
999 — expected exit 1, got 0
```

Same single case on clean `dfc6d55`; only the number in the label tracks my floor,
because `parity_floor()` reads it out of `gate.yml`. The case edits the
**self-contained** proof's `minimum: 890` → `999` and expects `gate.floor`, but
`gate.floor` fires when a floor **exceeds** the proved count — with 999 and 1037
neither does, so the stub gate legitimately passes and the case's expectation is
wrong. It went stale when muse-03 raised the core-parity floor to 1036 and the
stub began proving a count above 999. Not mine, but **it is a live guard that no
longer goes red**, which is precisely what rule 23 exists to prevent — worth a
packet of its own, and it needs a decision I should not make unilaterally: the
fix is either editing the self-contained floor the case mutates, or rewriting the
case to raise the floor *it* is asserting about.

## 5. Reports, not edits

**Core is the owner and its file looks right.** `service.namespace`'s description
names `src/muse/telemetry.py:390` — and after this branch, that line number is
correct (`"service.namespace": "cafaye"` is at `src/muse/telemetry.py:390`). The
description of `telemetry.sdk.name` also names `lib/kit/telemetry.rb:183`; I did
not check kit, but nothing in core's four additions looks wrong to me.

**Three omissions in the test suite are still there** (§1, rows 1–3). A span
exported *through the tracer in the test suite* still has its resource dropped
before validation. Closing that means `payloads()` carrying `span.resource` and
`recording_telemetry` taking muse's real resource — which breaks the four
exact-dict assertions at `tests/test_telemetry.py:195, 217, 239, 273`. That is a
wider change than "one file plus whatever proves the span question", so it is
named here rather than done. What this packet guarantees is narrower and worth
stating exactly: **the resource muse builds conforms to the schema muse ships.**
It does not yet guarantee that a resource *read off an exported span* is ever
validated, because nothing reads it.