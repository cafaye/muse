#!/usr/bin/env bash
#
# gate_self_test.sh — the proof that gate.yml is a gate and not a note.
#
#   bash tests/gate_self_test.sh
#
# WHAT THIS IS FOR
#
# A declaration that has only ever been green is a comment with a YAML
# extension. This script takes a copy of this repository's gate.yml, breaks
# exactly one thing in it at a time, and asserts core's checker goes RED and
# NAMES the finding it expects. Naming matters: "something went red" is a
# weak claim when a dozen checks can go red, and the check written for a
# specific defect can be dead code forever while the suite stays green.
#
# The shape is kit's tests/self_test.sh, for kit's reason: a control on the
# unmodified declaration FIRST, a fresh throwaway copy per breakage (one must
# never mask the next), and a non-zero exit if any breakage stayed green.
#
# WHAT IS COPIED, AND WHY IT IS NOT THE WHOLE TREE
#
# gate_check.py reads four things: gate.yml, mise.toml, the entrypoint file,
# and the CI workflow. It runs gate.command with the copy as its working
# directory. So a copy of those four paths IS a repository as far as the
# checker is concerned, and copying the rest — .venv above all — would buy
# nothing and cost a great deal. Nothing in the committed tree is a
# deliberately broken repository: the breakages are diffs applied to
# throwaway copies, so a reviewer reads what is being broken rather than
# having to reconstruct it.
#
# THE STUB GATE, STATED PLAINLY BECAUSE IT MATTERS
#
# The `--prove` breakages below need the checker to RUN a gate. They run a
# four-line stub that prints the summary lines muse's real `bin/prime` prints,
# captured verbatim from an actual run:
#
#     ============================= 907 passed in 102.01s ============================
#     Required test coverage of 100% reached. Total coverage: 100.00%
#
# The stub exists so that thirteen breakages do not mean thirteen real gate
# runs (each is a `uv sync --locked` plus a 907-test suite; the brief's own
# rule is no sleeps, and a self-test whose runtime is thirteen suites is a
# self-test nobody runs). It is NOT evidence that muse's gate works. That is
# the other
# half of this packet, run separately and recorded in REPORT-core-10-muse.md:
# `harness/bin/gate-check --prove .` against the REAL bin/prime, in both
# directions. Read that report for the real counts; read this file for the
# proof that the declaration can fail.
#
# THE ONE THAT MATTERS MOST IS THE LAST BREAKAGE
#
# Twelve of these are a checker reading two files and disagreeing. The last is
# the run this declaration exists to refuse: the gate is real, it ran, it
# exited 0, and its summary says `904 passed, 3 skipped` — three tests that
# never executed because they need a cafaye/core checkout. Nothing about the
# declaration is wrong. The count is ABOVE the suite floor, so the suite
# proof passes. Only `core-parity` catches it. That is the identity defect
# this whole format was written for, reproduced on purpose, and it is the one
# case where deleting one block from gate.yml would turn a red into a green
# with no other finding changing. It is the case immediately followed by its
# own control, which runs the identical gate with that block deleted and
# asserts it goes GREEN — the cost of the deletion, measured rather than
# claimed.

set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"

# --------------------------------------------------------------------------
# Locate core's checker. There is no copy in this repository on purpose:
# `gate_check.py` travels with core's `harness/cafaye_contract.py`, and a
# vendored copy of one without the other would be a second YAML dialect.
#
# A checker that could not be found exits 2 and never 0. A missing checkout is
# not a clean bill of health, it is an unknown, and this script reporting
# "all breakages caught" because it proved nothing is the exact failure mode
# the whole packet exists to remove.
# --------------------------------------------------------------------------
CHECKER="${CAFAYE_GATE_CHECK:-}"
if [ -z "$CHECKER" ]; then
  for candidate in "$ROOT/../core/harness/gate_check.py" "$ROOT/../../core/harness/gate_check.py"; do
    if [ -r "$candidate" ]; then CHECKER="$candidate"; break; fi
  done
fi

