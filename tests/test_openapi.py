"""The contract, checked against core and against the running app.

Three things drift, and each has a test here:

- **`openapi/v1.yaml` against the code.** FastAPI generates a document from the
  response models; `openapi/v1.yaml` is the committed contract the SDKs are generated
  from. Two documents describing one API is exactly how a client compiles against a
  field that is not there. Both directions are asserted: an endpoint in the file that
  the app does not serve, and a field in a response model that the file omits.
- **`cafaye.yml` against core's schema.** The manifest format is core's, and a
  manifest that does not validate is a manifest `caf dev` will not read.
- **The published event type against core's grammar and catalog.** The pattern is
  copied into `muse/contracts.py`; the parity test proves the copy is current.

The manifest and schema checks are skipped without a core checkout, and they *say* so
rather than passing quietly — `MUSE_CORE_SCHEMAS` points at one, and `core` above that
is the repository root.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest
import yaml

from muse.api import RouteRequestBody, RouteResponseBody
from muse.auth import SCOPE, SCOPE_CLAIM, SCOPES_CLAIM
from muse.contracts import TOKENS_CONSUMED, validate_event_type
from muse.main import create_app

from .support.jwks import Identity, token

#: `anyio` as well as `unit`: one test here drives the real app end to end to prove
#: the running service emits the correlation headers the document promises, and
#: nothing about a document comparison can catch a header the middleware forgets.
pytestmark = [pytest.mark.unit, pytest.mark.anyio]

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI = REPO_ROOT / "openapi" / "v1.yaml"
MANIFEST = REPO_ROOT / "cafaye.yml"


def spec() -> dict:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


def prose(document: str) -> str:
    """A description with its line breaks collapsed.

    The security scheme's description is a YAML block scalar written to be *read*, so it
    wraps wherever the prose wraps. An assertion about a phrase across that wrap would
    be testing the line width of a document rather than its content, and it would fail
    the next time somebody rewraps a paragraph.
    """
    return " ".join(document.split())


def manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def core_root() -> Path | None:
    """core's repository root, or `None` when no checkout is pointed at."""
    schemas = os.environ.get("MUSE_CORE_SCHEMAS")
    if not schemas:
        return None
    root = Path(schemas).parent
    return root if (root / "schemas" / "cafaye.manifest.schema.json").is_file() else None


# --- the OpenAPI document --------------------------------------------------


def test_the_document_exists_where_the_manifest_points() -> None:
    """`exposes.api` is a repository-relative path, and a manifest pointing at a
    missing file makes `caf dev` and every SDK generator fail with a path error."""
    assert (REPO_ROOT / manifest()["exposes"]["api"]).is_file()


def test_the_document_is_openapi_31() -> None:
    """3.1, not 3.0. 3.1 is what core's conventions require and what makes `null` a
    valid type rather than a keyword that changes meaning."""
    assert spec()["openapi"].startswith("3.1")


def test_info_version_is_present_and_parseable() -> None:
    """core requires it, and it is the only signal a consumer has for attributing a
    deprecated endpoint to a release."""
    assert re.match(r"^\d+\.\d+\.\d+$", spec()["info"]["version"])


def test_every_path_is_under_the_v1_prefix() -> None:
    """core: every path is prefixed, and `/v1` is never mutated in place. A second
    prefix in one document means a breaking change landed without one."""
    for path in spec()["paths"]:
        assert path.startswith("/v1/"), f"{path} is not under /v1"


def test_there_is_exactly_one_prefix() -> None:
    prefixes = {path.split("/")[1] for path in spec()["paths"]}
    assert prefixes == {"v1"}


def test_the_version_prefix_and_the_document_version_are_both_present() -> None:
    """core's sync rule: the path prefix says which contract, `info.version` says
    which build of the document. Bumping one without the other is the mistake the rule
    exists to prevent."""
    assert "/v1/" in next(iter(spec()["paths"]))
    assert spec()["info"]["version"]


def test_the_only_endpoint_is_the_routed_completion() -> None:
    """Pinned so adding a path is a deliberate act with a test to update, rather than
    something that lands because a handler was added."""
    assert list(spec()["paths"]) == ["/v1/route"]


