# REPORT-core-10 — muse declares its gate

## The finding first

**muse's gate was green while a third of its drift detection had not run, and
nothing in the repository said so.** `bin/prime` exits 0 with
`895 passed, 3 skipped` on any machine that does not happen to have a
`cafaye/core` checkout beside it. Those three tests are the only thing in this
repository that catches muse's vendored copies of core's specs drifting —
`test_contracts.py::test_patterns_are_byte_identical_to_core`, the
telemetry-vocabulary drift guard in `test_error_vocabulary.py`, and the
manifest-drift guard in `test_openapi.py`. They were not broken. They were not
running, and the exit code said nothing about it.

That is the identity defect core's `docs/gate.md` names — 1430 tests green
against an empty database — reproduced in this repository, one layer down. The
suite is honest; the *gate* was not, because nothing asserted the difference
between a run where everything ran and a run where 895 of 898 did.

`gate.yml` closes it. The `core-parity` proof matches a pytest summary line
only when nothing was skipped, so `gate-check --prove` is **red** on the exact
run `bin/prime` reports as a pass:

```
$ env -u MUSE_CORE_SCHEMAS ../core/harness/bin/gate-check --prove .
FAIL gate.proof-missing: proof 'core-parity' never appeared; the gate's output
  contains no line matching '^=+ (?!.*skipped)([0-9]+) passed'
FAIL …: 1 failure(s), 2 warning(s)
PROVE2_EXIT=1
   …and that run's own summary line was:
   ======================= 895 passed, 3 skipped in 30.20s ========================
```

The count is **above** the suite floor of 890, so `suite` passes. Nothing in
the static phase sees anything. Only `core-parity` refuses.

---

## What I changed, and why each thing is true of *this* repository

Three files. `bin/prime` is untouched, and that is deliberate — the packet says
not to modify the gate's behaviour to make it pass, and the gate was not what
was broken.

### `gate.yml` — new, at the root

Written last, once I knew the truth. The fields that took judgement:

- **`command: [bin/prime]` / `entrypoint: bin/prime`** — the gate, as the
  repository already spells it.
- **`miseTask: prime`** — muse had **no `[tasks]` section at all**, so it could
  have omitted this and drawn no warning: `gate.task-undeclared` only fires
  when a mise config *has* tasks. I added a four-line `[tasks.prime]` to
  `mise.toml` anyway. Two reasons, both in the file's comments: the fleet's
  spelling works here, and `miseTask` becomes a **second, independent reading**
  of what the gate is — which is what `gate.task-unresolvable` exists to catch.
  `run` is the bare file, not a shell string, so the checker can resolve it
  without a shell. Verified end to end: `mise run prime` → exit 0, 898 passed.
- **`timeoutSeconds: 1200`** — CI's own budget (the `gate` job is
  `timeout-minutes: 20`). A warm local run is ~35 s; a cold checkout spends most
  of the rest in `uv sync --locked`.
- **`proof[].suite`, floor 890** — the decrease-detector, with five tests of
  deliberate margin below today's 895 so a new test does not force a hand-edit
  of the floor first.
- **`proof[].core-parity`, floor 898** — the load-bearing one. `minimum: 898`
  is exact, not rounded: 898 − 895 is precisely those three drift guards, so
  this floor cannot be satisfied by a run in which the cross-repo tier did not
  execute, and it is a ratchet in the other direction too.
- **`ci.invokes: [bin/prime]`, not `[bin/prime, -q, -rs]`** — CI does run the
  flags and both spellings pass today. Declaring them would make the check
  sensitive to a verbosity change that has nothing to do with whether CI gates
  this repository, which is the only question the field asks.
- **`external.selfContained: false`** with three requirements: `toolchain`
  (the two mise pins), `network` (PyPI once, on a cold checkout), and
  `filesystem` (a core checkout with `MUSE_CORE_SCHEMAS` pointed at it). The
  network requirement's `satisfy.command` is `[bin/prime]` — a
  repository-relative path the checker verifies — because there is no separate
  setup script here to name instead. Named in a comment as *not* required:
  no database, no service, no credential, no secret.

### `tests/gate_self_test.sh` — new

The proof that the declaration can fail. 13 breakages, each naming the finding
it expects, plus 3 controls and 3 warning cases.

### `AGENTS.md`, `README.md` — small additions

`AGENTS.md` gains a "The gate is declared, not discovered" section, because the
declaration is only useful if the next person reads it before deleting the
proof that makes it load-bearing.

---

## The self-test, and what it actually proved

```
$ bash tests/gate_self_test.sh
PASS: gate_self_test — 3 controls green, 13 breakages went red and each named
the finding it was written for, 3 warnings stayed green with their exit code
at 0, 1 hygiene case passed. 0 skipped.
SELFTEST_EXIT=0
```