if [ -z "$CHECKER" ] || [ ! -r "$CHECKER" ]; then
  echo "gate_self_test: core's harness/gate_check.py was not found." >&2
  echo "  This script proves that gate.yml can fail, and that is impossible" >&2
  echo "  without the checker. Point CAFAYE_GATE_CHECK at the file, or check" >&2
  echo "  out cafaye/core next to this repository. Exiting 2: the check could" >&2
  echo "  not happen, which is not the same as having passed." >&2
  exit 2
fi

PY="${CAFAYE_GATE_PYTHON:-}"
if [ -z "$PY" ]; then
  for candidate in python3 python3.14 python3.13 python3.12 python3.11 python; do
    command -v "$candidate" >/dev/null 2>&1 || continue
    PY="$candidate"
    break
  done
fi
if [ -z "$PY" ] || ! "$PY" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 11) else 1)'; then
  echo "gate_self_test: gate_check.py needs python >= 3.11 (tomllib); none found." >&2
  echo "  Set CAFAYE_GATE_PYTHON. Exiting 2, for the same reason as above." >&2
  exit 2
fi

WORK="$(mktemp -d "${TMPDIR:-/tmp}/muse-gate-self-test.XXXXXX")"
trap 'rm -rf "$WORK"' EXIT

# Four counters, not one, because a single total cannot be read: a run with
# "15 passed" could be fifteen reds or ten reds and five controls, and those
# are different claims. The brief's rule is the reason — "N passed" alone is a
# claim nobody can check, and a skipped check is not a passing check.
failures=0
controls=0      # cases that must come back GREEN
breakages=0     # cases that must go RED and name their finding
warn_cases=0    # cases that must warn AND still exit 0
hygiene=0       # the leak case, which is neither of the above
skipped=0       # cases not run; reported as 0, never folded into a pass count
copy_name=""

# fresh_copy <name> — a repository the checker cannot tell from this one.
# `.github` is here and not an afterthought: the workflow is the artifact
# `gate.ci-disagrees` reads by path, so a copy without it would fail on a
# missing file rather than on the defect under test.
fresh_copy() {
  copy_name="$1"
  local dst="$WORK/$copy_name"
  rm -rf "$dst"
  mkdir -p "$dst/.github/workflows" "$dst/bin"
  cp "$ROOT/gate.yml" "$dst/gate.yml"
  cp "$ROOT/mise.toml" "$dst/mise.toml"
  cp "$ROOT/bin/prime" "$dst/bin/prime"
  cp "$ROOT/.github/workflows/ci.yml" "$dst/.github/workflows/ci.yml"
  chmod +x "$dst/bin/prime"
  printf '%s' "$dst"
}

# edit <file> <old> <new> — a textual breakage that FAILS LOUDLY if this
# repository has moved past it. A self-test that silently stops breaking
# anything is worse than no self-test: an unmatched edit means the recipe is
# stale, and a stale recipe that "passes" is the whole class of defect here.
edit() {
  "$PY" - "$1" "$2" "$3" <<'PY'
import sys

path, old, new = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(path, encoding="utf-8").read()
if old not in body:
    sys.exit(f"gate_self_test: breakage no longer applies to {path}: {old!r} not found")
open(path, "w", encoding="utf-8").write(body.replace(old, new, 1))
PY
}

# write <file> <content-on-stdin>
write() { cat > "$1"; }

# strip_ci_block <file> — delete the whole top-level `ci:` block, comments and
# all. A dedicated helper rather than `edit`, because the block carries four
# lines of comment that exist to be edited: an `edit` recipe pinned to them
# would be a second copy of the truth that rots the first time someone
# rewords a comment, and this breakage must keep working after that.
strip_ci_block() {
  "$PY" - "$1" <<'PY'
import sys

path = sys.argv[1]
lines = open(path, encoding="utf-8").read().splitlines(keepends=True)
kept, dropping, seen = [], False, False
for line in lines:
    if not dropping and line.startswith("ci:"):
        dropping, seen = True, True
        continue
    if dropping and line and not line[0].isspace():
        dropping = False
    if not dropping:
        kept.append(line)
if not seen:
    sys.exit(f"gate_self_test: no top-level ci: block in {path}")
open(path, "w", encoding="utf-8").write("".join(kept))
PY
}

