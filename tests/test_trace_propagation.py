"""Traceparent in, traceparent out — and the canary that must never appear.

This is the file the packet's security constraint lives in, so it is worth being
explicit about what is being claimed. `redaction.py` exists because prompts,
completions and API keys are all text that ends up somewhere it should not. Adding
observability creates a new destination for exactly that text: a tracing backend,
which by design is a searchable, retained, widely-readable store. So "we must not log
prompt or completion text" becomes "we must not put it on a span either", and the
only way that survives future changes is a test that fails when it happens.

The canary is a unique string placed in **both** the prompt and the completion, and
the assertion is on the rendered span payload — every attribute of every span, as one
string. Asserting attribute-by-attribute would only prove the allowlist catches the
names somebody already thought of. The canary test is short enough that truncation
cannot be what makes it pass, and it is asserted *both* ways: the string appears
nowhere, and the specific dangerous keys are absent from every span.

The propagation tests use the real `LiteLLMProvider` with the litellm stub, because
the header is added by the adapter and a double would prove nothing about it.
"""

from __future__ import annotations

import json
from functools import cache
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from muse.breaker import BreakerRegistry
from muse.errortype import SCHEMA_PATH
from muse.main import Container, Settings, create_app
from muse.metering import Meter
from muse.providers import Completion, LiteLLMProvider, Price, ProviderRegistry
from muse.providers.credentials import StaticCredentials
from muse.providers.fake import FakeProvider
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RouteTable, routes_from_yaml
from muse.telemetry import TRACEPARENT_HEADER, Telemetry
from muse.vault import Vault

from .conftest import AUTH_HEADERS, asgi_client
from .support.fake_database import FakeDatabase
from .support.litellm_stub import make_stub
from .support.test_app import TEST_KEY
from .support.tracing import by_name, names, payloads, recording_telemetry, rendered

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

#: The caller's trace. W3C's own example, so a reader can check it against the spec.
INBOUND_TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
INBOUND_SPAN_ID = "00f067aa0ba902b7"
INBOUND = f"00-{INBOUND_TRACE_ID}-{INBOUND_SPAN_ID}-01"

#: A string that must not survive anywhere in the exported spans. It appears in the
#: prompt, in the completion, and — to catch an error path that echoes one into the
#: other — nowhere else, so a hit in the payload is unambiguous.
CANARY = "CANARY-9f3a1c7e-DO-NOT-EXPORT"


def _table(provider: str = "openai", model: str = "fast") -> RouteTable:
    return routes_from_yaml(
        f"version: 1\nroutes:\n  - model: {model}\n    candidates:\n      - provider: {provider}\n"
    )


#: The model the litellm stub's price table actually holds. A route named `fast` would
#: be skipped as unpriced *before* dispatch, so a test using the real adapter against
#: the stub has to name a model the stub can price or it would prove nothing about
#: the call it meant to make.
STUB_MODEL = "gpt-4o-mini"


def _app(registry: ProviderRegistry, table: RouteTable, telemetry: Telemetry, **kwargs):
    database = FakeDatabase()
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table, telemetry=telemetry, **kwargs),
        vault=Vault(database, TEST_KEY),
        meter=Meter(database),
        credentials=StaticCredentials({}),
        telemetry=telemetry,
    )
    return create_app(container=container)


def _body(content: str = "hello") -> dict:
    return {"model": "fast", "messages": [{"role": "user", "content": content}]}


def _trace_id_of(header: str) -> str:
    return header.split("-")[1]


# --- inbound propagation ---------------------------------------------------


async def test_a_well_formed_traceparent_is_continued() -> None:
    """muse joins the caller's trace rather than starting one of its own.

    The trace id in the response is the caller's, so a log line in the caller's
    system and a span in the collector's are the same trace.
    """
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND}
        )

    assert response.status_code == 200
    assert _trace_id_of(response.headers[TRACEPARENT_HEADER]) == INBOUND_TRACE_ID

    request_span = by_name(exporter, "muse.request")[0]
    assert request_span["attributes"]["muse_trace_id"] == INBOUND_TRACE_ID
    # The span id is muse's own, not the caller's: continuing a trace means becoming
    # a child of it, and reusing the parent's span id would make two spans claim to
    # be the same work.
    assert request_span["attributes"]["muse_parent_span_id"] == INBOUND_SPAN_ID


