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

import pytest

from muse.breaker import BreakerRegistry
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
from .support.tracing import by_name, names, recording_telemetry, rendered

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


def _app(registry: ProviderRegistry, table: RouteTable, telemetry: Telemetry):
    database = FakeDatabase()
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table, telemetry=telemetry),
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
    assert provider_span["attributes"]["error.type"] == "ProviderAuthError"
    assert by_name(exporter, "muse.request")[0]["attributes"]["http.response.status_code"] == 503
    assert "sk-live-SECRET" not in rendered(exporter)


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
        "ProviderAuthError"
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