# install_stub <dir> <summary-line> — the gate the proving breakages run.
# $1 is the summary line to print, so one breakage can print a run with
# skips in it and another a clean one.
install_stub() {
  local dir="$1" summary="$2"
  write "$dir/bin/prime" <<STUB
#!/usr/bin/env bash
# A stand-in for bin/prime, written by tests/gate_self_test.sh. Prints the
# summary lines the real gate prints and nothing else, so the proving
# breakages cost a second each instead of a full suite run.
set -euo pipefail
echo "==> syncing (locked)"
echo "==> lint"
echo "==> tests"
echo '$summary'
echo 'Required test coverage of 100% reached. Total coverage: 100.00%'
STUB
  chmod +x "$dir/bin/prime"
}

# check <dir> [extra args...] — run the checker, capture output and code.
# The exit code is the checker's own, read from the command substitution
# rather than from `$?` of a pipeline: `$?` under `set -o pipefail` here is
# still the last command's, and this fleet's one recorded false green was
# exactly that mistake under zsh.
out=""
code=0
check() {
  local dir="$1"
  shift
  out="$("$PY" "$CHECKER" --log-dir "$dir/.log" "$@" "$dir" 2>&1)" || code=$?
}

# expect_red <label> <dir> <finding-id> [args...]
# The exit code must be 1 AND the report must NAME the finding. Both halves.
expect_red() {
  local label="$1" dir="$2" expect="$3"
  shift 3
  code=0
  check "$dir" "$@"
  if [ "$code" -ne 1 ]; then
    printf 'FAIL gate_self_test: %s — expected exit 1, got %s\n%s\n' "$label" "$code" "$out" >&2
    failures=$((failures + 1))
    return
  fi
  if ! printf '%s' "$out" | grep -qF "$expect"; then
    printf 'FAIL gate_self_test: %s — went red as something else and never said %s\n%s\n' \
      "$label" "$expect" "$out" >&2
    failures=$((failures + 1))
    return
  fi
  breakages=$((breakages + 1))
  printf 'PASS gate_self_test: breakage %s/%s: %s — caught by `%s`\n' \
    "$breakages" "$((breakages + failures))" "$label" "$expect"
}

# expect_green <label> <dir> [args...]
expect_green() {
  local label="$1" dir="$2"
  shift 2
  code=0
  check "$dir" "$@"
  if [ "$code" -ne 0 ]; then
    printf 'FAIL gate_self_test: %s — expected exit 0, got %s\n%s\n' "$label" "$code" "$out" >&2
    failures=$((failures + 1))
    return
  fi
  controls=$((controls + 1))
  printf 'PASS gate_self_test: control %s: %s\n' "$controls" "$label"
}

# expect_green_proving <label> <dir>
# expect_green with `--prove` spelled out, for the cases where the whole claim
# is about the proving phase. Kept as a named helper rather than an inline
# `--prove` argument so that every proving case reads the same way, and so a
# reader can count them.
expect_green_proving() {
  local label="$1" dir="$2"
  shift 2
  expect_green "$label" "$dir" --prove "$@"
}

# expect_warn <label> <dir> <finding-id> [args...]
# The finding must be printed AND the exit code must STILL be 0. The exit
# code is the half that matters: `gate.requirement-unproven` means "this
# machine could not answer that", and a checker that turned it into a failure
# would be red on a laptop and green on CI.
expect_warn() {
  local label="$1" dir="$2" expect="$3"
  shift 3
  code=0
  check "$dir" "$@"
  if [ "$code" -ne 0 ]; then
    printf 'FAIL gate_self_test: %s — a warning moved the exit code to %s\n%s\n' \
      "$label" "$code" "$out" >&2
    failures=$((failures + 1))
    return
  fi
  if ! printf '%s' "$out" | grep -qF "$expect"; then
    printf 'FAIL gate_self_test: %s — exited 0 without even printing %s\n%s\n' \
      "$label" "$expect" "$out" >&2
    failures=$((failures + 1))
    return
  fi
  warn_cases=$((warn_cases + 1))
  printf 'PASS gate_self_test: warning %s: %s — said `%s` and still exited 0\n' \
    "$warn_cases" "$label" "$expect"
}

