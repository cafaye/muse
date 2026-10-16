#!/usr/bin/env bash
# ci/pipeline.sh — muse's pipeline, backend-agnostic.
#
# One pipeline, two runners. The backend's only job is to provide an
# environment and call this script; the gate is this repository's. Which
# backend ran is recorded in `ci/backend`, and switching is one line —
# `github-actions` keeps `.github/workflows/` calling kit's reusable
# workflows, `instabox` provisions a sandbox that polls the repo and runs this
# file. `kit/backends/README.md` is the contract.
#
# WHERE THE FACTS COME FROM: `.github/workflows/ci.yml` IN THIS REPOSITORY.
#
#   * THE GATE is `bin/prime`, with `-q -rs`. The `gate` job runs
#     `bin/prime -q -rs 2>&1 | tee "$RUNNER_TEMP/prime.log"`, and `gate.yml`
#     declares the gate as `[bin/prime]`. `bin/prime` is `uv sync --locked` +
#     ruff + `uv run --locked pytest`, so the pipeline does not restate any of
#     them: a second spelling of the gate is a second thing to keep true.
#
#   * THE CORE TIER IS FORCED ON. This is the half of the `gate` job that makes
#     its green mean something, and it is the whole reason this repository's
#     `gate.yml` exists. With `MUSE_CORE_SCHEMAS` unset the suite is green and
#     reports four skipped tests; those four are the only checks that catch
#     muse's vendored copies of core's specs drifting. The job checks out
#     `cafaye/core` and sets the variable; this pipeline resolves the same core
#     through the repository's own `src/muse/coreloc.py` (the resolution
#     `bin/fetch-core` drives) and exports the same variable, so the parity tier
#     executes on the sandbox as it does on a runner.
#
#   * NO TEST MAY SKIP. The `gate` job's next step greps the teed log and fails
#     the build on any skip, because with the tier forced on a skip means a
#     check nobody can see ran nothing. Same grep, same reason.
#
#   * UV.LOCK DID NOT MOVE. `bin/prime` uses `--locked` on every `uv sync` and
#     `uv run`; the `gate` job's last step is `git diff --exit-code -- uv.lock`,
#     which is the assertion that it stayed true.
#
#   * NO PATH ENTRIES. `grep GITHUB_PATH .github/workflows/*.yml` finds none;
#     the workflow gets its toolchain from `astral-sh/setup-uv` and
#     `actions/setup-python`, which the pins block below reproduces as `mise`.
#
# NOT REPLICATED HERE, and named so nobody wonders: the `pins` job (a static
# read of `.python-version`, `mise.toml` and this workflow — offline, seconds,
# and about the workflow rather than about the tree this pipeline runs) and the
# `gate` job's "the parity guard can go red" step (a mutation proof that the
# parity test FAILS on drift, which is a proof about the guard and not part of a
# green gate). Both belong to CI's own self-check and neither changes what a
# green pipeline means.
#
# WHAT IT NEEDS FROM THE ENVIRONMENT (backends/README.md is the contract):
#   WORKSPACE   the directory holding cafaye/<name> checkouts
#   GH_TOKEN    a token that can read this project's private sources
#   CI          1, so nothing waits on a prompt
# A cafaye/core checkout resolvable beside the workspace (CORE_PATH /
# CORE_SPECS_PATH, `../core`, or core beside the primary worktree) is what the
# filesystem requirement in `gate.yml` names; `bin/fetch-core` is how it is got.
set -euo pipefail

# ---------------------------------------------------------------------------
# identity — the repository root comes from this file, not the caller's cwd
# ---------------------------------------------------------------------------
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
REPO_NAME="$(basename "$REPO_DIR")"

WORKSPACE="${WORKSPACE:-$(cd "$REPO_DIR/../.." && pwd)}"
BRANCH="${CI_BRANCH:-master}"
export CI="${CI:-1}"
export WORKSPACE

log() { printf '\n== %s\n' "$1"; }

log "pipeline: $REPO_NAME at $REPO_DIR"
if git rev-parse --git-dir >/dev/null 2>&1; then
  git fetch --quiet origin "$BRANCH" 2>/dev/null || true
  git reset --hard FETCH_HEAD --quiet 2>/dev/null || true
fi

# ===========================================================================
# MARKED 1 — THE GATE. `bin/prime -q -rs`, the argv `.github/workflows/ci.yml`'s
# `gate` job runs. A list, so a multi-word gate works without eval and nothing
# written here is re-parsed.
# ===========================================================================
GATE=(./bin/prime -q -rs)