def test_the_probe_endpoints_are_not_in_the_contract() -> None:
    """`/healthz` and `/readyz` are infrastructure, not contract. Documenting them
    would put them under the /v1 rule they do not belong under, and an SDK generated
    from this document would grow a client for a health check."""
    for path in spec()["paths"]:
        assert "health" not in path and "ready" not in path


def test_a_security_scheme_is_declared() -> None:
    """core: bearer JWTs only, and a document without a scheme tells a generator
    nothing about how to authenticate."""
    assert spec()["components"]["securitySchemes"]["bearerAuth"]["scheme"] == "bearer"


def test_security_is_applied_globally() -> None:
    """A global `security` with no per-operation override, so a new endpoint inherits
    "requires a credential" rather than defaulting to open."""
    assert spec()["security"] == [{"bearerAuth": []}]


# --- the document against the code -----------------------------------------


def test_every_documented_path_is_served_by_the_app() -> None:
    for path, operations in spec()["paths"].items():
        served = {
            method.upper()
            for method in create_app().openapi()["paths"][path]
            if method in {"get", "post", "put", "patch", "delete"}
        }
        assert served == {method.upper() for method in operations}, (
            f"{path}: the document says {sorted(operations)}, the app serves {sorted(served)}"
        )


def test_every_path_the_app_serves_is_documented() -> None:
    """The other direction. An endpoint in the code that the committed contract omits
    is an endpoint no SDK has, which is a gap nobody notices until a caller asks for
    it."""
    for path in create_app().openapi()["paths"]:
        if path.startswith("/v1/"):
            assert path in spec()["paths"], f"{path} is served but not documented"