async def test_the_outbound_provider_call_carries_the_same_trace_id() -> None:
    """`traceparent` out, on the actual adapter call.

    Asserted on `extra_headers` as the litellm stub captured it, because the adapter
    is the only place a header can be added and the stub is the only place it can be
    seen. The trace id matches the caller's; the span id is the provider call's own
    child span, so the vendor's own telemetry (if it emits any) joins the same trace
    at the right depth rather than colliding with muse's.
    """
    telemetry, _ = recording_telemetry()
    stub = make_stub()
    registry = ProviderRegistry()
    registry.register(LiteLLMProvider(name="openai", api_key="sk-test", litellm=stub))
    app = _app(registry, _table(model=STUB_MODEL), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json={"model": STUB_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND},
        )

    assert response.status_code == 200
    sent = stub.captured["extra_headers"][TRACEPARENT_HEADER]
    assert _trace_id_of(sent) == INBOUND_TRACE_ID
    assert sent.split("-")[2] != INBOUND_SPAN_ID, "the provider call reused the parent's span id"


async def test_a_malformed_traceparent_starts_a_new_trace_and_still_serves() -> None:
    """Never a 400, never an exception.

    A trace header is an observability affordance. If it can take a customer's request
    down, then anyone who can send a header can deny service, and a bug in a caller's
    header construction becomes muse's outage. The only thing a bad header may cost
    is correlation.
    """
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json=_body(),
            headers={**AUTH_HEADERS, TRACEPARENT_HEADER: "totally not a traceparent"},
        )

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"]
    fresh = response.headers[TRACEPARENT_HEADER]
    assert _trace_id_of(fresh) != INBOUND_TRACE_ID
    assert len(_trace_id_of(fresh)) == 32
    assert by_name(exporter, "muse.request")[0]["attributes"]["muse_trace_id"] == _trace_id_of(
        fresh
    )


async def test_an_absent_traceparent_starts_a_new_trace() -> None:
    """The common case: a caller that has never heard of W3C trace context."""
    telemetry, _ = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post("/v1/route", json=_body(), headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert len(_trace_id_of(response.headers[TRACEPARENT_HEADER])) == 32


async def test_the_legacy_trace_id_header_still_works() -> None:
    """`X-Trace-Id` is the packet-02 contract and stays exactly as it was.

    Two correlation ids on one response is a cost, and it is paid deliberately: the
    committed OpenAPI document publishes `X-Trace-Id` in the header and the body, so
    removing it is a breaking change to a contract the SDKs are generated from. The
    new header is additive.
    """
    telemetry, _ = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, "X-Trace-Id": "abc123def456"}
        )

    assert response.headers["X-Trace-Id"] == "abc123def456"
    assert response.json()["trace_id"] == "abc123def456"