# ===========================================================================
# MARKED 2 — PATHS THIS REPOSITORY'S CI ADDS. `grep GITHUB_PATH
# .github/workflows/*.yml` finds none. Empty on purpose.
# ===========================================================================
PATHS=()

if [ "${#PATHS[@]}" -gt 0 ]; then
  for d in "${PATHS[@]}"; do
    export PATH="$d:$PATH"
  done
fi

log "the toolchain the repository pins"
# `mise install` materializes the versions in mise.toml (CPython 3.14, uv
# 0.12.20) — the same two pins `astral-sh/setup-uv` and `actions/setup-python`
# install on a runner — and `mise exec` runs the gate under them. When there is
# no mise, the gate runs on whatever this machine ships, which is reported
# rather than hidden.
#
# A function rather than the template's `GATE=(mise exec -- "${GATE[@]}")`
# because the core resolution below needs the same prefix and macOS ships bash
# 3.2, where `"${EMPTY[@]}"` under `set -u` is an unbound-variable error. The
# guarded branch is the only place the array is expanded.
MISE=()
if [ -f mise.toml ] && command -v mise >/dev/null 2>&1; then
  mise install
  MISE=(mise exec --)
elif [ -f mise.toml ]; then
  echo "pipeline: mise.toml is present and mise is NOT on PATH — the gate runs on the machine's own toolchain"
fi
under_mise() {
  if [ "${#MISE[@]}" -gt 0 ]; then
    "${MISE[@]}" "$@"
  else
    "$@"
  fi
}

# ---------------------------------------------------------------------------
# resolve core and force the parity tier on (the `gate` job's env + checkout)
# ---------------------------------------------------------------------------
# `uv run --locked`, never a bare `uv run`: a bare one re-resolves and can
# rewrite `uv.lock` on the way here, which is the gate's own defect (bin/prime's
# header). `coreloc.fetch` walks CORE_PATH/CORE_SPECS_PATH, `../core`, then
# core beside the primary worktree, and clones only as the explicit last
# fallback — see `src/muse/coreloc.py`.
log "the core parity tier"
if [ -z "${MUSE_CORE_SCHEMAS:-}" ]; then
  CORE_ROOT="$(under_mise uv run --locked python -c \
    'from muse.coreloc import fetch, repository_root; print(fetch(repository_root()))')"
  export MUSE_CORE_SCHEMAS="$CORE_ROOT/schemas"
fi
echo "pipeline: MUSE_CORE_SCHEMAS=$MUSE_CORE_SCHEMAS"
if [ ! -d "$MUSE_CORE_SCHEMAS" ]; then
  echo "pipeline: no core schemas at $MUSE_CORE_SCHEMAS. The four parity tests would skip, so the run is failed here instead. Set CORE_PATH, or put core beside the primary worktree — bin/fetch-core walks the order." >&2
  exit 1
fi

log "the gate"
# Tee the whole log so the skip guard below can read it, and rely on the
# `pipefail` in `set -euo pipefail` to keep a red suite red through the pipe.
LOG="$(mktemp "${TMPDIR:-/tmp}/prime-${REPO_NAME}.XXXXXX.log")"
under_mise "${GATE[@]}" 2>&1 | tee "$LOG"

log "no test may skip"
# The load-bearing check, copied from the `gate` job. With the parity tier
# forced on, a skip is a claim that has quietly stopped being checked (PLAN §3).
# Blanket rather than a per-test allowlist: a list is a list that grows.
if [ ! -s "$LOG" ]; then
  echo "pipeline: no gate log at $LOG — the skip guard read nothing, which is not a pass." >&2
  exit 1
fi
if grep -nE '^(SKIPPED )|[0-9]+ skipped' "$LOG"; then
  echo "pipeline: a test skipped while MUSE_CORE_SCHEMAS is set, so a tier nobody can see ran nothing." >&2
  exit 1
fi

log "uv.lock did not move"
git diff --exit-code -- uv.lock

# ===========================================================================
# MARKED 3 — SHIP. Deleted on purpose: muse ships no chart (`chart/Chart.yaml`
# is absent) and has no `.github/workflows/publish.yml`, so there is nothing to
# publish from this pipeline. A service that gains a chart copies the template's
# image+chart block back in and takes `bin/publish` from kit
# (`backends/README.md` step 2). Never reimplement the push.
# ===========================================================================

# ---------------------------------------------------------------------------
# the last line a log tail reads
# ---------------------------------------------------------------------------
COMMIT="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
log "pipeline: passed $REPO_NAME at $COMMIT (nothing published)"