# expect_no_leak <label> <dir> <value>
# A gate whose output contains a value read from its own environment. The
# checker's REPORT must not carry it; its log must. The report is what a
# person reads and what a CI log keeps forever for people who were not there,
# and no off-the-shelf tool detects this class: 0 of 268 Semgrep rules
# intersect CWE-532, gosec has no ast.CallExpr case, Bandit is ast.Constant
# only. So it is a test here rather than a promise in a comment.
expect_no_leak() {
  local label="$1" dir="$2" secret="$3"
  local report log code
  report="$(DATABASE_URL="$secret" "$PY" "$CHECKER" --prove --log-dir "$dir/.log" "$dir" 2>&1)"
  code=$?
  log="$(cat "$dir/.log/gate.log" 2>/dev/null || true)"
  if [ "$code" -eq 0 ]; then
    printf 'FAIL gate_self_test: %s — the leaky gate was reported green\n' "$label" >&2
    failures=$((failures + 1))
  elif printf '%s' "$report" | grep -qF "$secret"; then
    printf 'FAIL gate_self_test: %s — the checker copied a value out of the gate environment into its own report\n' \
      "$label" >&2
    failures=$((failures + 1))
  elif ! printf '%s' "$log" | grep -qF "$secret"; then
    printf 'FAIL gate_self_test: %s — the gate log should hold the gate output; it does not, so the check above proved nothing\n' \
      "$label" >&2
    failures=$((failures + 1))
  else
    hygiene=$((hygiene + 1))
    printf 'PASS gate_self_test: hygiene %s: %s — the report stayed clean and the log held it\n' \
      "$hygiene" "$label"
  fi
}

# --------------------------------------------------------------------------
# THE CONTROL. Without it, thirteen reds prove nothing at all: a checker that
# refused everything would satisfy every expectation below.
# --------------------------------------------------------------------------
control="$(fresh_copy control)"
install_stub "$control" '============================= 907 passed in 102.01s ============================'
expect_green 'the control — an unmodified declaration, proved and static — is green in both phases' \
  "$control" --prove

# The static phase on its own, with no gate run at all. Separate because the
# two phases can disagree and a self-test that only ever runs the pair cannot
# say which half was load-bearing.
control_static="$(fresh_copy control-static)"
expect_green 'the control — the static half alone — is green and names no failure' "$control_static"

# --------------------------------------------------------------------------
# THE BREAKAGES
# --------------------------------------------------------------------------

one="$(fresh_copy declaration-missing)"
rm -f "$one/gate.yml"
expect_red 'a repository that declares no gate at all' "$one" 'gate.declaration-missing'

b="$(fresh_copy command-missing)"
edit "$b/gate.yml" 'command: [bin/prime]' 'command: [bin/absent]'
expect_red 'a gate command naming a file this repository does not have' "$b" 'gate.command-missing'

b="$(fresh_copy entrypoint-missing)"
edit "$b/gate.yml" 'entrypoint: bin/prime' 'entrypoint: bin/absent'
expect_red 'a gate entrypoint this repository does not have, while the command still does' \
  "$b" 'gate.entrypoint-missing'

b="$(fresh_copy entrypoint-not-executable)"
chmod -x "$b/bin/prime"
expect_red 'a gate nobody is allowed to execute' "$b" 'gate.entrypoint-not-executable'

b="$(fresh_copy task-missing)"
edit "$b/gate.yml" 'miseTask: prime' 'miseTask: verify'
expect_red 'a mise task that is not in this repository'"'"'s mise config' "$b" 'gate.task-missing'

# The brief names this one, and it is the check that makes adding `[tasks]`
# worth doing: the task and the entrypoint are two readings of one gate, and
# this is what catches them disagreeing.
b="$(fresh_copy task-unresolvable)"
edit "$b/mise.toml" 'run = "bin/prime"' 'run = "bin/some-other-file"'
expect_red 'a mise task that resolves to a different file than the entrypoint names' \
  "$b" 'gate.task-unresolvable'

b="$(fresh_copy ci-missing)"
edit "$b/gate.yml" 'workflow: .github/workflows/ci.yml' 'workflow: .github/workflows/nope.yml'
expect_red 'a declaration naming a CI workflow that is not in this repository' "$b" 'gate.ci-missing'