Pass and skip counts are separate: **3 controls green, 13 breakages red, 3
warnings green, 1 hygiene case, 0 skipped.** Every breakage ran, and the script
fails if any category reports zero — a self-test whose recipes have gone stale
reports "0 breakages" and exits 0, which is the exact failure it exists to
catch.

The four the brief names, and where they landed:

| Brief's breakage | Result |
| --- | --- |
| `entrypoint` names a file that does not exist | breakage 3, `gate.entrypoint-missing` |
| `miseTask` resolves to a different file than `entrypoint` | breakage 6, `gate.task-unresolvable` |
| the `proof` regex matches nothing the gate prints | breakages 9 and 13 |
| `ci.workflow` missing / does not invoke the gate | breakages 7 and 8 |

Breakage 13 is the one that matters, and it is not string matching:

```
breakage 13/13: a gate that exited 0 with three tests skipped:
               above the suite floor, and still refused — caught by `gate.proof-missing`
control 3:     the same skipped run with the core-parity proof deleted —
               green, because nothing else notices
```

That second line is the measurement, not a claim: it is the same run with one
block deleted from `gate.yml`, and it goes green. **The cost of deleting
`core-parity` is one passing run over three tests that never executed.** Had
breakage 13 ever gone red for some other reason, this case would catch it.

The warning cases assert the exit code stays **0** while the finding prints —
`gate.requirement-unproven` ×1, `gate.task-unreadable`, `gate.ci-undeclared`.
That is the tri-state contract made mechanical: a warning means "this machine
could not answer that", and a checker that laundered it into a failure would be
red on a laptop and green on CI.

The hygiene case runs a gate that prints a value read from its own environment
and asserts the checker's **report** stays clean while its **log** holds it. No
off-the-shelf tool detects that class: 0 of 268 Semgrep rules intersect
CWE-532, gosec has no `ast.CallExpr` case, Bandit is `ast.Constant`-only. So it
is a test, not a promise.

### A defect this found in my own work

The self-test's `edit` refuses an unmatched recipe, and that caught me. My
first `gate.yml` described both floors in prose inside comments —
`# 890, deliberately below today's 895` — and never wrote a `minimum:` key at
all. The schema permits an absent floor, so `gate-check` was **green**: a suite
of any size would have satisfied a "proof" that asserted nothing. Breakage 11
tried to edit `minimum: 890`, found nothing, and failed loudly. Fixed; the
floors are now keys.

I am recording this because it is the argument for the whole script. A
declaration I had read, written and checked by hand was wrong in exactly the
way the format exists to make visible, and the only thing that caught it was a
recipe that refused to apply.

---

## Gating this packet with muse's own gate

Run under `bash` with `set -o pipefail`, exit code read from `${PIPESTATUS[0]}`
— not from `$?` of a pipe, which is this fleet's one recorded false green.

**With the core checkout, which is the state CI's `gate` job runs:**

```
$ MUSE_CORE_SCHEMAS=../core/schemas bin/prime
============================= 898 passed in 40.98s =============================
Required test coverage of 100% reached. Total coverage: 100.00%
GATE_EXIT=0

$ mise run prime          # the alias gate.yml now promises
MISE_RUN_PRIME_EXIT=0
============================= 898 passed in 40.98s =============================
```

**898 passed, 0 skipped**, 100% branch coverage. Nothing is hiding in that
green: the three drift guards ran.

**Without it, the state this packet exists to close:**

```
$ bin/prime
RUN_A_EXIT=0
================== 895 passed, 3 skipped in 133.32s (0:02:13) ==================
```

`bin/prime` exits **0** there. Three tests skipped. That is the finding, and it
is still true — I did not change the gate, because the gate's behaviour was not
what was broken. What changed is that `gate-check --prove` now calls that run
red.

**The checker, both phases, both directions:**

| Run | Exit | Findings |
| --- | --- | --- |
| `gate-check .` (static) | 0 | 0 fail, 2 warn (both `gate.requirement-unproven`, by design) |
| `gate-check --prove .` + `MUSE_CORE_SCHEMAS` | 0 | 0 fail, 2 warn |
| `gate-check --prove .` without it | **1** | `gate.proof-missing` on `core-parity` |
| `bash tests/gate_self_test.sh` | 0 | 3 controls green, 13 breakages red, 3 warnings green, 1 hygiene, **0 skipped** |

Both warnings are `gate.requirement-unproven`, on `mise install` and
`git clone`. Correct and expected: the checker refuses to run them, because an
answer that depended on what happened to be on PATH would be red on a laptop
and green on CI. Reported, counted separately, and never acted on.

---

## What I could not verify

This section is the point, not an appendix. Five things, in descending order of
how much they should worry you.

