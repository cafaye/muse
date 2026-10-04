# REPORT — gateway-rename-muse-01

The repository an earlier sweep missed. `cafaye.yml` declared
`dependencies[].name: guard` — the last reference to the service's old name left
anywhere in the fleet — and that line *resolves*, so a manifest that parses,
validates against core's schema and starts nothing has been on master since.

Four commits on `worker/gateway-rename-muse-01`. Nothing pushed, nothing merged, no
tag, `origin` untouched.

| commit | what |
|---|---|
| `a6ad2c2` | the manifest, and the three assertions that replaced one circular one (5 files, all `M`) |
| `028de2c` | the service's name in 25 more places, one case variant, and one sentence that read wrong (8 files, all `M`) |
| `0662165` | `CHANGELOG.md` |
| `1e31c77` | the self-test's hardcoded count, which my own floor raise had broken |
| this report | `REPORT-gateway-rename-muse-01.md`, the only file this packet adds |

---

## 1. The gate, before and after

Measured on clean `d4d5128` **before the first edit**, and again on the branch head.

| | no core checkout | with `MUSE_CORE_SCHEMAS=../core/schemas` |
|---|---|---|
| **before** (`d4d5128`) | `968 passed, 3 skipped`, **exit 0** | `1 failed, 970 passed` |
| **after** (branch head) | `969 passed, 4 skipped`, **exit 0** | `1 failed, 972 passed` |

`bin/prime` is green at every one of the four commits. Coverage is 100% at both
ends, branch coverage on, `--cov-fail-under=100`.

**The one failure is pre-existing and I did not cause it.** Attribution first:
`git status --porcelain` was empty when I measured it, at `d4d5128`, before I had
edited a file. It is `test_the_vendored_schema_is_byte_identical_to_core`, and the
cause is legible in the diff:

```
core's schemas/telemetry/traces.schema.json has gained four resource attributes
that muse's vendored copy at src/muse/schemas/telemetry/ does not have:
  + service.namespace
  + telemetry.sdk.name
  + telemetry.sdk.language
  + telemetry.sdk.version
vendored = 203 lines, core = 207 lines
```

**Not fixed here, deliberately.** Re-vendoring core's schema is core's call, not a
rename's side effect: muse's copy is asserted *byte-identical*, so the fix is to
copy core's four lines across, which is a commit about `core`'s drift and would
belong in a packet about `core`. It is named in `gate.yml`'s floor comment so the
next reader of a red `core-parity` proof knows it is not theirs.

**The same check, on the declaration.** `core/harness/bin/gate-check .` → `0
failure(s), 2 warning(s)`, both the expected `gate.requirement-unproven`.
`gate-check --prove .` → **3 failures** — `gate.nonzero`, and both proofs
`proof-missing`. I attributed that too, in a throwaway worktree at clean
`d4d5128`, and got **the identical 3 failures with the same names**. It is the one
failure above and nothing else: the summary line reads `1 failed, 972 passed`, and
a line with a failure does not match `^=+ (?!.*skipped)([0-9]+) passed`.

**Also run, and passing:** `bash tests/gate_self_test.sh` → 20 of 20. *"3 controls
green, 13 breakages went red and each named the finding it was written for, 3
warnings stayed green with their exit code at 0, 1 hygiene case passed. 0
skipped."* See §5 for why that one was red first.

**Every command was bounded by `timeout`.**

---

## 2. The real defect was not the one I was sent

The packet said `tests/test_compose.py` was load-bearing and asked me to find every
assertion there that reads a dependency name. **There are none.** `test_compose.py`
reads `docker-compose.yml`, `kit.ref` and `migrations/` — it never opens
`cafaye.yml`:

```
$ grep -n 'MANIFEST\|cafaye.yml' tests/test_compose.py
  (no output)
```

All 8 of its `guard` occurrences are the tripwire sense — *"a guard that cannot fail
is not a guard"*, *"the guard that can pass by finding nothing"*. The file is
excellent and it is about a different file.

**The assertion was in `tests/test_openapi.py`**, two tests, and this is where the
packet's real instruction — *"an assertion that would pass with a stale name by
accident"* — bites.

### 2.1 I measured the mechanism before I trusted my reading of it

My first hypothesis was that the set-membership form was blind to a revert, so I
applied the mutation the packet suggests and watched it:

```
manifest: gateway -> guard, and the membership set taught `gateway`
  (both mutations printed in the files first, not inferred from an exit code)

1 failed, 1 passed
```

