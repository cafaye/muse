"""``muse.coreloc`` — where core is, from any checkout of this repository.

The module exists because gate.yml used to satisfy its filesystem requirement by
cloning core from GitHub beside whatever checkout asked. That was measured on
this machine on 2026-10-10: the suite itself is environment-independent
(``1061 passed, 4 skipped`` in a worktree and in the main checkout — the four
are the ``MUSE_CORE_SCHEMAS``-gated parity tests, an environment *variable*), so
muse's copy of BRK-4R2's hazard was loud rather than silent: a network clone in a
gate that needs none, aimed at ``../core`` — which, from a worktree, is the
directory every worker's worktree shares.

billing fixed its silent one with a resolution order used by every reader
(``test/support/core_location.rb``, 20 assertions). This is that order here:

1. ``CORE_PATH`` / ``CORE_SPECS_PATH`` — the explicit seam, still first.
2. ``../core`` beside this repository — the main checkout's answer.
3. core beside the **primary worktree**, through ``git worktree list``.

The tests below drive that order against trees built in ``tmp_path`` with a real
``git worktree`` and an injected ``git`` runner, because the third candidate is a
question asked of git and a stand-in that answered it from the filesystem would
prove nothing. The clone fallback is asserted to fire only when all three miss,
and to aim beside the primary worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from muse import coreloc


def git_worktree_list(*paths: str) -> str:
    """The porcelain text a repository holding the named worktrees prints."""
    lines = [f"worktree {path}" for path in paths]
    lines.append("HEAD abcdef1234567890abcdef1234567890abcdef12")
    lines.append("")
    lines.append("branch refs/heads/master")
    return "\n".join(lines) + "\n"


class FakeRun:
    """A ``subprocess.run`` that answers only ``git worktree list``."""

    def __init__(self, porcelain: str, *, fail: bool = False) -> None:
        self.porcelain = porcelain
        self.fail = fail
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[0] != "git" or argv[3:] != ["worktree", "list", "--porcelain"]:
            raise AssertionError(f"coreloc asked git for something else: {argv}")
        if self.fail:
            raise subprocess.CalledProcessError(128, argv)
        return subprocess.CompletedProcess(argv, 0, self.porcelain, "")


def make_core(root: Path) -> Path:
    """A directory that IS core: the event schemas and the manifest."""
    core = root / "core"
    core.joinpath("schemas").mkdir(parents=True)
    core.joinpath("cafaye.yml").write_text("name: core\n", encoding="utf-8")
    return core


def make_repo(tmp_path: Path) -> Path:
    """A directory that looks like a checkout but is NOT core."""
    repo = tmp_path / "wt" / "muse-hz2"
    repo.mkdir(parents=True)
    repo.joinpath("cafaye.yml").write_text("name: muse\n", encoding="utf-8")
    return repo


# --- the identification ------------------------------------------------------


def test_repository_root_is_this_repository():
    # Called by bin/fetch-core at runtime and by nothing else in the suite, so
    # it is asserted here rather than left to a coverage gap: the one line that
    # answers "which checkout is this" is the line wrong __file__ arithmetic
    # would break, and the failure would be a clone aimed at a directory nobody
    # chose.
    root = coreloc.repository_root()
    assert root.joinpath("gate.yml").is_file()
    assert root.joinpath("src", "muse", "coreloc.py").is_file()
    assert root.joinpath("tests", "test_coreloc.py").is_file()


def test_a_directory_with_only_a_manifest_is_not_core(tmp_path):
    # Every service in the fleet has a cafaye.yml, so the workspace directory a
    # failed lookup would otherwise resolve to must be refused.
    almost = tmp_path / "core"
    almost.mkdir()
    almost.joinpath("cafaye.yml").write_text("name: core\n", encoding="utf-8")
    assert coreloc.readable(almost) is False


def test_a_directory_with_only_schemas_is_not_core(tmp_path):
    almost = tmp_path / "core"
    almost.joinpath("schemas").mkdir(parents=True)
    assert coreloc.readable(almost) is False


def test_core_is_the_schemas_and_the_manifest_together(tmp_path):
    assert coreloc.readable(make_core(tmp_path)) is True


# --- the three candidates ----------------------------------------------------


def test_the_explicit_seam_is_the_whole_resolution_when_set(tmp_path):
    repo = make_repo(tmp_path)
    explicit = make_core(tmp_path / "explicit")
    run = FakeRun(git_worktree_list(str(tmp_path / "primary")))
    assert coreloc.checkout(repo, explicit=explicit, run=run) == explicit
    # Git is not even asked: the explicit seam short-circuits before candidate 3
    # could name a primary worktree.
    assert run.calls == []


def test_a_worktree_resolves_core_beside_the_primary_worktree(tmp_path):
    # The measured hazard as a tree: this "repository" has no core sibling, and
    # the one that matters is beside the PRIMARY worktree git names.
    repo = make_repo(tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    core = make_core(primary.parent)
    run = FakeRun(git_worktree_list(str(primary)))
    assert coreloc.checkout(repo, run=run) == core
    assert run.calls, "candidate 3 must actually ask git"


def test_the_main_checkout_resolves_core_beside_itself(tmp_path):
    # The repository root sits BESIDE core in the workspace, so candidate 2
    # answers. Git is still asked once — the candidate LIST names the primary
    # worktree before the walk begins, and one `git worktree list` per
    # resolution is the whole cost of the order. What matters is that the ANSWER
    # is the sibling, not the clone.
    repo = tmp_path / "main" / "muse"
    repo.mkdir(parents=True)
    make_core(repo.parent)
    run = FakeRun(git_worktree_list(str(tmp_path / "primary")))
    assert coreloc.checkout(repo, run=run) == repo.parent / "core"


def test_the_explicit_seam_wins_over_both_implicit_candidates(tmp_path):
    # The order is the fix: with every candidate present, a named path is the
    # one returned, and an implicit candidate winning would be the clone-always
    # behaviour wearing a resolver's clothes.
    repo = make_repo(tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    make_core(primary.parent)
    make_core(repo.parent)
    explicit = make_core(tmp_path / "explicit")
    run = FakeRun(git_worktree_list(str(primary)))
    assert coreloc.checkout(repo, explicit=explicit, run=run) == explicit


def test_a_missing_git_is_a_missing_candidate_not_an_exception(tmp_path):
    # A tarball or a vendored copy is not a git repository at all. Candidate 3
    # answers nothing rather than aborting the resolution, and the clone is what
    # covers that machine.
    repo = make_repo(tmp_path)
    make_core(repo.parent)
    assert coreloc.checkout(repo, run=FakeRun("", fail=True)) == repo.parent / "core"
    # Candidate 2 misses too, so the resolution is None and the CALLER decides —
    # this module's contract is a path or None, never a raise for "core is not
    # on this machine", because a repository cloned on its own has no schemas to
    # check against and answering "no problem" for a document never read is the
    # outcome a contract test exists to prevent.
    assert coreloc.checkout(tmp_path / "nowhere", run=FakeRun("", fail=True)) is None


def test_a_first_line_that_is_not_a_worktree_is_no_answer(tmp_path):
    # Porcelain's first line is `worktree <path>` for the main checkout; a
    # parser that accepts anything would hand back a HEAD sha as a path.
    repo = make_repo(tmp_path)
    make_core(repo.parent)
    assert coreloc.checkout(repo, run=FakeRun("HEAD abc\n")) == repo.parent / "core"
    assert coreloc.primary_worktree(repo, run=FakeRun("HEAD abc\n")) is None


# --- path_for and searched ---------------------------------------------------


def test_path_for_is_the_resolution_or_a_place_to_put_core(tmp_path):
    # Found: the resolution beside the primary worktree. Two directories,
    # because the bare half of this test must not be answered by a core the
    # first half made — that is how the first version of it "found" a directory
    # nobody had put there for it.
    repo = tmp_path / "a" / "wt" / "muse"
    repo.mkdir(parents=True)
    primary = tmp_path / "a" / "primary"
    primary.mkdir()
    make_core(primary.parent)
    run = FakeRun(git_worktree_list(str(primary)))
    assert coreloc.path_for(repo, run=run) == primary.parent / "core"
    # With nothing anywhere, the answer is the first candidate — a directory
    # that does not exist, which is what a caller's rescue needs in order to
    # reach its own skip and name somewhere core could go. None would raise on
    # `None.joinpath` instead, turning "this tier could not check anything" into
    # "this tier is broken".
    bare = tmp_path / "b" / "bare"
    bare.mkdir(parents=True)
    bare_primary = tmp_path / "b" / "primary"
    bare_primary.mkdir()
    resolved = coreloc.path_for(bare, run=FakeRun(git_worktree_list(str(bare_primary))))
    assert resolved == bare.parent / "core"
    assert resolved.is_dir() is False


def test_candidates_are_the_order_the_docstring_claims(tmp_path):
    repo = make_repo(tmp_path)
    primary = tmp_path / "primary"
    primary.mkdir()
    make_core(repo.parent)
    run = FakeRun(git_worktree_list(str(primary)))
    assert coreloc.candidates(repo, run=run) == [
        repo.parent / "core",
        primary.parent / "core",
    ]
    explicit = tmp_path / "explicit"
    assert coreloc.candidates(repo, explicit=explicit, run=run) == [explicit]


# --- the clone fallback ------------------------------------------------------


def test_fetch_returns_the_resolution_without_cloning_when_core_is_there(tmp_path):
    repo = make_repo(tmp_path)
    make_core(repo.parent)
    clone = CloneDouble()
    assert (
        coreloc.fetch(repo, clone=clone, run=FakeRun(git_worktree_list(str(tmp_path / "x"))))
        == repo.parent / "core"
    )
    assert clone.calls == []


def test_fetch_clones_beside_the_primary_worktree_when_nothing_resolves(tmp_path):
    # The worktree case: the clone goes where the main checkout's sibling would
    # be, NOT at the worktree's own ../core — which is the directory every
    # worker's worktree shares, and where the old satisfy argv aimed.
    repo = make_repo(tmp_path)
    primary = tmp_path / "workspace" / "muse"
    primary.mkdir(parents=True)
    destination = primary.parent / "core"
    clone = CloneDouble()
    assert (
        coreloc.fetch(repo, clone=clone, run=FakeRun(git_worktree_list(str(primary))))
        == destination
    )
    assert clone.calls == [["git", "clone", "--depth", "1", coreloc.CLONE_URL, str(destination)]]


def test_fetch_clones_beside_this_repository_when_git_names_no_primary(tmp_path):
    repo = make_repo(tmp_path)
    destination = repo.parent / "core"
    clone = CloneDouble()
    assert coreloc.fetch(repo, clone=clone, run=FakeRun("", fail=True)) == destination
    assert clone.calls == [["git", "clone", "--depth", "1", coreloc.CLONE_URL, str(destination)]]


def test_a_failed_clone_names_the_destination_and_the_seam(tmp_path):
    repo = make_repo(tmp_path)

    def failing_clone(argv, **kwargs):
        raise subprocess.CalledProcessError(128, argv, stderr=b"could not resolve host")

    with pytest.raises(SystemExit) as excinfo:
        coreloc.fetch(
            repo, clone=failing_clone, run=FakeRun(git_worktree_list(str(tmp_path / "x")))
        )
    message = str(excinfo.value)
    assert "could not clone" in message
    assert coreloc.CLONE_URL in message
    assert "CORE_PATH" in message


class CloneDouble:
    """A clone that records the argv and creates nothing."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))


# --- repository_root ---------------------------------------------------------


def test_repository_root_is_the_root_of_this_repository():
    # `parents[2]` is a claim about where this module sits that nothing
    # asserted, and bin/fetch-core is its only caller. It is asserted here
    # rather than by running the script because the failure it guards is
    # quiet in exactly the way this packet is about: `bin/fetch-core` prints
    # whatever path comes back, so a module moved one directory down would
    # resolve a root that is not the root, and the clone fallback would aim
    # beside the wrong checkout with a green gate over it.
    root = coreloc.repository_root()
    assert root.joinpath("pyproject.toml").is_file()
    assert root.joinpath("src", "muse", "coreloc.py").is_file()
    assert root.joinpath("bin", "fetch-core").is_file()