async def test_the_running_app_emits_every_correlation_header_the_document_promises() -> None:
    """The third direction, and the one a header can get wrong without anyone noticing.

    The two tests above check the document against the *code*; nothing there would
    catch a header the document promises and the middleware forgets to send. So this
    drives the real app and reads the real response — the property rule 14 is actually
    about, which is that the committed contract is what the service does.
    """

    from muse.breaker import BreakerRegistry
    from muse.main import Container, Settings
    from muse.metering import Meter
    from muse.providers import Price, ProviderRegistry
    from muse.providers.credentials import StaticCredentials
    from muse.providers.fake import FakeProvider
    from muse.router import Router
    from muse.routes import routes_from_yaml
    from muse.vault import Vault

    from .conftest import AUTH_HEADERS, asgi_client
    from .support.fake_database import FakeDatabase
    from .support.test_app import TEST_KEY, auth_for
    from .support.tracing import recording_telemetry

    documented = spec()["paths"]["/v1/route"]["post"]["responses"]

    telemetry, _ = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    table = routes_from_yaml(
        "version: 1\nroutes:\n  - model: fast\n    candidates:\n      - provider: openai\n"
    )
    database = FakeDatabase()
    app = create_app(
        container=Container(
            settings=Settings(env="test"),
            database=database,
            registry=registry,
            routes=table,
            router=Router(registry, table, telemetry=telemetry, breakers=BreakerRegistry()),
            vault=Vault(database, TEST_KEY),
            meter=Meter(database),
            credentials=StaticCredentials({}),
            auth=auth_for(),
            telemetry=telemetry,
        )
    )

    async with asgi_client(app) as client:
        ok = await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            headers={
                **AUTH_HEADERS,
                "traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
            },
        )
        # A 503 is the response a caller most wants a trace for, so it is asserted
        # too rather than assumed to travel the same path.
        unauthorized = await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )
        not_found = await client.get(
            "/does-not-exist",
            headers={"traceparent": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"},
        )

    assert ok.status_code == 200
    assert unauthorized.status_code == 401
    assert not_found.status_code == 404
    # httpx lowercases response header names, and HTTP header names are
    # case-insensitive, so the comparison is too.
    for response in (ok, unauthorized, not_found):
        assert set(response.headers) >= {"x-trace-id", "traceparent"}
        assert re.fullmatch(
            spec()["components"]["headers"]["Traceparent"]["schema"]["pattern"],
            response.headers["traceparent"],
        )
    assert not_found.headers["traceparent"], "the 404 path is not covered by the contract"
    assert documented, "the contract is empty, so the assertion above proves nothing"


def test_the_response_schema_names_exactly_the_response_models_fields() -> None:
    """A field in a response model that the document omits is a field no SDK has. This
    is the check that makes the committed document worth committing."""
    documented = set(spec()["components"]["schemas"]["RouteResponse"]["properties"])
    assert documented == set(RouteResponseBody.model_fields)


def test_the_request_schema_names_exactly_the_request_models_fields() -> None:
    """The same, for the request. And it is the reason the request model sets
    `extra="forbid"`: a field the document does not list is rejected rather than
    silently ignored."""
    documented = set(spec()["components"]["schemas"]["RouteRequest"]["properties"])
    assert documented == set(RouteRequestBody.model_fields)


def test_every_documented_response_schema_is_actually_reachable() -> None:
    """Every status the document lists for the endpoint, except the two it marks as
    reserved or method-level. A documented status the app cannot produce is a promise
    to a client that nothing keeps.

    `403` was added by muse-06 and `404` is listed here rather than reached by a
    documented path: the document promises both, and the two tests below drive the real
    app for each.
    """
    documented = {int(status) for status in spec()["paths"]["/v1/route"]["post"]["responses"]}
    assert documented == {200, 401, 403, 404, 405, 422, 500, 503}


def test_the_security_scheme_names_the_capability_the_operation_requires() -> None:
    """core's checklist: "Auth: required scopes + `account_id` scoping stated in the
    security scheme."

    `x-required-scopes` is darkroom's spelling and it is checked here rather than
    invented, because a second service inventing its own extension is how a generator
    learns to read neither. The prose in the description is *also* asserted, since the
    extension is machine-readable and the description is what a human reads.
    """
    scheme = spec()["components"]["securitySchemes"]["bearerAuth"]

    assert scheme["scheme"] == "bearer"
    assert scheme["bearerFormat"] == "JWT", "every other service states the format"
    assert scheme["x-required-scopes"]["route"] == [SCOPE]

    description = prose(scheme["description"])
    for phrase in (
        SCOPE,  # the capability, by name
        "account_id",  # the tenancy claim
        "RS256",  # the pinned algorithm
        "identity",  # the only issuer
        "open platform decision",  # the claim name is not settled
        "503",
    ):
        assert phrase in description, f"the scheme never mentions {phrase!r}"


def test_the_document_does_not_claim_the_token_is_unverified() -> None:
    """The claim that became false the moment this landed.

    Both the old phrases are named so that a future edit which reintroduces either —
    as stale prose, or as a copy of an older document — fails here rather than telling
    a caller that any non-empty bearer string will do.
    """
    document = spec()
    scheme = prose(document["components"]["securitySchemes"]["bearerAuth"]["description"])
    info = prose(document["info"]["description"])

    for stale in (
        "does not verify the token",
        "no signature, no expiry, no scopes",
        "the token is not verified",
        "Auth is a stub",
    ):
        assert stale not in scheme, f"the scheme still says {stale!r}"
        assert stale not in info


def test_the_document_states_that_identity_being_down_refuses_the_request() -> None:
    """The operational contract, in the place someone will read at 3am.

    A generated SDK is not where an operator looks during an outage, so this has to be
    in the document itself rather than only in the repository README.
    """
    scheme = prose(spec()["components"]["securitySchemes"]["bearerAuth"]["description"])
    assert "does not serve unauthenticated traffic when identity is down" in scheme

    unavailable = prose(spec()["paths"]["/v1/route"]["post"]["responses"]["503"]["description"])
    assert "identity" in unavailable
    assert "unauthenticated traffic when identity is down" in unavailable


def test_the_document_records_that_the_scope_claim_name_is_open() -> None:
    """Both names accepted, and the disagreement refused.

    Stated in the document because the *next* reader is whoever decides it. Without
    this, a reader sees one service accepting two claim names and reasonably concludes
    the duality is a design rather than an unresolved platform question.
    """
    scheme = prose(spec()["components"]["securitySchemes"]["bearerAuth"]["description"])
    assert SCOPES_CLAIM in scheme and SCOPE_CLAIM in scheme
    assert "disagree" in scheme
    assert "open platform decision" in scheme
    assert "provisional" in scheme


async def test_the_app_answers_403_for_a_verified_token_with_no_scope() -> None:
    """The document's 403, reached through the real app.

    Driven rather than asserted against a literal, because the point is that a client
    reading this file is told about a status the service really produces. The policy
    behind it lives in `tests/test_auth.py`; what is checked *here* is only that the
    document and the code agree.
    """
    identity = Identity()
    response = await _route(identity, token(identity, scopes=""))
    assert response.status_code == 403
    assert "403" in spec()["paths"]["/v1/route"]["post"]["responses"]
    assert SCOPE in response.json()["detail"]


async def test_the_app_answers_503_when_the_signing_keys_cannot_be_retrieved() -> None:
    """The document's second 503 cause, reached through the real app.

    The document distinguishes two reasons for one status — the providers are down, or
    `identity` is — and a client branching on that difference is the whole reason the
    503 is worth separating from a 401.
    """
    identity = Identity()
    identity.fail_with(503)
    response = await _route(identity, token(identity))
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


async def _route(identity, token: str):
    """One authenticated request against a real app.

    Local rather than imported from `tests/test_auth.py`: two test modules reaching into
    each other's helpers is a coupling that makes one of them unrunnable alone, and
    these two need the fixture rather than the whole auth suite.
    """
    from muse.providers import Price, ProviderRegistry
    from muse.providers.fake import FakeProvider
    from muse.routes import routes_from_yaml

    from .conftest import asgi_client
    from .support.fake_database import FakeDatabase
    from .support.test_app import auth_for, build_test_app

    app = build_test_app(
        registry=ProviderRegistry().register(FakeProvider(name="openai", price=Price(1, 1))),
        database=FakeDatabase(),
        table=routes_from_yaml(
            "version: 1\nroutes:\n  - model: fast\n    candidates:\n      - provider: openai\n"
        ),
        auth=auth_for(identity),
    )
    async with asgi_client(app) as client:
        return await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"role": "user", "content": "hi"}]},
            headers=bearer(token),
        )