**The membership test failed.** `assert 'guard' in {'gateway', 'identity'}` — so
that shape is *not* blind in the way a membership test usually is, and I was wrong
about the mechanism. Recorded because a report that quietly dropped the wrong
hypothesis would have been indistinguishable from one that never had it.

### 2.2 The actual defect is one level up, and it is worse

```python
def test_every_declared_dependency_is_a_service_and_not_a_package() -> None:
    for dependency in manifest()["dependencies"]:
        assert dependency["name"] in {"identity", "guard"}
```

The expectation is defined **three lines above the loop that consults it**. So the
assertion is satisfied by *agreement* and can never be satisfied by *correctness*:
rename every reference in the fleet to a name that resolves to nothing, edit that
set in the same commit, and it is green. That is not a hypothetical — it is exactly
how `guard` sat on master for the length of a fleet-wide rename with its own test
agreeing with it.

**And nothing else in the fleet checks that line at all.** I looked rather than
assumed:

- `kit`'s `tests/fleet_check.py` — the only cross-repo gate that reads this
  manifest — greps the whole fleet for `dependencies` and finds **no validator for
  a dependency name**. It reads the manifest for the shared cluster and for stale
  container copies.
- `core`'s `fleet.yml` is an **event registry**, and its own header says so: *"NOT
  HERE: which repositories exist. That is `caf`'s business."*
- `pantry` is the resolver, and the packet notes a rename worker is live there.

So `tests/test_openapi.py` held the only check in the fleet on a line that
resolves against a registry outside every repository, and it could not be made
into a real check without being edited in the same commit as the drift it is meant
to catch.

### 2.3 Three assertions now, in increasing order of what they can see

| test | what it can fail on |
|---|---|
| `..._declares_exactly_the_services_it_runs_behind` | the manifest vs a pin in this file. Compared as an ordered **list**, so a duplicate is visible — compare sets and a name listed twice collapses into one element |
| `..._is_a_service_and_not_a_package` | a dependency that is really a distribution. **Derived** from `pyproject.toml` instead of retyping the manifest's own fact — it reuses `test_dependencies._declared()` rather than parsing `pyproject.toml` a second time, because two parsers for one file is one of them a copy that rots |
| `..._resolves_in_the_fleet_registry` | **a name this repository never chose.** Reads `core`'s `fleet.yml` through the `MUSE_CORE_SCHEMAS` checkout the other cross-repo guards already use |

The third is the one that could have caught this packet, and I have written down its
**limit** rather than leaving it to be discovered: `fleet.yml` records the services
that *publish events*, so a future dependency on a non-publisher (`kit`, `caf`)
fails there on a perfectly good name. The failure message names both readings
instead of guessing, because guessing in the other direction is how a real rename
gets waved through as a registry gap. It also asserts its own floor before its
absence — `len(recorded) >= len(FLEET_SERVICES_MUSE_RUNS_BEHIND)` — so a
`fleet.yml` that lost its rows fails by name rather than making the check below it
vacuously true.

I did **not** add a third cross-document assertion tying `cafaye.yml`'s dependency
names to `openapi/v1.yaml`'s prose. It would catch a half-done rename, but it is a
check between two documents over a string, which is precisely the class muse's own
`test_tenant_scoping.py:401` argues against — *"a guard that fires on prose is a
guard that gets deleted on the first day it is inconvenient."*

### 2.4 The proof that they bite

Manifest reverted to `guard`, nothing else changed, mutation printed in the file
first:

```
$ grep -n '^  - name: guard$' cafaye.yml
126:  - name: guard
$ python -c "...print parsed deps"
parsed deps: ['identity', 'guard']
```

All three red, each with a message that names the fact rather than the difference:

```
AssertionError: cafaye.yml declares ['identity', 'guard']; this repository runs
behind ['identity', 'gateway']. Add a dependency here AND in the pin above, in
the same commit …

AssertionError: ['guard'] — core's fleet.yml records ['billing', 'courier',
'gateway', 'identity', 'muse'], and a dependency `caf dev` and `pantry` cannot
resolve is one no registry holds. Two readings, and this message refuses to pick
for you …

AssertionError: assert {'identity': ...guard': False} == {'identity': ...teway': False}
3 failed
```

**3 of 3**, so none of the three is a guard that cannot fail.

---

## 3. Every occurrence, classified, and the totals reconcile