async def test_no_traceparent_is_sent_when_nothing_is_tracing() -> None:
    """The no-op telemetry must not send a header with a fabricated trace id.

    Inventing one would put muse's spans into whatever trace a vendor happens to
    assign, and would make "we propagate the caller's trace" untrue in the one
    configuration nobody configured.
    """
    stub = make_stub()
    registry = ProviderRegistry()
    registry.register(LiteLLMProvider(name="openai", api_key="sk-test", litellm=stub))
    app = _app(registry, _table(model=STUB_MODEL), Telemetry.noop())

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json={"model": STUB_MODEL, "messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 200
    assert "extra_headers" not in stub.captured


# --- the spans -------------------------------------------------------------


async def test_the_three_spans_are_emitted() -> None:
    """The request, the routing decision, and the provider call.

    `muse.route` is the one that earns its keep: it is the only span that can answer
    "why did this call go to openai and cost three times the estimate", and it is
    absent unless it is deliberately created.
    """
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND}
        )

    assert sorted(names(exporter)) == ["muse.provider.call", "muse.request", "muse.route"]

    route_span = by_name(exporter, "muse.route")[0]
    assert route_span["attributes"]["muse_route"] == "fast"
    assert route_span["attributes"]["muse_candidates_tried"] == 1
    assert route_span["attributes"]["muse_attempts"] == 1

    provider_span = by_name(exporter, "muse.provider.call")[0]
    assert provider_span["attributes"]["muse_provider"] == "openai"
    assert provider_span["attributes"]["muse_tokens_in"] == 10
    assert provider_span["attributes"]["muse_tokens_out"] == 5
    assert provider_span["attributes"]["muse_cost_micros"] > 0
    assert provider_span["attributes"]["muse_latency_ms"] >= 0
    assert by_name(exporter, "muse.request")[0]["attributes"]["http.response.status_code"] == 200


async def test_a_failed_request_still_emits_its_spans_with_an_error_type() -> None:
    """A 503 is the span an operator actually needs.

    The status and the error *class* are recorded; the provider's message is not. A
    vendor's error text is third-party text, and a content-policy rejection quotes
    the offending content back — so `error.message` is the one attribute that could
    carry a prompt, which is why it is not on the allowlist.

    The value is core's vocabulary and not muse's class name. `provider_auth` is what
    every service in the fleet will spell this as, and the assertion is on the exact
    string so a mapping that answered `internal_error` — or `_OTHER` — fails here
    instead of quietly reshaping a dashboard nobody is looking at yet.
    """
    from muse.errors import ProviderAuthError
    from muse.providers.fake import ScriptedProvider

    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(
            name="openai",
            price=Price(1000, 2000),
            errors=(ProviderAuthError("Incorrect API key: sk-live-SECRET"),),
        )
    )
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND}
        )

    assert response.status_code == 503
    provider_span = by_name(exporter, "muse.provider.call")[0]
    assert provider_span["attributes"]["error.type"] == "provider_auth"
    assert by_name(exporter, "muse.request")[0]["attributes"]["http.response.status_code"] == 503
    assert "sk-live-SECRET" not in rendered(exporter)


# --- error status and error class are one biconditional --------------------
#
# core-05 made these two an `allOf` in traces.schema.json rather than two
# suggestions: a span whose status is `error` must carry a class, and a span
# carrying a class must have failed. Before this packet muse recorded the class
# and left the status `unset`, which is exactly the half of the pair that leaves a
# span two queries reading it differently — the fleet view filters on status
# `error`, so a classified failure that never reached the predicate was invisible
# in the one place the fleet looks for it.


async def test_a_failed_provider_call_marks_the_span_failed_and_classifies_it() -> None:
    """Both halves of the pair, asserted on the span that actually failed.

    The class alone is what muse emitted before; the status alone is what a span
    with no classification would have to offer a query. Asserting the exact class as
    well keeps this from passing on `error.type` merely being present.
    """
    from muse.errors import ProviderTimeout
    from muse.providers.fake import ScriptedProvider

    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(name="openai", price=Price(1000, 2000), errors=(ProviderTimeout("slow"),))
    )
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND}
        )

    assert response.status_code == 503
    provider_span = by_name(exporter, "muse.provider.call")[0]
    assert provider_span["status"] == "error"
    assert provider_span["attributes"]["error.type"] == "timeout"