**1. I never ran the gate on a machine with no `MUSE_CORE_SCHEMAS` *and* no
network.** The 895/3-skipped baseline and the red `gate-check --prove` both ran
on this machine with `env -u MUSE_CORE_SCHEMAS`, which removes the variable but
not the checkout. The red proving run is therefore a proof that the *proof*
works, not a proof that the *requirement* list is complete. Nothing here
establishes that a truly cold machine fails for the reasons `gate.yml` says it
does — I exercised the paths I could and inferred the rest from reading
`bin/prime`. A container with no `~/.config/uv` cache and no core checkout would
be the real test, and I did not build one.

**2. The `filesystem` requirement's `satisfy.command` is the weakest thing in
the file.** `git clone --depth 1 git@github.com:cafaye/core.git ../core` gets
you a checkout; it does **not** set `MUSE_CORE_SCHEMAS`, and no file in this
repository can assert that variable. So the command satisfies half of a
requirement and `unmet` names the symptom (`895 passed, 3 skipped`) rather than
a command that returns nonzero. I chose this over writing no requirement —
the requirement is still true and its absence is how the identity defect
happened — but it is a requirement whose `satisfy` is a partial remedy, and I
have said so in the file's comments rather than leaving it to look complete.
**The thing that actually makes the tier non-optional is the `core-parity`
proof, not this requirement.** A reader who wants the requirement to be
self-sufficient should add a `tests/core_checkout.sh` that clones and verifies,
which is a real script with a real exit code, and is owed to a follow-up.

**3. The `--prove` breakages in the self-test run a stub gate, not
`bin/prime`.** They print the summary lines the real gate prints, captured
verbatim from an actual run, because thirteen breakages each running a full
`uv sync --locked` plus 898 tests is a self-test nobody runs. So the script
proves *the declaration can fail*; it does **not** prove *muse's gate works*.
That is the separate table above — `bin/prime` and `mise run prime`, both
exit 0, 898 passed, 0 skipped — and the two are not interchangeable. If the
real gate's output ever changes shape, the stub keeps matching and the self-test
stays green while the real proving run goes red. **A future change to
`bin/prime`'s output must be made in `install_stub` in the same commit.** I did
not add a check that compares the stub's summary line to the real gate's,
because that check would need to run the real gate, which is the cost this
design exists to avoid.

**4. Nothing here runs the checker inside muse's own gate.** `bin/prime` does
not invoke `gate-check`, so a wrong `gate.yml` does not make the gate red — it
makes `gate-check` red. core runs its declaration's static half from its own
`bin/prime`; muse cannot, because `gate_check.py` travels with
`harness/cafaye_contract.py` and a vendored copy of one without the other would
be a second YAML dialect. The honest options are (a) core publishes the checker
as an installable, or (b) muse's CI gains a step that runs it against a core
checkout. **I did not do (b)**: adding a step to `.github/workflows/ci.yml`
would change CI behaviour in a packet whose brief is a declaration, and the
sparse checkout that job performs (`sparse-checkout: schemas`) does not even
contain `harness/`. That is the single highest-value follow-up, and it is a
follow-up.

**5. `gate.ci-disagrees` cannot see `MUSE_CORE_SCHEMAS`, so CI could stop
setting it and this declaration would not notice.** The checker reads `run:`
bodies textually; an `env:` block is not in one. I have said this in
`gate.yml`'s comments. The coverage is indirect and real but partial: CI's own
`no test may skip` step would catch the consequence, which is why the gap is
narrow rather than open — but the *declaration* does not close it, and I am not
able to close it from here.

Two smaller notes. `ci.invokes: [bin/prime]` is verified against the current
workflow text only; a CI change to how the gate is invoked is caught when
someone re-runs the checker, not automatically. And I did not verify the gate
on a cold venv (`rm -rf .venv && bin/prime`) — the network requirement's
`unmet` describes what that looks like from reading uv's behaviour, not from
watching it.

---

## For the manager

- **The gate is declared and it is load-bearing.** 13 reds, each naming its
  finding; 3 controls green; 0 skipped.
- **One judgement call worth your review: `[tasks.prime]` in `mise.toml`.**
  Four lines, `bin/prime` untouched, verified working. It buys the fleet's
  spelling and the `task-unresolvable` cross-check. If you would rather this
  packet not touch `mise.toml`, delete the `[tasks.prime]` block and the
  `miseTask:` line together — the declaration then draws no warning at all,
  because `gate.task-undeclared` only fires when a mise config *has* tasks.
- **The floor ratchet is not automatic.** `minimum: 890` will not stop
  drifting down; `minimum: 898` will not stop drifting down either. core
  enforces its own floor with a test in `tests/test_specs.py`. **muse has no
  equivalent**, and adding one means importing core's schema into a test, which
  is the vendoring question this packet deliberately does not open. So: the
  floors are correct today and unenforced tomorrow unless someone edits them.
  That is a known, named gap, not an oversight.
- **Highest-value follow-up: run the checker from CI** (item 4 above). Until
  then, `gate.yml` is a true statement that nothing in muse reads.