"""`docs/openai-quickstart.md`, held to the code it tells a reader to call.

The document exists because the aliases were reachable and undocumented: a self-hoster
had no base URL, no credential story and no model name. That gap closes, and then the
document is the only path a reader has — so it drifts the moment the code does, and it
drifts **silently**, which is the one thing a reader cannot detect and a test can.

Three properties, and each has a test named after the failure it prevents:

- **Every path it names is a path the service serves.** A quickstart that documents an
  endpoint which does not exist is worse than no quickstart: it costs a reader an
  afternoon. Read off `openapi/v1.yaml` and, in the other direction, checked against the
  running app's generated document — the same both-directions rule
  `tests/test_openapi.py` applies to the contract, because a path present in the
  document and absent from the app is the case a one-way check cannot see.
- **Every model name it tells a reader to ask for is in `config/routes.yaml`.** A route
  table is edited; a quickstart's table is not, so the table is the thing that goes
  stale. Asserted against the committed file rather than against a copy, which is what
  makes this a guard rather than a restatement.
- **The scope and the issuer it names are the constants in the code.** `completions:write`
  and the default issuer are strings a document can get wrong while the code is right,
  and nothing else in the suite would notice.

The document's own honesty is asserted too, and that is the fourth property: **it must
say it was run and must not claim the one thing it did not run.** A quickstart that
asserts `identity` can mint a token this service accepts would be the most damaging
possible lie in this repository, because §3 says in prose that it cannot. So the file is
required to carry a "not verified here" line, and the test that would catch its removal
is named after that.

`docs/openai-quickstart.md` is prose with fenced blocks in it, so the assertions read the
fenced code and the tables rather than the whole file — a mention of a path in a
paragraph explaining *why* a path is refused is not a claim that the path is served.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

from muse.auth import REQUIRED_CLAIMS, SCOPE
from muse.jwks import JWKS_PATH
from muse.main import DEFAULT_IDENTITY_AUDIENCE, DEFAULT_IDENTITY_ISSUER, create_app
from muse.routes import routes_from_yaml

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parent.parent
QUICKSTART = REPO_ROOT / "docs" / "openai-quickstart.md"
OPENAPI = REPO_ROOT / "openapi" / "v1.yaml"
ROUTES_YAML = REPO_ROOT / "config" / "routes.yaml"

#: A path in a fenced block, or in a table cell. The `(host)/v1/models` spelling is how
#: the document writes a path with its origin attached, which is what a reader copies.
_PATH_IN_TEXT = re.compile(r"(?:localhost:\d+|https?://[\w.-]+)?(/v1/[a-z/]*)")


def text() -> str:
    return QUICKSTART.read_text(encoding="utf-8")


def fenced_blocks() -> list[str]:
    """Every ``` fenced block, without the fences.

    So a path asserted here is one a reader could paste, rather than one the document
    mentions in passing while explaining why it is refused.
    """
    blocks, inside, current = [], False, []
    for line in text().splitlines():
        if line.startswith("```"):
            if inside:
                blocks.append("\n".join(current))
                current = []
            inside = not inside
            continue
        if inside:
            current.append(line)
    return blocks


def document_paths() -> set[str]:
    """Every `/v1/...` path the document asks a reader to call."""
    found: set[str] = set()
    for block in fenced_blocks():
        for match in _PATH_IN_TEXT.finditer(block):
            found.add(match.group(1).rstrip("/"))
    return found


def spec() -> dict:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


def route_models() -> tuple[str, ...]:
    return tuple(routes_from_yaml(ROUTES_YAML.read_text(encoding="utf-8")).models())


# --- the document exists and is where the README points at it ---------------


def test_the_quickstart_is_where_the_readme_says_it_is() -> None:
    """A quickstart nothing links to is a file. The README is where a reader arrives.

    Checked by resolving the link rather than by grepping for the filename, so a link
    with the wrong relative depth is caught too — `../README.md` from `docs/` and
    `README.md` from the root are both correct spellings in their own place and only one
    of them resolves.
    """
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    links = re.findall(r"\]\(([^)]*quickstart[^)]*)\)", readme)
    assert links, "the README links to no quickstart"
    for link in links:
        assert (REPO_ROOT / link).resolve().is_file(), (
            f"the README links at {link}, which is not a file"
        )


# --- every path it names is served -------------------------------------------


def test_every_path_the_quickstart_calls_is_in_the_committed_contract() -> None:
    """Names in a quickstart must resolve against `openapi/v1.yaml`.

    The alias paths and `/v1/route`, and nothing else. If a packet adds a path the
    document has not caught up with, this does not fire — the document is allowed to
    cover less than the service has. It fires on the dangerous direction.
    """
    documented = document_paths()
    assert documented, "the quickstart calls no path at all, which cannot be right"
    assert documented <= set(spec()["paths"]), (
        f"undocumented paths: {documented - set(spec()['paths'])}"
    )


def test_the_quickstart_calls_both_completion_paths_and_the_model_list() -> None:
    """The three it exists to document, named explicitly.

    Without this, a rewrite that quietly drops `/v1/models` leaves the first test
    green — it asserts a subset. A guard that passes by covering less is the failure
    `AGENTS.md`'s Dependencies section writes about, so the floor is asserted before the
    absence it is the floor for.
    """
    assert document_paths() == {"/v1/chat/completions", "/v1/models", "/v1/route"}