`guard` in this repository means two things: **the service**, and **a tripwire** —
an assertion, used the way "test" is used. At the branch head:

```
186 occurrences at d4d5128
 − 46 renamed away
 + 69 added on purpose
 = 209 measured
```

The 69 are all deliberate, and every one is accounted for: **39** in this report,
**12** in the `CHANGELOG.md` entry, **4** in the comments in
`tests/test_openapi.py` that explain why the old shape was circular, and one each
in `AGENTS.md`, `cafaye.yml`, `src/muse/main.py` and `gate.yml`. A rename that
leaves no trace of what it renamed is a rename whose next reader cannot check, so
the count going **up** is the intended direction and not a leak — the same reason
`REPORT-gateway-rename-guard-01.md` and the `docs` sweep report both quote the old
name throughout.

**Reconciled two ways, by a script whose own assertions caught it twice.** The
first version classified by regex and its "tripwire" pattern was `guard|Guard`,
which matches everything — the service bucket was structurally unreachable and the
assertion designed to catch exactly that fired. The second version keyed on
`path:line:text` and reported 31 lines "vanishing", every one a line whose *number*
moved in a file the sweep had edited. The third keys on content and cross-checks
two independent methods:

```
=== muse, reconciling d4d5128 -> HEAD ===
   169  distinct lines matching /guard/i at d4d5128
    46  RENAMED — absent from HEAD by content
    46  removed lines the diff reports   <-- must equal the above
   123  still present verbatim at HEAD

  at HEAD, 183 distinct lines match, bucketed:
       6  deliberate: names the retired name to EXPLAIN the rename
     67  history: a REPORT-* a dispatched packet wrote   (28 before + this report's 39)
     32  history: CHANGELOG.md records what was true    (21 before + this entry's 12)
     78  left alone: the tripwire sense, each one read in context
   183  total surviving
```

(`169`/`183` are distinct `(path, text)` pairs. `git grep -c` counts 174 and 220
raw lines, because five lines are byte-identical duplicates within a file.)

### Changed: 46 lines, 46 occurrences, 11 files

Counted from the diff, not tallied by hand.

| lines | file | what |
|---|---|---|
| 14 | `tests/test_auth.py` | **every occurrence is the service**, including the `aud="guard"` fixtures |
| 8 | `cafaye.yml` | 7 in prose, 1 the dependency itself |
| 6 | `src/muse/auth.py` | the middleware's `scope`, the RS256 pin, `refusalOf` |
| 4 | `tests/test_openapi.py` | the two assertions' own text and names |
| 4 | `AGENTS.md` | rules 4, 20 (×2) and the core-parity floor paragraph |
| 3 | `src/muse/main.py` | one of them the capital-`Guard` variant — §4.1 |
| 2 | `README.md` | the two places it names the service |
| 2 | `openapi/v1.yaml` | the security-scheme description and a header comment |
| 1 each | `src/muse/jwks.py`, `tests/support/jwks.py`, `tests/test_api.py` | the 300s TTL, a path claim, the trace-id propagation |

### Left alone: 123 lines verbatim, plus 60 added by this packet

| lines | bucket | why |
|---|---|---|
| 78 | **the tripwire** — the other meaning of the word | `"a guard that cannot find its target is not a guard"` is a rule about tripwires, and it is the rule this sweep was working under. Renaming any of these deletes a sentence |
| 28 | `REPORT-*` — history | a dispatched packet's own record |
| 21 | `CHANGELOG.md` — history | records what was true |
| 5 | **the retired name, named on purpose** | the comments in `cafaye.yml`, `src/muse/main.py` and `tests/test_openapi.py` that explain this rename |

The largest single cluster of survivors is `tests/test_tenant_scoping.py` at 14,
**all 14 tripwires**, including two capitalised section headings — `# Guard 1 —` and
`# Guard 2 —`. And `tests/test_migrations.py:143` is a *test name*,
`test_every_table_is_created_guarded`, where "guarded" means `IF NOT EXISTS`.
Renaming that would have been the packet corrupting its own tripwire.

**Say so if you are counting.** `git grep -oi guard | wc -l` reads **209** at the
head, up from 186, and a reader who assumes the sweep was incomplete is wrong
about **123** of the lines that were already there — plus all 60 this packet added
on purpose. That is the packet's own instruction and this is the number to quote
alongside it.

---

## 4. The three blind spots, and what each one found here

### 4.1 Case variants — exactly one, and it sat under a renamed quote