async def test_every_span_agrees_about_whether_it_failed() -> None:
    """The fleet-wide property, over every span one exporter holds.

    Two requests through one app, deliberately shaped to produce all three kinds of
    span: a success, a provider that failed and was retried-then-fell-through to the
    fallback, and a refusal from a breaker that opened on that failure. The
    assertion is the biconditional itself — a class is present **if and only if** the
    span failed — applied to every span rather than to one the test picked.

    The floor comes first, for the reason the canary test gives: "nothing here
    carries a class" is also true of an exporter that exported nothing, and of a
    vocabulary where the mapping raised and every call fell back to no attribute at
    all. So the classes are asserted as present, on the spans that should have them,
    before any absence is claimed.
    """
    from muse.errors import ProviderTimeout
    from muse.providers.fake import ScriptedProvider

    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(name="openai", price=Price(1000, 2000), errors=(ProviderTimeout("slow"),))
    )
    registry.register(FakeProvider(name="anthropic", price=Price(1000, 2000)))
    two_candidates = routes_from_yaml(
        "version: 1\nroutes:\n  - model: fast\n    candidates:\n"
        "      - provider: openai\n      - provider: anthropic\n"
    )
    app = _app(registry, two_candidates, telemetry, breakers=BreakerRegistry(threshold=1))

    async with asgi_client(app) as client:
        first = await client.post("/v1/route", json=_body(), headers=AUTH_HEADERS)
        second = await client.post("/v1/route", json=_body(), headers=AUTH_HEADERS)

    # Both requests were served — by the fallback, and by the fallback again once the
    # breaker held the primary back. An error rate that counted these would be a lie.
    assert (first.status_code, second.status_code) == (200, 200)

    classes = [
        span["attributes"].get("error.type")
        for span in payloads(exporter)
        if span["name"] == "muse.provider.call"
    ]
    assert sorted(value for value in classes if value is not None) == ["circuit_open", "timeout"]

    disagreeing = [
        span
        for span in payloads(exporter)
        if (span["status"] == "error") != ("error.type" in span["attributes"])
    ]
    assert disagreeing == [], "a span's status and its error class tell different stories"


async def test_a_mapped_span_validates_against_core_schema() -> None:
    """muse's own rule, checked by core's own schema rather than by my reading of it.

    `traces.schema.json` is vendored into `muse/schemas/`, so this runs on the default
    gate rather than only when `MUSE_CORE_SCHEMAS` points at a checkout — the byte-for-
    byte drift check on that copy is what needs the checkout, not the schema itself. A
    hand-written assertion of the biconditional is my interpretation of core's rule;
    this is core's rule, applied to a span a real request produced.

    The document is projected onto the fields core knows: muse's own `muse_*` attribute
    names are **not** in core's trace allowlist yet (see the report), and asserting
    around that here would make this a test about an unrelated gap.
    """
    from muse.errors import ProviderAuthError
    from muse.providers.fake import ScriptedProvider

    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(name="openai", price=Price(1000, 2000), errors=(ProviderAuthError("no"),))
    )
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        await client.post("/v1/route", json=_body(), headers=AUTH_HEADERS)

    document = _core_document(by_name(exporter, "muse.provider.call")[0])
    _validator().validate(document)

    # The same document claiming to have succeeded is rejected, and so is the same
    # document with the class replaced by muse's own class name. These are core's
    # rules, and the second half of the pair is the reason this packet had to touch the
    # span status at all.
    not_failed = {**document, "status": {"code": "unset"}}
    assert not _validator().is_valid(not_failed)

    undeclared = {**document, "attributes": {"error.type": "ProviderAuthError"}}
    assert not _validator().is_valid(undeclared)

    no_class = {**document, "attributes": {}}
    assert not _validator().is_valid(no_class)

    # Every span muse exports carries a status, because the SDK always sets one —
    # asserted rather than assumed, because the assertion below depends on it. core's
    # `then` constrains `status` without requiring it to exist, so a document that
    # omitted the key entirely would slip past the biconditional; that hole is in core's
    # schema and is reported rather than worked around here.
    assert all(span["status"] in {"unset", "ok", "error"} for span in payloads(exporter))


def _core_document(span: dict) -> dict:
    """A real exported span, in the shape `traces.schema.json` describes.

    Read off the exporter rather than written out, so what is validated is what a
    collector would have received rather than what this test believes muse emits.
    """
    allowlist = set(
        json.loads(Path(SCHEMA_PATH).read_text(encoding="utf-8"))["$defs"]["tracesAttributes"][
            "properties"
        ]
    )
    return {
        "name": span["name"],
        "kind": span["kind"],
        "status": {"code": span["status"]},
        "attributes": {
            name: value for name, value in span["attributes"].items() if name in allowlist
        },
    }


