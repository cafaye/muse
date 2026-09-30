"""What the suite imports must be what `bin/prime` installs.

The gate is `uv sync --frozen` with no extras, so the suite runs against exactly the
distributions in the transitive closure of `[project.dependencies]` plus every
`[dependency-groups]` entry. An import outside that set is a test that passes in
whoever's venv happens to carry the package and fails on every fresh clone. That is
the defect that turned `muse-03`'s gate red at merge: the OTLP exporter lived in the
`otel` extra, the suite required it, and nothing in the gate installed it — so
`muse-03` was green in the worktree it was written in and red everywhere else.

Two assertions, because there are two ways to get this wrong and each needs its own:

1. **every imported module is provided by that closure**, resolved at submodule
   granularity. This is the only check that catches an import behind an extra:
   `opentelemetry` *is* declared, `opentelemetry.exporter.otlp` is not, and a
   top-level comparison cannot tell those apart.
2. **every third-party root is claimed by a direct declaration**, so muse never
   inherits a package because something else happened to pin it. `pydantic` and
   `starlette` were imported by `api.py` and `main.py` while arriving only as
   `fastapi`'s transitive dependencies — which is a latent version of the same
   accident, one fastapi repin away from a broken build.

(1) reads each installed distribution's RECORD rather than `find_spec`, so ownership
is an answer about *declarations* and not about the current machine: with the extra
missing the exporter module cannot be found at all, and with the extra installed but
undeclared its owning distribution is outside the closure. The check fails in both
states, which is what makes it worth having.

No socket, no subprocess: the graph comes from `pyproject.toml` and `uv.lock`, and
ownership from the venv's own metadata (AGENTS.md rule 3).
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parent.parent
PYPROJECT = ROOT / "pyproject.toml"
LOCK = ROOT / "uv.lock"
TREES = (ROOT / "src", ROOT / "tests")

#: The module the endpoint path imports. Named on its own so the regression this
#: module exists for fails with a message that points at itself.
OTLP_EXPORTER = "opentelemetry.exporter.otlp.proto.http.trace_exporter"


def _normalized(name: str) -> str:
    """PEP 503 normalization, so a name from `pyproject.toml`, a name from
    `uv.lock` and a distribution's own metadata all compare equal."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _requirement(requirement: str) -> tuple[str, list[str]]:
    """`psycopg[binary]>=3.3.6` -> `("psycopg", ["binary"])`.

    Specifiers and markers are dropped: what a closure needs is the name, and a
    version floor belongs to the declaration, not to the graph walk.
    """
    match = re.match(r"\s*([A-Za-z0-9._-]+)\s*(?:\[([^\]]*)\])?", requirement)
    if match is None:
        return "", []
    extras = match.group(2) or ""
    return _normalized(match.group(1)), [x.strip() for x in extras.split(",") if x.strip()]


def _declared() -> set[str]:
    """The distributions `pyproject.toml` names: base plus every dependency group.

    A self-referential extra (`muse[otel]`) expands to that extra's own requirements,
    because that is what `uv sync` resolves it to. It is also why the dev group can
    ask for the exporter without a second copy of the floor: two floors in two places
    is a floor that drifts, and a drifted floor is this bug again, quieter.
    """
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    own = _normalized(project["project"]["name"])
    extras = project["project"].get("optional-dependencies", {})
    names: set[str] = set()

    def collect(requirements: list[str]) -> None:
        for requirement in requirements:
            if not isinstance(requirement, str):
                continue  # a `{include-group = "..."}` entry names a group, not a distribution
            name, requested = _requirement(requirement)
            if name == own:
                for extra in requested:
                    collect(extras[extra])
            else:
                names.add(name)

    collect(project["project"]["dependencies"])
    for group in project.get("dependency-groups", {}).values():
        collect(group)
    return names


def _gate_closure() -> set[str]:
    """Every distribution `uv sync --frozen` puts in the venv: the declared set plus
    whatever `uv.lock` records as a dependency of it.

    An edge carrying an `extra` is not followed. A third-party extra is opt-in,
    `bin/prime` passes none, and following those edges would bless exactly the
    mistake this module exists to catch.
    """
    packages = {
        _normalized(package["name"]): package
        for package in tomllib.loads(LOCK.read_text(encoding="utf-8"))["package"]
    }
    closure: set[str] = set()
    pending = list(_declared())
    while pending:
        name = pending.pop()
        if name in closure:
            continue
        closure.add(name)
        pending.extend(
            _normalized(edge["name"])
            for edge in packages.get(name, {}).get("dependencies", [])
            if "extra" not in edge
        )
    return closure


def _owners() -> dict[str, set[str]]:
    """Every module this venv can supply, mapped to the distributions shipping it.

    From the RECORD files rather than `packages_distributions()`, which answers only
    at top-level granularity — and at that granularity the declared
    `opentelemetry-api` and the extra-only `opentelemetry-exporter-otlp-proto-http`
    are indistinguishable, which is precisely the distinction being tested.
    """
    owners: dict[str, set[str]] = {}
    for distribution in metadata.distributions():
        name = _normalized(distribution.metadata["Name"])
        for path in distribution.files or ():
            parts = [part for part in str(path).split("/") if part and part != ".."]
            if not parts or "__pycache__" in parts or not parts[-1].endswith(".py"):
                continue
            parts[-1] = parts[-1][: -len(".py")]
            if parts[-1] == "__init__":
                parts.pop()
            if parts:
                owners.setdefault(".".join(parts), set()).add(name)
    return owners


