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
from muse.contracts import TOKENS_CONSUMED, validate_event_type
from muse.main import create_app

#: `anyio` as well as `unit`: one test here drives the real app end to end to prove
#: the running service emits the correlation headers the document promises, and
#: nothing about a document comparison can catch a header the middleware forgets.
pytestmark = [pytest.mark.unit, pytest.mark.anyio]

REPO_ROOT = Path(__file__).resolve().parent.parent
OPENAPI = REPO_ROOT / "openapi" / "v1.yaml"
MANIFEST = REPO_ROOT / "cafaye.yml"


def spec() -> dict:
    return yaml.safe_load(OPENAPI.read_text(encoding="utf-8"))


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
    from .support.test_app import TEST_KEY
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
    to a client that nothing keeps."""
    documented = {int(status) for status in spec()["paths"]["/v1/route"]["post"]["responses"]}
    assert documented == {200, 401, 404, 405, 422, 500, 503}


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
    document a reader trusts most and checks least."""
    document = spec()
    for status, operation in document["paths"]["/v1/route"]["post"]["responses"].items():
        for media_type, payload in operation["content"].items():
            for name, example in payload.get("examples", {}).items():
                assert isinstance(example, dict), f"{status}/{name} is not an object"
                if media_type == "application/problem+json":
                    assert example["code"] in TITLES, f"{status}/{name} has a non-reserved code"
                    assert example["status"] == int(status), (
                        f"{status}/{name} disagrees with status"
                    )
                    assert example["type"].endswith(f"/{example['code']}")


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
        assert dependency["required"] is False


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