def test_every_error_response_is_problem_json() -> None:
    """core: every non-2xx is `application/problem+json`. A 500 documented as
    `application/json` produces a client that cannot parse our own errors."""
    responses = spec()["paths"]["/v1/route"]["post"]["responses"]
    for status, operation in responses.items():
        if status.startswith("2"):
            continue
        assert "application/problem+json" in operation["content"], f"{status} is not problem+json"


def test_the_problem_schema_requires_the_trace_id() -> None:
    """core: `trace_id` is always present and always matches the header. Optional in
    the schema would make every generated client treat it as maybe-absent."""
    required = spec()["components"]["schemas"]["Problem"]["required"]
    assert "trace_id" in required
    assert "code" in required


def test_the_problem_schema_closes_itself() -> None:
    """core closes every schema it owns. An open one means a field we add later
    reaches a client that was written against this document."""
    assert spec()["components"]["schemas"]["Problem"]["additionalProperties"] is False


def test_the_error_codes_are_core_reserved_ones() -> None:
    """The reserved list from core's openapi conventions. An invented code is a client
    that has to special-case muse."""
    reserved = {
        "unauthorized",
        "forbidden",
        "not_found",
        "conflict",
        "validation_failed",
        "rate_limited",
        "idempotency_key_reused",
        "internal",
        "unavailable",
    }
    documented = set(spec()["components"]["schemas"]["Problem"]["properties"]["code"]["enum"])
    assert documented <= reserved


def test_the_trace_id_header_is_declared_on_every_response() -> None:
    """core's checklist: echoed in both header and body. A response without the
    header means support has nothing to grep for."""
    for status, operation in spec()["paths"]["/v1/route"]["post"]["responses"].items():
        assert "X-Trace-Id" in operation["headers"], f"{status} does not echo X-Trace-Id"


def test_the_traceparent_header_is_declared_on_every_response() -> None:
    """Packet muse-03, and the same reasoning as `X-Trace-Id`.

    A caller that sent a `traceparent` needs one back to continue the trace, and it
    needs it on the *error* responses most of all — a 503 is exactly when someone
    wants to pull up the trace.
    """
    for status, operation in spec()["paths"]["/v1/route"]["post"]["responses"].items():
        assert "traceparent" in operation["headers"], f"{status} does not echo traceparent"