`git grep -cE '\b(GUARD|Guard)\b'` returned **nothing and exited 1**, because git's
ERE has no `\b` — the fleet's recorded lie. Re-run without `\b`, and the tree has
**8** capitalised occurrences. One is the service:

```python
# src/muse/main.py, before
#: The `aud` a token must carry to be for this service — muse's own client id. Guard
#: calls it "guard's own client id" for the same reason: a resource server's audience
```

A lowercase replacement cannot match a capitalised subject. **And it sat on the line
above a quoted string the earlier sweep had already renamed to "gateway's own client
id"** — two files in one repository quoting one sentence differently, on adjacent
lines, with neither wrong on its own. Verified rather than assumed against
`gateway/src/index.test.ts:278`, which names it *"gateway's own client id"*.
`src/muse/auth.py:216` was already correct: the sweep renamed the quote and left
the sentence's subject.

### 4.2 Derived text — a clean sweep, and here is why that is a finding

The gateway service's `$10` → `$12` RESP length is the case that makes this blind
spot famous. In muse there is **no derived text**, and I looked for it rather than
assuming: every numeric literal on a line mentioning the old name; every
`len(...) == N` in the suite; `SERVICE_NAME_PATTERN`'s bounds; the length of the
name itself (`guard` is 5 bytes, `gateway` is 7, and nothing in muse measures the
length of a *service* name — `validate_service_name` is applied only to muse's own
`muse`).

What exists instead is the **false friend**: `tests/test_providers.py:983` and
`tests/test_router.py:225` already read `"gateway timeout after 30s"`, which is the
HTTP 504 phrase and has nothing to do with this service. Correct as-is, untouched,
and worth naming so the next sweeper does not "fix" them.

### 4.3 My own tooling lying — three times, each caught by an assertion

Not one. My reconcile script was wrong twice and my first hypothesis about the
membership test was wrong once. All three are in §2.1, §3 and above. The pattern
is the fleet's own: a check whose own emptiness or whose own reachability is
unasserted is not a check.

---

## 5. A defect I caused, found and fixed

Raising `core-parity` 968 → 973 took `tests/gate_self_test.sh` red with it —
**1 of 20 cases, the CONTROL**:

```
FAIL gate_self_test: the control — an unmodified declaration, proved and static —
  is green in both phases — expected exit 0, got 1
FAIL gate.floor: proof 'core-parity' reported 907 and the declaration's floor is 973
```

Not pre-existing, not a flake. Five call sites installed a stub gate printing a
hardcoded `907 passed` — a figure from the muse-08 era — and then asserted the
result against the **real** `gate.yml` the copy carried. So the control's green
depended on a literal in the test equalling a number in the declaration, and
`gate.yml`'s own rule says that floor moves in the same commit as the suite. Every
future floor raise would have done this again, which is a self-test that trains its
reader to re-baseline the stub instead of reading the failure.

`parity_floor()` now reads the `minimum:` belonging to `- id: core-parity` out of
`gate.yml`, and `parity_summary()` prints it. The `floor` breakage still goes red
and for the right reason: it edits `minimum: 890` → `999` in the throwaway copy
while the stub prints the real floor.

Proven able to fail, mutation verified in the file by printing lines 231-233:

```
derivation intact      -> 20 of 20, 0 skipped
parity_floor mutated   -> 1 of 20, "reported 968 and the declaration's floor is 973"
```

Reverted from a byte copy; `git diff --stat` empty after.

---

## 6. What I deliberately did not do

- **Did not touch another repository.** `core`, `kit`, `guard`/`gateway`, `docs` and
  the rest were **read** to verify names and claims, never written to.
- **Did not rename the GitHub repository.** It is `cafaye/gateway` already
  (`core/fleet.yml`: `repository: git@github.com:cafaye/gateway.git`). Note for a
  reader: the *directory* on this machine is still `cafaye/guard/`, so
  `gateway/src/middleware/jwt.ts` in prose resolves through a checkout called
  `guard`. Correct, and nobody's to move from here.
