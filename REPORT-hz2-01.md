# HZ-2 — the sibling-read hazard, measured

The measurement half of HZ-2 (BRK-4R2's family, fleet-wide). Every number below
was read out of a gate run on this machine, in two environments: the worktree
under `/Users/kaka/Code/any/moon/wt/<repo>-<name>` and the main checkout under
`/Users/kaka/Code/any/moon/cafaye/<repo>`. Nothing here is arithmetic.

## The table

| repo | environment | gate | runs | skips | failures |
|---|---|---|---|---|---|
| muse | worktree `muse-hz2` | `./bin/prime` | 1061 passed | 4 skipped | 0 (exit 0) |
| muse | main checkout | `./bin/prime` | 1061 passed | 4 skipped | 0 (exit 0) |
| pantry | worktree `pantry-hz2` | `PANTRY_CAFAYE_ROOT=<cafaye> ./bin/prime` | 177 passed | 1 skipped | 0 (exit 0) |
| pantry | main checkout | `PANTRY_CAFAYE_ROOT=<cafaye> ./bin/prime` | 178 passed | 0 skipped | 0 (exit 0) |
| darkroom | worktree `darkroom-hz2` | `TEST_DATABASE_URL`+`DARKROOM_DATABASE_URL` set, `./bin/prime --db` | see below | see below | 0 |
| darkroom | main checkout | same | 143 passed (top-level `--- PASS:` lines) | 0 skipped | 0 (GATE_EXIT=0) |
| conductor | no worktree exists | `bin/ci` reads no sibling — ruled out by grep, not by a second run | — | — | — |

## What each measurement says

### muse — the hazard is loud, and the numbers prove it

1061 passed / 4 skipped **identically in both environments**. The 4 skips are
the `MUSE_CORE_SCHEMAS`-gated parity tests, and that is an environment
*variable*, resolved the same way from any directory. There is no silent tier:
the defect is in `gate.yml`'s requirement, whose `satisfy` argv is
`git clone --depth 1 git@github.com:cafaye/core.git ../core` — a clone aimed at
a network from a gate that should need none, and one that, run from a worktree,
would put a core checkout in `/Users/kaka/Code/any/moon/wt/` — the directory
every worker's worktree shares. Loud (it is a declaration, not code), slow, and
worktree-unsafe. That is the fix target here; the suite itself is already
environment-independent.

### pantry — the billing family, live

Two sibling reads of `../caf`, and they disagree about it:

1. `bin/prime`'s manifest-lint tier tests `[[ -f ../caf/go.mod ]]` and skips the
   whole tier in the worktree — *even with `PANTRY_CAFAYE_ROOT` pointed at the
   workspace*, because that variable gates the Go tier and this shell tier
   ignores it. The skip prints a message, so it is the honest-skip contract
   working; it is still a tier that ran in one environment and not the other.
2. `cmd/pantry/datastore_declaration_test.go` reads
   `filepath.Join(root, "..", "caf")` and `t.Skipf`s
   `TestTheGeneratedTelemetryPackageIsStillGenerated` in the worktree. That is
   the measured delta: **177 in the worktree, 178 in the main checkout**, one
   skip's worth.

A note on method, recorded because it nearly produced a wrong number: the first
worktree run reported `163 passed / 15 skipped` — but that run executed
*concurrently* with the main-checkout run, and `tests/rls.sh:172` picks its
scratch-cluster port by asking `lsof` who is listening (a known TOCTOU,
`REPORT-pantry-write-narrowing-01` §8.2). The two runs raced, the worktree's
`--serve` lost, and its 16 data-path gates "did not run". Re-run sequentially:
177 passed, 1 skipped, 0 data-path skips. **The concurrency was the measurement
tool's defect, not the repository's** — and it is exactly the class of
environment-dependence this packet is about, one level down.

### darkroom — the read is in the declaration, not the gate

`gate.yml:247`'s database requirement proves itself by running
`docker compose --project-directory . -f ../kit/templates/compose/docker-compose.yml -f ./docker-compose.yml up -d --wait`.
That path is nil from a worktree, so `gate-check --prove` cannot satisfy the
requirement there — loudly, via the checker's own `unmet` path. The gate
*binary* itself reads no sibling: `bin/prime --db` needs two environment
variables and a reachable cluster, both identical in the two environments, and
the runs bore that out. So darkroom's defect is a satisfy argv that names a path
only the main checkout has — the same relative-path defect as BRK-4R2, one file
over, and the fix is the same shape: resolve kit the HZ-1 way (explicit →
`../kit` → kit beside the primary worktree) and leave a fetch at `kit.ref` as
the explicit, commented fallback for a CI runner with no sibling.

### conductor — ruled out, untouched

`bin/ci` runs setup, rubocop, bundler-audit, brakeman, rails test and a seed
step. Grepped `bin/ script/ lib/ config/ Gemfile Rakefile` for `../core`,
`../caf`, `../kit`: no hits. Its citations live in `.md` prose and `cafaye.yml`
comments only, which the brief says to leave. Nothing was changed here.

## What was not changed, and why

- **conductor** — no live sibling read; prose only.
- **`vendir.lock.yml`'s `cp ../core/…` / `git -C ../core rev-parse HEAD`** — a
  documented manual snapshot procedure, not a gate step; the lockfile's own
  header already records that a stale pin is legal and that nothing checks the
  bytes. The instructions will name where to find core (the same order), not
  grow a check.
- **kit, gateway, identity, core** — prose citations, per the brief.
- **muse's 4 `MUSE_CORE_SCHEMAS` skips** — not a defect to fix; they are the
  deliberate skip for a machine with no core, identical in both environments.

## The after numbers — `bin/fetch-core` is the requirement now

Same commit (`7e8fe89`), two environments, `MUSE_CORE_SCHEMAS` unset in both,
nothing in the environment naming core:

| environment | gate | runs | skips | coverage | failures | exit |
|---|---|---|---|---|---|---|
| worktree `muse-hz2` | `./bin/prime` | 1078 passed | 4 | 100.00% | 0 | 0 |
| main checkout | `./bin/prime` | 1078 passed | 4 | 100.00% | 0 | 0 |

1076 of the 1078 are the suite this measurement started from (1061) plus the 15
resolution tests; the four skips are unchanged and are the same four in both
environments. The counts match, so the clone argv is no longer reached on this
machine and the resolution is environment-independent.

### The 100% ceiling caught the fix, which is the finding

The first run over the branch was red:

    src/muse/coreloc.py                    50      1     12      0    98%   73
    FAIL Required test coverage of 100% not reached. Total coverage: 99.96%
    ====================== 1076 passed, 4 skipped in 25.04s ======================

Line 73 is `repository_root()`'s `return Path(__file__).resolve().parents[2]`.
Its only caller is `bin/fetch-core`, a script, so no test reached it — and the
failure it guards is exactly this packet's shape one level quieter: the script
prints whatever path comes back, so a module moved one directory down resolves
a root that is not the root, the clone fallback aims beside the wrong checkout,
and every other test stays green.
`test_repository_root_is_the_root_of_this_repository` closes it.
