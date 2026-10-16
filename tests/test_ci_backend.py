"""``ci/backend`` and ``ci/pipeline.sh``, read against kit's backend contract.

``kit/backends/README.md`` is the contract: the one line in ``ci/backend``
selects the runner that calls ``ci/pipeline.sh``, and a name kit does not ship
is a declaration nothing reads — the file looks configured while no backend
matches it.

``KNOWN_BACKENDS`` is a TRANSCRIPTION of kit's contract — the table in
``kit/backends/README.md`` and the directories under ``kit/backends/`` — not a
copy of anything in this repository. It is a reason to FAIL a name kit does not
ship; when kit adds a backend, the entry lands here in the same commit that
adopts it here.

No socket, no subprocess: three facts read out of two files (AGENTS.md rule 3).
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parent.parent
BACKEND = ROOT / "ci" / "backend"
PIPELINE = ROOT / "ci" / "pipeline.sh"

#: The backends ``kit/backends/README.md`` declares and kit ships.
KNOWN_BACKENDS = frozenset({"github-actions", "instabox"})


def test_ci_backend_names_a_backend_kit_ships() -> None:
    """A name kit does not ship is a runner that never picks the repository up."""
    declared = BACKEND.read_text(encoding="utf-8").strip()
    assert declared in KNOWN_BACKENDS, (
        f"ci/backend names {declared!r}, which kit does not ship. "
        "kit/backends/README.md's table is the contract; a name outside it is a "
        "declaration no backend reads."
    )


def test_ci_backend_names_exactly_one_backend() -> None:
    """The README says "change the one line in `ci/backend`", so the file is one
    token and a newline — not a token plus a comment a backend would read as a
    second word."""
    raw = BACKEND.read_text(encoding="utf-8")
    assert raw == raw.strip() + "\n"
    assert " " not in raw.strip()


def test_pipeline_exists_and_is_executable() -> None:
    """The contract's first clause is that the pipeline be executable."""
    assert PIPELINE.is_file(), "ci/pipeline.sh is missing; the backend calls it"
    assert PIPELINE.stat().st_mode & 0o111, (
        "ci/pipeline.sh has no executable bit; the contract's first clause is "
        "that it be executable (chmod +x)."
    )