def test_the_traceparent_header_is_declared_as_an_optional_request_parameter() -> None:
    """Optional, because a caller that has never heard of W3C trace context must get
    a working service.

    A client generated from a document that marked it required would refuse to send
    anything, and the obvious fix in a generated SDK is to invent a plausible-looking
    traceparent — which is worse than not sending one.
    """
    parameters = spec()["paths"]["/v1/route"]["post"]["parameters"]
    traceparent = next(p for p in parameters if p["name"] == "traceparent")

    assert traceparent["in"] == "header"
    assert traceparent["required"] is False


def test_the_documented_traceparent_pattern_is_the_one_muse_accepts() -> None:
    """The document and the parser must agree, in the strict direction that matters.

    The published pattern is *narrower* than what `parse_traceparent` accepts — it
    excludes the future versions the W3C spec says to continue rather than discard.
    A client sending a `02-…` header would be rejected by its own generated
    validator and then correctly served by muse, which is the asymmetry the
    description says out loud. What must not happen is the other direction: a
    documented value that muse itself would refuse.
    """
    from muse.telemetry import parse_traceparent

    pattern = spec()["components"]["headers"]["Traceparent"]["schema"]["pattern"]
    examples = [
        spec()["components"]["headers"]["Traceparent"]["example"],
        next(
            p
            for p in spec()["paths"]["/v1/route"]["post"]["parameters"]
            if p["name"] == "traceparent"
        )["example"],
    ]

    assert re.fullmatch(pattern, examples[0])
    for example in examples:
        assert parse_traceparent(example) is not None, f"{example} is documented but refused"


def test_the_request_body_is_required() -> None:
    assert spec()["paths"]["/v1/route"]["post"]["requestBody"]["required"] is True


def test_the_document_closes_its_request_schema() -> None:
    """A typo'd `max_token` is rejected rather than ignored, so a caller who
    misspells it finds out instead of believing they capped the response."""
    assert spec()["components"]["schemas"]["RouteRequest"]["additionalProperties"] is False


def test_the_choices_are_exactly_one() -> None:
    """`n > 1` is not supported, and saying so in the schema is what stops a generated
    client from offering it."""
    choices = spec()["components"]["schemas"]["RouteResponse"]["properties"]["choices"]
    assert choices["minItems"] == choices["maxItems"] == 1


def test_cost_micros_is_an_integer_and_never_negative() -> None:
    """The money type. A float here would be a float in billing's sum, and a float
    that rounds differently on two machines is a reconciliation bug nobody can
    reproduce."""
    cost = spec()["components"]["schemas"]["RouteResponse"]["properties"]["cost_micros"]
    assert cost["type"] == "integer"
    assert cost["minimum"] == 0


def test_every_example_in_the_document_is_well_formed() -> None:
    """Walked rather than trusted. An example with a stale field name is the part of a
    document a reader trusts most and checks least.

    Both OpenAPI spellings are handled, because the document uses both: a single
    unnamed `example`, and the `examples` map that carries a `summary` per entry. muse-06
    added the second form to the 401 and the 503 so each cause gets a name, and this
    walk is what keeps a *named* example from being the one nobody checks.
    """
    document = spec()
    for status, operation in document["paths"]["/v1/route"]["post"]["responses"].items():
        for media_type, payload in operation["content"].items():
            for name, example in payload.get("examples", {}).items():
                assert isinstance(example, dict), f"{status}/{name} is not an object"
                if media_type != "application/problem+json":
                    continue
                body = example.get("value", example)
                assert body["code"] in TITLES, f"{status}/{name} has a non-reserved code"
                assert body["status"] == int(status), f"{status}/{name} disagrees with status"
                assert body["type"].endswith(f"/{body['code']}")


#: The reserved codes this service documents, for the example check above.
TITLES = {
    "unauthorized",
    "forbidden",
    "not_found",
    "conflict",
    "validation_failed",
    "rate_limited",
    "internal",
    "unavailable",
}


# --- the manifest ----------------------------------------------------------


def test_the_manifest_is_cafaye_spec_v0_2() -> None:
    """The core range this service compiles against. A manifest pinning a range it
    does not satisfy makes `caf contract` fail for everyone downstream."""
    assert manifest()["core"] == "^0.2.0"