# Also named in the brief. The mutation is not a deleted file — it is a
# workflow that exists, runs, and simply never runs the gate, which is the
# drift `gate.ci-disagrees` exists to catch.
b="$(fresh_copy ci-disagrees)"
edit "$b/.github/workflows/ci.yml" 'bin/prime -q -rs' 'echo "the tests passed"'
expect_red 'a CI workflow that exists, runs, and never invokes the gate' "$b" 'gate.ci-disagrees'

# The proof that does not match anything the gate actually prints. Named in
# the brief, and caught at the SHAPE layer rather than after a full gate run:
# a pattern that will not compile is `gate.schema`, which is the better
# answer, because the alternative is discovering it after spending the
# gate's whole timeoutSeconds.
b="$(fresh_copy proof-uncompilable)"
edit "$b/gate.yml" "match: '^=+ ([0-9]+) passed'" "match: '^=+ ([0-9]+/[0-9]+ passed'"
expect_red 'a proof pattern that does not compile, which would otherwise read as "no proof required"' \
  "$b" 'gate.schema'

# A proof with a floor and no single capture group to read the floor from.
# Only the proving phase can see this one: the declaration is well formed,
# the pattern compiles, and a checker that treated "no group" as "no floor"
# would accept a suite of any size. The stub gate matters here: this finding
# sits BEHIND a successful match, so the gate has to actually print the line
# for the checker's floor-reading to be the thing that refuses.
b="$(fresh_copy proof-unmeasurable)"
install_stub "$b" '============================= 907 passed in 102.01s ============================'
edit "$b/gate.yml" "match: '^=+ ([0-9]+) passed'" "match: '^=+ [0-9]+ passed'"
expect_red 'a proof with a floor and no capture group to read the floor from' \
  "$b" 'gate.proof-invalid' --prove

b="$(fresh_copy floor)"
install_stub "$b" '============================= 907 passed in 102.01s ============================'
edit "$b/gate.yml" 'minimum: 890' 'minimum: 999'
expect_red 'a gate that proves 907 tests where the declaration promised 999' "$b" 'gate.floor' --prove

# A gate that ran, printed its proof, and failed. `gate.proof-missing` on its
# own would accept a gate that printed nothing AND exited zero; this is the
# other half of the contract.
b="$(fresh_copy nonzero)"
write "$b/bin/prime" <<'SH'
#!/usr/bin/env bash
set -euo pipefail
echo '============================= 907 passed in 102.01s ============================'
echo 'FAILED tests/test_vault.py::test_a_thing - AssertionError' >&2
exit 1
SH
chmod +x "$b/bin/prime"
expect_red 'a gate that proved itself and still failed' "$b" 'gate.nonzero' --prove

# ---------------------------------------------------------------- 13 of 13
# THE ONE. A real gate, a well-formed declaration, an exit code of 0, and a
# summary line saying three tests did not run. Every other fact in gate.yml
# is true. The count is ABOVE the suite floor, so `suite` passes. Nothing in
# the static phase sees anything at all. Only `core-parity` refuses, and
# `gate.proof-missing` is a FAILURE, so this run is red.
#
# It is the identity defect — 1430 tests green against an empty database —
# reproduced on purpose, in this repository, against this declaration.
thirteen="$(fresh_copy skipped-tier)"
install_stub "$thirteen" '================== 904 passed, 3 skipped in 136.68s (0:02:15) =================='
expect_red 'a gate that exited 0 with three tests skipped: above the suite floor, and still refused' \
  "$thirteen" 'gate.proof-missing' --prove

# The proof that it is core-parity doing the refusing, and not some other
# finding happening to fire. This is the same run with the core-parity proof
# deleted, and it goes GREEN — which is the finding in its own right: it is
# the exact cost of deleting that one block from gate.yml, measured rather
# than asserted. If the breakage above ever goes red for a different reason,
# this case is what catches it.
b="$(fresh_copy skipped-tier-without-core-parity)"
install_stub "$b" '================== 904 passed, 3 skipped in 136.68s (0:02:15) =================='
"$PY" - "$b/gate.yml" <<'PY'
import re
import sys