@cache
def _validator():
    """core's schema, compiled once.

    Cached because a `Draft202012Validator` walks the whole document to compile itself,
    and this is called once per assertion rather than once per test.
    """
    return Draft202012Validator(json.loads(Path(SCHEMA_PATH).read_text(encoding="utf-8")))


async def test_an_unmapped_class_is_reported_as_other_and_named_in_the_log() -> None:
    """The safety net, through the real app.

    `muse.errortype.error_type` refuses a class it does not know; this is what happens
    next. `_OTHER` and a log line naming the class — and *only* the class, because the
    message here is a vendor's and a content-policy rejection quotes the offending
    content back. So the request still returns the 503 the caller deserves, the span
    still says something true, and the omission is loud in the one place an operator
    will look before the dashboard.

    The red test for this is in `test_error_vocabulary.py`: a class in `muse.errors`
    with no mapping fails the gate. This one is about what happens in the window
    before somebody adds the entry.
    """
    from muse.errors import ProviderUnavailable
    from muse.providers.fake import ScriptedProvider

    class AProviderErrorNobodyClassified(ProviderUnavailable):
        """A brand new failure, added without a mapping."""

    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(
            name="openai",
            price=Price(1000, 2000),
            errors=(AProviderErrorNobodyClassified("the vendor said something"),),
        )
    )
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route", json=_body(), headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND}
        )

    assert response.status_code == 503
    provider_span = by_name(exporter, "muse.provider.call")[0]
    assert provider_span["attributes"]["error.type"] == "_OTHER"
    assert provider_span["status"] == "error"
    assert "the vendor said something" not in rendered(exporter)


async def test_the_breaker_state_is_recorded_on_the_provider_span() -> None:
    """The signal that turns "muse is slow" into "openai is being held back"."""
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    breakers = BreakerRegistry(threshold=5)
    table = _table()
    database = FakeDatabase()
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table, telemetry=telemetry, breakers=breakers),
        vault=Vault(database, TEST_KEY),
        meter=Meter(database),
        credentials=StaticCredentials({}),
        telemetry=telemetry,
    )
    app = create_app(container=container)

    async with asgi_client(app) as client:
        await client.post("/v1/route", json=_body(), headers=AUTH_HEADERS)

    assert (
        by_name(exporter, "muse.provider.call")[0]["attributes"]["muse_breaker_state"] == "closed"
    )


# --- THE CANARY ------------------------------------------------------------


async def test_no_span_attribute_carries_prompt_or_completion_text() -> None:
    """The security assertion, end to end, through the real app.

    The canary is in the prompt *and* in the completion the provider returns, so a
    leak from either side is caught. The assertion is on the rendered payload, so a
    leak through a key nobody predicted is caught too. And the two are asserted
    separately, because "the canary is absent" could otherwise be true for the wrong
    reason — a truncated export, an empty span, a request that never reached the
    provider.
    """
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    registry.register(
        FakeProvider(
            name="openai",
            content=f"echoing back: {CANARY}",
            price=Price(1000, 2000),
        )
    )
    app = _app(registry, _table(), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json=_body(content=f"my prompt contains {CANARY}"),
            headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND},
        )

    # The request really did carry the canary, or the test proves nothing.
    assert response.status_code == 200
    assert CANARY in response.json()["choices"][0]["message"]["content"]
    assert names(exporter), "no spans were exported, so absence proves nothing"

    # ... and none of it reached a span.
    assert CANARY not in rendered(exporter)
    for span in by_name(exporter, "muse.provider.call") + by_name(exporter, "muse.route"):
        for name in span["attributes"]:
            assert not any(
                word in name.lower()
                for word in ("prompt", "message", "content", "text", "body", "header")
            )