- **Did not rewrite history.** 21 `CHANGELOG.md` lines and 28 `REPORT-*` lines stay.
- **Did not run a blind `sed`.** Each file was opened and every occurrence read in
  context first; six files took a `perl -pi` scoped to that file **because** all of
  their occurrences were classified as the service — `\bguard\b` cannot touch
  `guarded` or `guards` — and each diff was read back, which is what caught the one
  sentence the rename had to *create* a word for: `test_auth.py` read *"an array is
  a token guard itself would not parse"*, where `guard` was doing double duty as
  English — *a guard* would not parse. Renamed literally it reads *"a token gateway
  itself would not parse"*, which is not English, because `gateway` is a proper noun
  and `guard` was not. Now *"a token the gateway itself would not parse"*. That is
  the collision in the direction people forget: the two senses overlapped inside one
  sentence, so the rename had to add a word the sentence did not have.
- **Did not weaken an assertion to make a rename pass.** The one assertion I
  *changed shape* (`...is_a_service_and_not_a_package`) was made **stricter**, from
  a membership test that could only catch a typo to a derived set difference that
  catches a package.
- **Did not re-vendoring core's schema.** §1. Core's call.
- **Did not add a redirect, a dependency, or a skip** beyond the one core-gated test
  the resolution check needs — and that one is declared, named in `gate.yml` and
  AGENTS.md rule 3, counted in every summary in this report, and carries its limit in
  its own docstring.
- **Did not run** the Docker tiers (`docker build`, `docker compose`). The machine is
  shared and the packet scopes containers out. Named as not run, not skipped. What
  closes the gap: **the two sweep commits add and delete no file at all** —
  `a6ad2c2` is 5 files and `028de2c` is 8, all `M`, zero `A`/`D`/`R` — so
  `test_compose.py`'s Dockerfile copy-chain assertion and its migration-filename
  assertions cannot have gone stale, because no test file and no migration was
  added. The packet as a whole adds exactly one file, this report.
- **Did not add a check between `cafaye.yml` and `openapi/v1.yaml`.** §2.3.

---

## 7. Found outside the packet — named, not fixed

1. **Two `docs` pages now carry a drift note that is no longer true**, and they are
   the check on this work. `docs/src/content/docs/architecture/topology.md:440` —
   *"**`muse`'s manifest has not caught up with the rename:** it still carries
   `dependencies[].name: guard` … it is the one name on this page that a sibling
   repository still spells the old way."* And
   `docs/src/content/docs/runbooks/service-down.md:109` — *"**`muse`'s own manifest
   still declares a dependency named `guard`** … the rename has not reached it."*
   **As of this branch both sentences are false and `docs` should delete them** —
   not reword them, because the drift they report is the thing this packet fixed.
   Meanwhile `topology.md:434` — *"`muse` declares `identity` and `gateway` as
   dependencies"* — is now true as a fact rather than as an intention.
2. **`core`'s vendored-schema drift**, §1. Four resource attributes; muse's copy is
   203 lines to core's 207. This makes `gate-check --prove .` red on this machine
   and will make CI's `gate` job red for the same reason. **Whoever merges this
   should know that before the run goes red there.**
3. **`muse/auth.py`'s `auth.py:216` and `main.py:81` quoted one sentence two ways**
   before this sweep. §4.1. Fixed here, but the generalisable finding is that the
   earlier fleet sweep renamed **inside** a quoted string and not **beside** it, on
   adjacent lines, and a per-occurrence review is the only thing that sees that.
4. **`kit`'s `fleet_check.py` has no check on `dependencies[].name`**, and `core`'s
   `fleet.yml` is by design not a service registry. So the fleet has no shared place
   where "this manifest names a service that does not exist" is caught once for all
   nine repositories. The check I added is per-repository and needs a core checkout;
   a fleet-wide one belongs in `kit` or `caf`. **That is the successor's first real
   task**, and it is the only place this defect class gets caught by construction
   rather than by a sweep remembering to visit every repository.

---

## 8. What I would do next

1. Merge, then open the `docs` patch in §7 item 1 — two deletions.
2. Fix `core`'s vendored schema drift, or re-vendor muse's copy, so the core-parity
   tier is demonstrably green and the `973` floor is verifiable rather than argued.
3. Put the dependency-name check in `kit` or `caf`, where one implementation covers
   every repository and a missed sweep is caught by construction.
4. If a `cafaye` service is ever renamed again, the two blind spots are the whole
   lesson: grep case-insensitively **and** for capitalised subjects, and read the
   diff for numbers rather than trusting a token count. A renamed service that also
   changes the length of a string it appears in is a test that silently stops
   testing.

Nothing pushed. Nothing merged. No tag. No container, image or volume left running;
the one throwaway worktree I created for the §1 attribution was removed and
`git worktree list` shows 2 entries.