path = sys.argv[1]
body = open(path, encoding="utf-8").read()
# Drop the whole core-parity proof entry, and only it. Anchored on the id so
# a reworded comment above it does not silently stop applying — the same
# rule kit's and core's self-tests follow: a stale recipe must not pass.
new, n = re.subn(
    r"\n    - id: core-parity\n(?:      .*\n|      #.*\n)*(?=    #|\nci:|\Z)",
    "\n",
    body,
)
if n != 1:
    sys.exit(f"gate_self_test: expected exactly one core-parity proof to remove, found {n}")
open(path, "w", encoding="utf-8").write(new)
PY
expect_green_proving 'the same skipped run with the core-parity proof deleted — green, because nothing else notices' "$b"

# --------------------------------------------------------------------------
# THE WARNINGS, AND THE PROMISE THEY MAKE
# --------------------------------------------------------------------------
# These three assert the exit code stays 0 while the finding is printed. The
# tri-state contract made mechanical: a warning is a claim this machine could
# not settle, and a checker that laundered it into a failure would be red on a
# laptop and green on CI — the same defect in a new place.

b="$(fresh_copy requirement-unproven)"
expect_warn 'a requirement satisfied by a bare name on PATH, which this checker did not run' \
  "$b" 'gate.requirement-unproven'

b="$(fresh_copy task-unreadable)"
edit "$b/mise.toml" 'run = "bin/prime"' 'run = "bin/prime | tee /dev/null"'
expect_warn 'a mise task whose run string is a pipeline, so only a shell could say what it runs' \
  "$b" 'gate.task-unreadable'

b="$(fresh_copy ci-undeclared)"
strip_ci_block "$b/gate.yml"
expect_warn 'a declaration that says nothing about CI, so nothing checks that CI runs this gate' \
  "$b" 'gate.ci-undeclared'

# --------------------------------------------------------------------------
# THE HYGIENE CASE. Not a finding id, because it is not a finding: it is the
# property that no finding may ever be one.
# --------------------------------------------------------------------------
b="$(fresh_copy secret-output)"
write "$b/bin/prime" <<'SH'
#!/usr/bin/env bash
# A gate that leaks a value out of its own environment into its output, the
# way a failing assertion that formats a connection string does.
set -uo pipefail
echo "could not reach ${DATABASE_URL:-unset}"
echo '============================= 907 passed in 102.01s ============================'
exit 1
SH
chmod +x "$b/bin/prime"
expect_no_leak 'the gate that leaked at runtime' "$b" \
  'postgres://gate:should-never-be-printed@localhost:5432/gate'

# --------------------------------------------------------------------------
printf '\n'
if [ "$failures" -ne 0 ]; then
  printf 'FAIL: gate_self_test — %s of %s cases failed.\n' "$failures" \
    "$((controls + breakages + warn_cases + hygiene + failures))" >&2
  exit 1
fi
# The two "nothing ran" guards, and they are the reason a green line above is
# worth reading. A self-test whose recipes have gone stale reports zero
# breakages and exits 0, which is the exact failure this script exists to
# detect — a checker that has stopped checking, reported as a checker that
# passed. `edit` refusing an unmatched recipe catches it per-case; this
# catches the case where the whole file stopped running.
if [ "$breakages" -eq 0 ] || [ "$controls" -eq 0 ] || [ "$warn_cases" -eq 0 ]; then
  printf 'FAIL: gate_self_test — %s breakages, %s controls, %s warnings ran. A run with\n' \
    "$breakages" "$controls" "$warn_cases" >&2
  printf '  a zero in it proved nothing, not that nothing was wrong.\n' >&2
  exit 1
fi
# Every counter, separately, always. "15 passed" on its own could mean fifteen
# reds or ten reds and five controls; the point of this line is that a reader
# can tell which.
printf 'PASS: gate_self_test — %s controls green, %s breakages went red and each named the finding it was written for, %s warnings stayed green with their exit code at 0, %s hygiene case passed. %s skipped.\n' \
  "$controls" "$breakages" "$warn_cases" "$hygiene" "$skipped"