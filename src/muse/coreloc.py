"""Where core is, from **any** checkout of this repository.

gate.yml's filesystem requirement used to satisfy itself with

    git clone --depth 1 git@github.com:cafaye/core.git ../core

which is right for a CI runner and wrong in two quieter ways: on this machine
core is already on disk (the workspace's main checkout sits beside it), so the
clone is a network dependency in a gate that should need none; and run from a
worktree it would write a core checkout into the directory every worker's
worktree shares. billing measured the same hazard as a silent skip (BRK-4R2);
muse's copy of it is loud and slow, and the fix is the same resolution order:

1. ``CORE_PATH`` / ``CORE_SPECS_PATH`` — the explicit seam. Unchanged, and
   still first: a variable a person set is the one answer nobody re-litigates.
2. ``../core`` beside this repository — the main checkout's answer, kept first
   among the implicit ones because it is what gate.yml and the documents
   describe.
3. core beside the **primary worktree**, found through ``git worktree list
   --porcelain``. A worktree lives under ``<workspace>/wt/muse-<name>`` and has
   no ``core`` sibling; the main checkout, which does, is the question git
   answers on its first line. This is the new candidate and the whole point:
   a worktree resolves the same core the main checkout does.

A missing core is NOT an exception here — ``fetch`` is the only one that acts,
and it clones at the primary worktree's sibling (never at a worktree's own
``../core``), so a gate run from either environment lands the clone where the
main checkout would have put it. On a machine with core already present, no
clone runs at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

# The two explicit seams, in the order this repository has always read them.
EXPLICIT_VARIABLES = ("CORE_PATH", "CORE_SPECS_PATH")

# Where a clone comes from when there is genuinely nothing on disk — the CI
# runner with no sibling checkout. One URL, depth 1, pinned nowhere: a clone at
# master is a moving target inside a gate, which is why this is the FALLBACK
# and not the path.
CLONE_URL = "git@github.com:cafaye/core.git"

# What makes a directory core rather than another cafaye checkout. A
# `cafaye.yml` on its own is not enough — every service in the fleet has one,
# and the workspace directory this repository was vendored into is exactly
# what a failed lookup would otherwise resolve to. The event schemas are what
# this repository's cross-repo tier reads, so their presence is the
# identification.
SCHEMAS = "schemas"
MANIFEST = "cafaye.yml"


def normalised(path: Path | str) -> Path:
    """The same path with its ``..``s collapsed, and nothing else.

    Lexical, and deliberately not ``Path.resolve()``: resolve would follow
    symlinks, which on this machine turns ``/var/folders/...`` into
    ``/private/var/folders/...`` and makes the printed
    ``MUSE_CORE_SCHEMAS=<path>/schemas`` hint a different string from the one
    the caller typed. ``a/b/../core`` and ``a/core`` are the same directory;
    the resolution should say so once, here, rather than every caller
    discovering it.
    """
    return Path(os.path.normpath(path))


def repository_root() -> Path:
    """This repository's root, from the module's own location."""
    return Path(__file__).resolve().parents[2]


def readable(path: Path) -> bool:
    """Whether ``path`` is a core checkout and not a directory named like one."""
    return path.joinpath(SCHEMAS).is_dir() and path.joinpath(MANIFEST).is_file()


def primary_worktree(root: Path, run=subprocess.run) -> Path | None:
    """The primary worktree of the git repository holding ``root``, or None.

    ``git worktree list --porcelain`` prints ``worktree <path>`` on its first
    line for the main checkout. It is asked of GIT rather than answered from the
    filesystem because the question is "where is the main checkout of THIS
    repository" — a filesystem search would guess at a layout, and ``..`` of a
    bare repository's common dir is the repository itself, which can hold
    anything. A tree that is not a git repository at all answers None, and the
    caller falls through to the clone.
    """
    try:
        completed = run(
            ["git", "-C", str(root), "worktree", "list", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
    except OSError, subprocess.CalledProcessError:
        return None
    first = completed.stdout.splitlines()[0] if completed.stdout else ""
    prefix = "worktree "
    if not first.startswith(prefix):
        return None
    return Path(first[len(prefix) :].strip())


def candidates(root: Path, explicit: str | None = None, run=subprocess.run) -> list[Path]:
    """Every path the resolution walks, in order, with ``explicit`` first."""
    if explicit:
        return [normalised(explicit)]
    found = [normalised(root.joinpath("..", "core"))]
    primary = primary_worktree(root, run=run)
    if primary is not None:
        # `..` and NOT the worktree itself: a worktree is a checkout of THIS
        # repository and carries its own cafaye.yml, so resolving the worktree
        # root would look like a success and hand back a directory with no
        # schemas in it — a skip wearing a found path's clothes.
        found.append(normalised(primary.joinpath("..", "core")))
    return found


def checkout(root: Path, explicit: str | None = None, run=subprocess.run) -> Path | None:
    """The first readable candidate, or None when core is genuinely absent."""
    for candidate in candidates(root, explicit=explicit, run=run):
        if readable(candidate):
            return candidate
    return None


def path_for(root: Path, explicit: str | None = None, run=subprocess.run) -> Path:
    """The resolved core, or — when there is none — somewhere an operator can put it.

    A reader handed None does not skip: it raises on ``None.joinpath``, which
    turns "this tier could not check anything" into "this tier is broken", and
    the deliberate skip becomes unreachable. So this is the resolved core or the
    first candidate: a path that does not exist is exactly what a caller's
    ``except OSError`` needs in order to skip and name somewhere core could go.
    """
    return (
        checkout(root, explicit=explicit, run=run)
        or candidates(root, explicit=explicit, run=run)[0]
    )


def fetch(root: Path, clone=subprocess.run, run=subprocess.run) -> Path:
    """Resolve core, and clone it beside the primary worktree when nothing is there.

    The clone is the explicit fallback for a machine with no sibling checkout,
    and it goes where the main checkout's sibling would be — so a gate run from
    a worktree and one from the main checkout land the clone in the same place
    rather than one worker's worktree directory.
    """
    resolved = checkout(root, run=run)
    if resolved is not None:
        return resolved
    primary = primary_worktree(root, run=run)
    destination = normalised((primary or root).joinpath("..", "core"))
    try:
        clone(
            ["git", "clone", "--depth", "1", CLONE_URL, str(destination)],
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        raise SystemExit(
            f"coreloc: could not clone {CLONE_URL} beside the primary worktree "
            f"({destination}): {error}. Set CORE_PATH to a core checkout, or clone "
            "one beside the main checkout — the resolve order in this module's "
            "docstring walks it without a network."
        ) from error
    return destination