def _imports() -> dict[str, set[str]]:
    """Absolute third-party imports across `src/` and `tests/`, mapped to their files.

    Imports inside a function body count, and that is the point: the lazy
    `from opentelemetry.exporter.otlp...` in `telemetry.py` is only reached when an
    endpoint is configured, so a suite that never got there would never notice the
    package was missing.
    """
    project = _normalized(tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"])
    stdlib = set(sys.stdlib_module_names)
    found: dict[str, set[str]] = {}
    for tree in TREES:
        for source in sorted(tree.rglob("*.py")):
            where = str(source.relative_to(ROOT))
            for node in ast.walk(ast.parse(source.read_text(encoding="utf-8"))):
                if isinstance(node, ast.Import):
                    imported = [alias.name for alias in node.names]
                elif isinstance(node, ast.ImportFrom):
                    if node.level or not node.module:
                        continue  # relative, and `from . import x` names no module
                    imported = [node.module]
                else:
                    continue
                for module in imported:
                    root = module.split(".")[0]
                    if root in stdlib or root in {project, "tests"}:
                        continue
                    found.setdefault(module, set()).add(where)
    return found


def _root_claims(declared: set[str]) -> dict[str, set[str]]:
    """Root package name -> the declared distributions shipping something beneath it.

    A root is a namespace far more often than a module — `opentelemetry` ships no
    `__init__.py` — so a root counts as declared when a *declared* distribution
    provides something under it, not when a file sits at exactly that path.
    """
    claims: dict[str, set[str]] = {}
    for module, distributors in _owners().items():
        owned = distributors & declared
        if owned:
            claims.setdefault(module.split(".")[0], set()).update(owned)
    return claims


def _provided(owners: dict[str, set[str]], closure: set[str]) -> set[str]:
    """Every module name the closure can supply: what its distributions ship, plus the
    namespace prefixes above them.

    The prefixes are not leniency, they are the only way a namespace can be named:
    `from opentelemetry import context` refers to a package no distribution ships a
    file for, and the distribution that answers for it is the one shipping
    `opentelemetry.context`. It cannot mask a missing extra, because a module that
    *is* shipped by something outside the closure is named by that owner and fails.
    """
    provided: set[str] = set()
    for module, distributors in owners.items():
        if not distributors & closure:
            continue
        parts = module.split(".")
        for depth in range(1, len(parts) + 1):
            provided.add(".".join(parts[:depth]))
    return provided


def test_every_import_is_provided_by_the_set_the_gate_installs() -> None:
    """The closure check, over every module the code and the suite import.

    A module the closure cannot supply is the merge-red case, so the message names the
    distributors: that is how you tell "nobody declares this" from "an extra declares
    it and the gate does not ask for that extra".
    """
    owners = _owners()
    provided = _provided(owners, _gate_closure())

    unprovided = {
        f"{module} (shipped by {', '.join(sorted(owners.get(module, ()))) or 'nothing found'}"
        f" — outside `uv sync --frozen`; imported by {', '.join(sorted(files))})"
        for module, files in _imports().items()
        if module not in provided
    }

    assert unprovided == set(), (
        "imports the gate does not install; add each to a dependency group, or to a\n"
        "self-referential extra the dev group pulls in, or the suite passes only in a\n"
        "venv that happens to have it:\n" + "\n".join(sorted(unprovided))
    )


def test_every_third_party_root_is_claimed_by_a_direct_declaration() -> None:
    """The declaration check, over every third-party root `src/` and `tests/` import.

    Transitive availability is not a declaration. A package that reaches the venv
    only because another one pins it can be repinned or dropped underneath muse, and
    the resulting failure is a clean-machine build error with nothing in muse's
    declarations to explain it.
    """
    claims = _root_claims(_declared())
    roots: dict[str, set[str]] = {}
    for module, files in _imports().items():
        roots.setdefault(module.split(".")[0], set()).update(files)

    unclaimed = {
        f"{root} (imported by {', '.join(sorted(files))})"
        for root, files in roots.items()
        if root not in claims
    }

    assert unclaimed == set(), (
        "imported without being declared; a transitive dependency is not a\n"
        "declaration, so add each to [project.dependencies] with a reason in\n"
        "README.md#dependencies:\n" + "\n".join(sorted(unclaimed))
    )


def test_the_otlp_exporter_the_endpoint_path_imports_is_in_the_gate() -> None:
    """The regression this module was added for, by name.

    `build_provider(endpoint=...)` imports the OTLP exporter, and
    `test_a_configured_endpoint_gets_a_batch_processor` exercises it. While the
    exporter was reachable only through the `otel` extra, the test suite needed a
    package the gate never installed: green in the worktree that had run
    `uv sync --extra otel`, red on every fresh clone. The exporter stays an optional
    extra for the *image* — pulling grpcio and protobuf into every install is still
    the wrong default — but a test that imports it makes it a dev dependency, and
    the dev group is what says what it needs.
    """
    assert _imports().get(OTLP_EXPORTER), (
        f"{OTLP_EXPORTER} is not imported any more, so this test has nothing left to "
        "guard; delete it rather than weaken it"
    )

    closure = _gate_closure()
    distributors = _owners().get(OTLP_EXPORTER, set())

    assert distributors & closure, (
        f"{OTLP_EXPORTER} is imported by the endpoint path and shipped by "
        f"{', '.join(sorted(distributors)) or 'nothing'}, none of which `uv sync "
        "--frozen` installs. A test that imports a package makes it a dev "
        "dependency: add `muse[otel]` to the dev group."
    )