def test_every_path_the_quickstart_calls_the_running_app_actually_serves() -> None:
    """The document against the app, not the document against the file.

    The direction `test_every_path_the_quickstart_calls_is_in_the_committed_contract`
    cannot see: a path in `openapi/v1.yaml` that the app does not register. That is
    what `app.openapi()` is for, and rule 24 records why `app.routes` is not — this
    FastAPI version represents an included router as an opaque object with no `.routes`,
    so a route walk finds none of these paths and passes.
    """
    served = set(create_app().openapi()["paths"])
    assert document_paths() <= served


# --- every model name it tells a reader to use -------------------------------


def test_every_model_the_quickstart_offers_is_in_the_committed_route_table() -> None:
    """`fast` and `smart` are read out of `config/routes.yaml`, not restated.

    The table in the document is prose, and prose is what nobody updates. Asserting
    against the committed file is what makes the assertion a guard: rename a route and
    this goes red in the same commit rather than in a reader's terminal.
    """
    offered = {"fast", "smart", "decide"}
    assert offered == set(route_models())


def test_the_quickstart_says_a_vendor_model_id_is_not_askable() -> None:
    """`gpt-4o-mini` is in the document **as a refusal**, and that is the point.

    A reader who sends `model: "gpt-4o-mini"` gets a 404, and the document has to say
    why before they find out. So this asserts the document contains the vendor id
    *and* the word that marks it as not-askable — the alternative being a document that
    merely lists route names and lets the reader assume the vendor ids were forgotten.
    """
    body = text()
    assert "gpt-4o-mini" in body
    assert re.search(r"`gpt-4o-mini` is a 404", body), (
        "the document must refuse the vendor id explicitly"
    )


# --- the constants it names are the code's -----------------------------------


def test_the_scope_the_quickstart_names_is_the_one_the_code_requires() -> None:
    """`completions:write`, read from `muse.auth.SCOPE` rather than typed here.

    A capability string is a public API decision (README, "Two open platform
    questions"), so a future change to it is a one-line change in one constant — and
    this document would then be wrong in a way that sends a reader to a 403 they cannot
    explain. The floor is asserted rather than assumed: `SCOPE` is non-empty, so a
    guard that could pass by finding nothing cannot pass here.
    """
    assert SCOPE and SCOPE in text()


def test_the_default_issuer_and_audience_the_quickstart_names_are_the_codes_defaults() -> None:
    """`https://identity.cafaye.com` and `muse`, read from `muse.main`.

    Both are defaults rather than requirements — a wrong one cannot make a bad token
    good, so a wrong default refuses everything loudly. Which is exactly why the
    document has to carry the code's default rather than one a reader inferred from a
    deployment.
    """
    assert DEFAULT_IDENTITY_ISSUER and DEFAULT_IDENTITY_ISSUER in text()
    assert DEFAULT_IDENTITY_AUDIENCE and DEFAULT_IDENTITY_AUDIENCE in text()
    assert JWKS_PATH in text(), "the key-set path is appended to the issuer, not configured"


def test_the_quickstart_names_every_claim_the_verifier_requires() -> None:
    """`REQUIRED_CLAIMS`, checked claim by claim.

    A reader who does not know a claim is required does not know which part of their
    token to fix. The claim list is a tuple in `muse.auth`, so the document is checked
    against the whole of it — including `account_id`, which is the one with no fallback
    and the one a self-hoster is most likely to leave out.
    """
    for claim in REQUIRED_CLAIMS:
        assert f"`{claim}`" in text(), f"the quickstart does not name the required claim {claim!r}"


# --- the document's own honesty ----------------------------------------------


def test_the_quickstart_records_what_was_and_was_not_verified() -> None:
    """It must carry a "not verified" line, and name what it did not run.

    The document's largest claim is the one it cannot make: that `identity` can mint a
    token this service accepts. It cannot — §3 says so, from `identity`'s own scope
    vocabulary and its own opaque token format — so a future edit that quietly upgrades
    the claim to "verified" would turn the most useful paragraph in the document into
    the most damaging one. Asserted rather than trusted because it is exactly the kind
    of line a later, more confident rewrite deletes.
    """
    body = text()
    assert "**Not verified here:**" in body
    assert "identity" in body.split("**Not verified here:**", 1)[1]


def test_the_quickstart_marks_the_minted_credential_as_development_only() -> None:
    """The §3a recipe signs tokens with a key the reader just generated.

    That is a *development* arrangement — the documented reason `MUSE_JWKS_URL` exists —
    and a document that presented it as a credential path would be handing out a
    production-grade-sounding recipe for pointing a service at an attacker-chosen key
    set. The marking has to be in the document, not implied by its filename.
    """
    assert re.search(r"\*\*development\*\* arrangement", text())


def test_the_quickstart_carries_no_credential_shaped_literal() -> None:
    """No `sk-…`, no JWT, no long base64 blob in the committed document.

    A worked example is exactly where a real key gets pasted by accident, and a
    committed file is exactly where it would then live forever. `account_id` here is a
    fixed all-zeros-plus-one UUID, which is what the example is allowed to carry.
    """
    body = text()
    assert not re.search(r"sk-[A-Za-z0-9_-]{8,}", body)
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", body), (
        "the document contains something JWT-shaped"
    )