async def test_a_provider_echoing_the_credential_is_not_recorded_on_a_span() -> None:
    """The credential, via the vendor's own error text, through the real adapter.

    Distinct from the canary test because it is a different mechanism: this is the
    adapter's redaction (`_sanitise`) meeting the span allowlist. A span that records
    only `error.type` cannot carry the message even if redaction were removed, which
    is the point of allowlisting rather than scrubbing — two independent barriers to
    the same leak, because the realistic failure is one of them being removed by a
    well-meaning change and nobody noticing for a month.
    """
    telemetry, exporter = recording_telemetry()
    stub = make_stub(raises="AuthenticationError", error_message="Incorrect API key: sk-live-XYZ")
    registry = ProviderRegistry()
    registry.register(LiteLLMProvider(name="openai", api_key="sk-live-XYZ", litellm=stub))
    app = _app(registry, _table(model=STUB_MODEL), telemetry)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json={"model": STUB_MODEL, "messages": [{"role": "user", "content": "hello"}]},
            headers={**AUTH_HEADERS, TRACEPARENT_HEADER: INBOUND},
        )

    # The provider did echo the key; the adapter scrubbed it; the span never saw it.
    assert response.status_code == 503
    assert "sk-live-XYZ" not in response.json()["detail"]
    assert by_name(exporter, "muse.provider.call")[0]["attributes"]["error.type"] == (
        "provider_auth"
    )
    assert "sk-live-XYZ" not in rendered(exporter)


async def test_a_held_credential_never_reaches_a_span() -> None:
    """The vault's own `Secret`, end to end.

    `Secret` prints as `[redacted]` and `record()` refuses the type outright, so there
    is no code path from the vault to a span even if a future attribute wanted it.
    """
    telemetry, exporter = recording_telemetry()
    registry = ProviderRegistry()
    stub = make_stub(raises="AuthenticationError", error_message="Incorrect API key: sk-live-XYZ")
    registry.register(LiteLLMProvider(name="openai", api_key="sk-live-XYZ", litellm=stub))
    database = FakeDatabase()
    table = _table(model=STUB_MODEL)
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table, telemetry=telemetry),
        vault=Vault(database, TEST_KEY),
        meter=Meter(database),
        credentials=StaticCredentials({}),
        scrub_secrets=(Secret("sk-live-XYZ"),),
        telemetry=telemetry,
    )
    app = create_app(container=container)

    async with asgi_client(app) as client:
        response = await client.post(
            "/v1/route",
            json={"model": STUB_MODEL, "messages": [{"role": "user", "content": "hello"}]},
            headers=AUTH_HEADERS,
        )

    assert response.status_code == 503
    assert "sk-live-XYZ" not in rendered(exporter)
    assert "sk-live-XYZ" not in response.headers[TRACEPARENT_HEADER]


def test_a_completion_content_value_is_not_an_allowlisted_attribute() -> None:
    """The unit-level statement behind the canary test.

    There is no attribute name a completion's text could be assigned to, so the
    integration test above is not resting on "muse happened not to do it this time".
    """
    from muse.telemetry import ALLOWED_SPAN_ATTRIBUTES

    assert "muse_completion" not in ALLOWED_SPAN_ATTRIBUTES
    assert "muse_content" not in ALLOWED_SPAN_ATTRIBUTES
    assert "Completion" not in "".join(ALLOWED_SPAN_ATTRIBUTES)


def test_a_completion_object_itself_cannot_be_recorded() -> None:
    """A `Completion` is a `dataclass`, so `record` would take its `repr` if it let
    non-scalars through — and that repr holds the content."""
    telemetry, exporter = recording_telemetry()
    completion = Completion(
        provider="openai",
        model="gpt-4o-mini",
        content=CANARY,
        tokens_in=1,
        tokens_out=1,
        price=Price(1, 1),
    )

    with telemetry.span("muse.test") as span:
        from muse.telemetry import record

        record(span, muse_model=completion)

    assert CANARY not in rendered(exporter)