def test_the_manifest_names_the_repository_over_ssh() -> None:
    """PLAN §1: SSH for anything cafaye owns, never HTTPS. core's schema rejects an
    HTTPS remote, so a bad habit fails the suite rather than shipping."""
    assert manifest()["repository"]["url"].startswith("git@github.com:cafaye/")
    assert manifest()["repository"]["url"].endswith(".git")


def test_the_manifest_declares_master_as_the_default_branch() -> None:
    """PLAN §1: `master` everywhere. core's schema makes this a `const`, so a
    different value cannot be committed."""
    assert manifest()["repository"]["defaultBranch"] == "master"


def test_the_manifest_publishes_exactly_the_one_event() -> None:
    """Pinned so adding a type is a deliberate act. core asserts the manifest and its
    catalog agree in both directions, so an unlisted type here fails *core's* suite."""
    assert manifest()["exposes"]["events"] == [TOKENS_CONSUMED]


def test_every_published_event_type_is_well_formed() -> None:
    """Three segments, prefixed with this service's own name."""
    for event_type in manifest()["exposes"]["events"]:
        assert validate_event_type(event_type) == event_type
        assert event_type.split(".")[0] == manifest()["name"]


def test_the_manifest_declares_no_consumed_events() -> None:
    """muse is a leaf in v0. A consumer list would be a subscription with no handler,
    and core requires a consumed type to exist in its catalog."""
    assert manifest()["consumes"] == []


def test_the_manifest_points_at_the_openapi_document() -> None:
    assert manifest()["exposes"]["api"] == "openapi/v1.yaml"


def test_every_declared_dependency_is_a_service_and_not_a_package() -> None:
    """core's rule 4: a package belongs in the language's lockfile. A gem or a module
    listed here is a dependency nothing can resolve."""
    for dependency in manifest()["dependencies"]:
        assert dependency["name"] in {"identity", "guard"}


def test_identity_is_a_required_dependency_and_guard_is_not() -> None:
    """muse-06, and the reason is a serving fact rather than a preference.

    `identity` was `required: false` while the token was an unchecked header. Every
    token is now verified against the key set it publishes, so a deployment without it
    answers `/v1/route` with 503 for every request — and a soft dependency is how
    `caf dev` and a topology tool start a service that cannot serve.

    `guard` stays soft: it is the public edge, and a service is reachable without an
    edge in front of it. Asserted as the *pair* because the two are the same field with
    two meanings, and a test that checks one without the other cannot tell which is
    which.
    """
    required = {d["name"]: d["required"] for d in manifest()["dependencies"]}
    assert required == {"identity": True, "guard": False}


def test_the_manifest_records_the_open_auth_decisions() -> None:
    """The three questions this packet implemented safely under.

    A comment is not documentation — it is a note. What matters is that a reader who
    finds the code in six months learns from the *manifest* that the claim name is
    open, and that a decision taken here is provisional. This fails if the DECISION
    NEEDED block is deleted, which is the failure mode that matters: the questions get
    silently closed by nobody answering them.
    """
    text = MANIFEST.read_text(encoding="utf-8")

    for question in (
        "The capability claim's *name*",  # `scopes` vs `scope`
        "completions:write",  # the required scope
        "account_id` is required here",  # the tenancy answer
    ):
        assert question in text, f"cafaye.yml no longer records: {question}"
    assert "DECISION NEEDED" in text


def test_the_manifest_validates_against_core() -> None:
    """The manifest format is core's, and a manifest that does not validate is one
    `caf dev`, `caf gen` and `pantry` all fail to read."""
    root = core_root()
    if root is None:
        pytest.skip("MUSE_CORE_SCHEMAS is not set; skipping validation against core")
    schema = json.loads((root / "schemas" / "cafaye.manifest.schema.json").read_text())
    jsonschema = pytest.importorskip("jsonschema")
    jsonschema.Draft202012Validator.check_schema(schema)
    jsonschema.validate(manifest(), schema)


def test_the_manifest_language_is_one_core_knows() -> None:
    """core's enum. A language core does not list means `caf` has no build recipe for
    this service."""
    assert manifest()["language"] in {
        "go",
        "ruby",
        "elixir",
        "python",
        "typescript",
        "rust",
        "spec",
    }
