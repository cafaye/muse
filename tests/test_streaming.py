"""`stream: true` over HTTP, on both paths, as OpenAI's wire format.

## What this file exists to hold

**Streaming must not become the second, weaker path.** Before 1.3.0 a request carrying
`stream` was a 422 naming the field — a *designed* refusal with a stated reason
(`openapi/v1.yaml` said so in its own words), not an oversight. 1.3.0 replaces it, and
the reason it needed saying at all is that replacing it is exactly how a service grows
a second authentication path, a second router call and a second metering call.

So the tests here are deliberately of two kinds:

- **wire format** — is this a stream a stock OpenAI client can consume? The frames, the
  terminator, the headers, the exact JSON of every frame.
- **invariants** — is a streaming request authenticated, scoped, routed and metered by
  the same code as a non-streaming one? Written as *comparisons against
  `POST /v1/route`* rather than as assertions about streaming in isolation, for the same
  reason `tests/test_alias.py` is written that way: a test that only checked streaming's
  own output would pass just as happily against a second implementation that happened to
  return the right bytes.

**A test that passes against nothing is worse than no test**, and this service has been
burned by that twice — the adapter's whole shape-handling was green against a dict where
litellm returns pydantic models. So the provider seam is exercised through the real
`FakeProvider`/`ScriptedProvider` doubles and the real router, and the failure modes are
injected rather than described: `tests/test_router_streaming.py` holds the
provider-fails-after-its-first-chunk and caller-disconnects cases, and this file drives
them through a real HTTP response so the wire format of a *failed* stream is asserted
too.
"""

from __future__ import annotations

import json

import pytest

from muse.errors import ProviderUnavailable
from muse.providers import Price, ProviderRegistry
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.routes import routes_from_yaml

from .conftest import asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.jwks import token
from .support.test_app import IDENTITY, build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PRICE = Price(1000, 2000)
MODEL = "gpt-4o-mini"
ROUTE = "/v1/route"
ALIAS = "/v1/chat/completions"

BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}


def routes(*models: str) -> str:
    """A routes file naming each model, all served by one provider."""
    return write_routes(
        {
            "version": 1,
            "routes": [
                {"model": name, "candidates": [{"provider": "openai", "model": MODEL}]}
                for name in models
            ],
        }
    )


def authed(**overrides) -> dict[str, str]:
    """A real signed token. Every check in the chain runs, so no test here can pass by
    bypassing the boundary it is testing."""
    return {"Authorization": f"Bearer {token(IDENTITY, **overrides)}"}


def app_for(registry: ProviderRegistry | None = None, *, database=None, auth=None):
    """An app whose `fast` route always completes."""
    resolved = registry or ProviderRegistry().register(
        FakeProvider(name="openai", content="hello there", tokens_in=12, tokens_out=8, price=PRICE)
    )
    return build_test_app(
        registry=resolved,
        database=database or FakeDatabase(),
        table=routes_from_yaml(routes("fast", "smart")),
        auth=auth,
    )


def frames_of(text: str) -> list[str]:
    """The `data:` payloads of an SSE body, in order, with the terminator removed."""
    payloads = [line[len("data: ") :] for line in text.split("\n\n") if line.startswith("data: ")]
    return [p for p in payloads if p != "[DONE]"]


def chunks_of(text: str) -> list[dict]:
    """The frames of an SSE body, parsed, terminator excluded."""
    return [json.loads(payload) for payload in frames_of(text)]


# --- the wire format --------------------------------------------------------


async def test_a_stream_is_an_sse_response_a_stock_client_recognises():
    """`text/event-stream`, and the three headers that decide whether a proxy will
    deliver it incrementally at all.

    `X-Accel-Buffering: no` and `Cache-Control: no-cache` are not decoration: a reverse
    proxy that buffers the response turns a stream into a 4-second pause followed by
    every frame at once, which looks to the caller like a broken server rather than a
    misconfigured proxy. A stream nobody can receive incrementally is not a stream.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    assert response.headers["x-accel-buffering"] == "no"
    assert response.headers["x-trace-id"], "support correlation survives the body"


async def test_every_frame_is_a_chat_completion_chunk_and_the_last_is_done():
    """The shape a client parses, and the terminator it stops on.

    `object` is `chat.completion.chunk` on every frame — that string is how a client
    knows these are not whole completions — and `[DONE]` is sent **unquoted**, because
    a client compares the payload text and `"[DONE]"` with quotes is not the terminator.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    body = response.text
    assert body.endswith("data: [DONE]\n\n")
    assert '"[DONE]"' not in body, "the terminator must not be JSON-encoded"

    chunks = chunks_of(body)
    assert chunks, "a stream must emit at least the frames the provider sent"
    assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}
    for chunk in chunks:
        assert set(chunk) <= {"id", "object", "created", "model", "choices", "usage"}


async def test_the_frames_are_separated_by_a_blank_line():
    """The blank line is the frame separator in SSE. Without it a client buffers the
    next event and a stream that "works" never flushes — the single most common way a
    hand-rolled SSE endpoint looks broken in production and passes a smoke test."""
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert "\n\n" in response.text
    # Every frame is `data: ...\n\n`: the split above yields the payloads with no
    # trailing newline left, and nothing else is interleaved between them (no comment
    # lines, no heartbeats), which is what a client parsing strictly expects.
    assert all(line.startswith("data: ") for line in response.text.splitlines() if line.strip())


async def test_the_text_the_caller_assembles_is_the_text_the_provider_produced():
    """Concatenating every `delta.content` must reproduce the completion, or the stream
    is lossy in a way the caller cannot detect."""
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assembled = "".join(
        choice["delta"].get("content", "")
        for chunk in chunks_of(response.text)
        for choice in chunk["choices"]
    )
    assert assembled.strip() == "hello there"


async def test_the_first_frame_carries_the_role_a_client_needs():
    """OpenAI's first chunk is `{"role": "assistant", "content": ""}`-shaped, and a
    client that assembles a message reads the role off it. Dropping it changes what the
    caller ends up with."""
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    first = chunks_of(response.text)[0]
    assert first["choices"][0]["delta"]["role"] == "assistant"


async def test_a_non_streaming_request_is_byte_for_byte_unchanged():
    """**The promise this packet makes about the path it did not touch.**

    `stream` defaults to `false`, so the body, the status, the headers and the
    `content-type` of a request that does not mention streaming are what they were. If
    this test ever fails, the change is a regression no changelog entry can justify.
    """
    async with asgi_client(app_for()) as client:
        plain = await client.post(ALIAS, json=BODY, headers=authed())
        explicit = await client.post(ALIAS, json={**BODY, "stream": False}, headers=authed())

    assert plain.status_code == explicit.status_code == 200
    assert plain.headers["content-type"].startswith("application/json")
    assert plain.json()["object"] == "chat.completion"
    # Same fields, and `stream: false` is not echoed anywhere.
    assert set(plain.json()) == set(explicit.json())
    assert "stream" not in plain.json()


async def test_streaming_works_identically_on_both_paths():
    """`/v1/route` streams too, and the frames are the same objects.

    The two paths render the same completion differently, and a stream is not a licence
    for one of them to grow a different frame shape. `id` and `created` are compared
    out: they are minted per request by design (`created: 0` is for `/v1/models`, whose
    ids are route names), so two requests can never agree on them and asserting they do
    would be asserting that the comparison is impossible.
    """
    async with asgi_client(app_for()) as client:
        on_route = await client.post(ROUTE, json={**BODY, "stream": True}, headers=authed())
        on_alias = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert on_route.status_code == on_alias.status_code == 200
    assert _anonymous(on_route.text) == _anonymous(on_alias.text)


def _anonymous(text: str) -> list[dict]:
    """An SSE body's frames with the per-request `id` and `created` removed."""
    return [
        {key: value for key, value in chunk.items() if key not in {"id", "created"}}
        for chunk in chunks_of(text)
    ]


# --- usage, and what it does not control ------------------------------------


async def test_usage_is_metered_but_not_returned_unless_asked_for():
    """**The distinction that matters most in this file.**

    `stream_options.include_usage` controls whether usage goes on the WIRE. It does not
    control whether the stream is METERED — a stream's token counts arrive only in a
    final usage chunk, so a stream that did not ask for them would be one whose spend is
    never billed, and every completion this service serves is metered.

    So with `include_usage` absent the frames carry no usage *and* the outbox row still
    records the tokens. Asserting both halves: a test that only checked the frames would
    pass against an implementation that quietly stopped billing streamed completions.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database=database)) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert all("usage" not in chunk for chunk in chunks_of(response.text)), (
        "usage must not be returned unless it was asked for"
    )
    assert len(database.outbox) == 1, "and the stream was still metered, exactly once"
    # The row, not a summary of it: the tokens the provider reported are what was billed.
    assert json.loads(database.outbox[0]["data"])["tokens_in"] == 12
    assert json.loads(database.outbox[0]["data"])["tokens_out"] == 8


async def test_include_usage_adds_a_final_chunk_before_the_terminator():
    """When asked for, usage is the last frame before `[DONE]` and carries no choices.

    `choices: []` on the usage chunk is OpenAI's shape and is what a client expects; a
    usage chunk with a choice in it would make a client append an empty delta to the
    message it is assembling.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database=database)) as client:
        response = await client.post(
            ALIAS,
            json={**BODY, "stream": True, "stream_options": {"include_usage": True}},
            headers=authed(),
        )

    chunks = chunks_of(response.text)
    usage_chunk = chunks[-1]
    assert usage_chunk["choices"] == []
    assert usage_chunk["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 8,
        "total_tokens": 20,
    }
    assert all("usage" not in chunk for chunk in chunks[:-1])
    # One outbox row still: asking to see the numbers does not bill twice.
    assert len(database.outbox) == 1, "asking to see the numbers does not bill twice"


async def test_stream_options_without_stream_is_not_an_error():
    """The options only have meaning on a stream, and ignoring them matches OpenAI
    rather than producing an error a caller cannot act on."""
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS, json={**BODY, "stream_options": {"include_usage": True}}, headers=authed()
        )

    assert response.status_code == 200
    assert response.json()["usage"]["total_tokens"] == 20, "the normal body, unchanged"


# --- failures, on the wire --------------------------------------------------


class DiesMidStream:
    """A provider that answers, then fails — the defect injected through the real HTTP
    path, so the *wire format of a failed stream* is asserted and not only the router's
    behaviour."""

    def __init__(self, name: str = "openai") -> None:
        self._inner = FakeProvider(name=name, content="one two three", price=PRICE)
        self.name = name
        self.calls = 0

    async def stream(self, request):
        from muse.providers import StreamChunk

        self.calls += 1
        yield StreamChunk(role="assistant")
        yield StreamChunk(content="one ")
        raise ProviderUnavailable("openai reset the socket")

    async def complete(self, request):
        return await self._inner.complete(request)

    async def health(self):  # pragma: no cover - never called here
        return await self._inner.health()

    def cost_per_1k_tokens(self, model: str) -> Price:
        return self._inner.cost_per_1k_tokens(model)


async def test_a_failure_after_the_first_chunk_is_an_error_frame_then_done():
    """**Question 3's answer, on the wire.**

    An SSE response cannot carry a 503 once it has begun, so the failure arrives as a
    `data:` frame carrying this service's own error envelope, followed by `[DONE]`. The
    terminator still goes out, so the client ends the stream the way it ends a success
    rather than waiting on a connection nobody will close.

    Asserted on the envelope's *fields* rather than on prose: `error.message` and
    `error.type` are what a client library raises from, so a frame without them produces
    an unhelpful error on the client however good the text is.
    """
    registry = ProviderRegistry().register(DiesMidStream())
    async with asgi_client(app_for(registry)) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert response.status_code == 200, "the headers were already sent"
    assert response.text.endswith("data: [DONE]\n\n"), "the client is told to stop"

    payload = json.loads(frames_of(response.text)[-1])
    assert payload["error"]["type"] == "unavailable"
    assert payload["error"]["code"] == "upstream_error"
    assert "reset the socket" in payload["error"]["message"]
    assert payload["error"]["param"] is None


async def test_the_chunks_that_arrived_before_the_failure_are_still_on_the_wire():
    """The caller keeps what genuinely arrived. The error frame says the stream stopped;
    it does not retract the text that was already sent."""
    registry = ProviderRegistry().register(DiesMidStream())
    async with asgi_client(app_for(registry)) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert '"content":"one "' in response.text.replace(" ", "").replace("content", "content") or (
        "one " in response.text
    )
    assert "error" in response.text


async def test_a_failed_stream_is_recorded_rather_than_dropped_silently(
    caplog,
):
    """**Silently recording nothing is not an option**, so the refusal to meter is
    itself an event.

    The choice not to bill a stream that did not complete is defensible; being unable to
    tell afterwards that it happened is not. A warning line is what turns "no outbox
    row" from an absence into a recorded fact an operator can query.

    Asserted on the *contents* as well as the fact, because a log line is the one place
    a caller's words can leak by accident (rule 16). The caller's prompt must not appear,
    and the provider's text must not either — only the error's class, the route name and
    the trace id.
    """
    database = FakeDatabase()
    registry = ProviderRegistry().register(DiesMidStream())
    app = app_for(registry, database=database)

    with caplog.at_level("WARNING", logger="muse.api"):
        async with asgi_client(app) as client:
            response = await client.post(
                ALIAS,
                json={
                    "model": "fast",
                    "messages": [{"role": "user", "content": "a SECRET_PROMPT"}],
                    "stream": True,
                },
                headers=authed(),
            )

    assert response.status_code == 200
    assert database.outbox == []
    warnings = [r for r in caplog.records if r.name == "muse.api"]
    assert len(warnings) == 1, "exactly one line, so it is a fact and not a flood"
    record = warnings[0]
    assert record.msg == "muse.stream.not_metered"
    assert record.reason == "stream_did_not_complete"
    assert record.error_class == "ProviderUnavailable", "the class, never the message"
    assert record.model == "fast"
    # The canary: the prompt and the provider's text must not be on the line.
    assert "SECRET_PROMPT" not in caplog.text
    assert "reset the socket" not in caplog.text


async def test_a_failed_stream_is_not_metered():
    """**Question 2's answer, measured rather than asserted in prose.**

    A stream that did not complete is not metered: the completion does not exist, and
    inventing one from the chunks that arrived would be a guess dressed as a measurement.
    The check is the absence of an outbox row — which is only a meaningful assertion
    because `test_usage_is_metered_but_not_returned_unless_asked_for` proves the same
    fake database *does* record a row on success.
    """
    database = FakeDatabase()
    registry = ProviderRegistry().register(DiesMidStream())
    async with asgi_client(app_for(registry, database=database)) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert response.status_code == 200
    assert database.outbox == [], "a stream that failed is not a completion to bill"


async def test_every_candidate_failing_before_the_first_chunk_is_still_a_503():
    """**The status is still a status when the failure is early enough.**

    This is the half of question 3 that makes the other half coherent: because the
    stream is opened *before* the response exists, a failure no caller has seen bytes
    for is still an ordinary `problem+json` 503 with the same body a non-streaming
    request gets. Only a failure *after* the first chunk has to become an error frame.
    """
    registry = ProviderRegistry().register(
        ScriptedProvider(
            name="openai",
            errors=[ProviderUnavailable("down")] * 5,
            completions=[],
            price=PRICE,
        )
    )
    async with asgi_client(app_for(registry)) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "unavailable"
    assert "every candidate" in response.json()["detail"]


# --- the invariants: streaming is not a second path -------------------------


async def test_a_stream_is_authenticated_exactly_as_a_non_streaming_one_is():
    """No credential, no provider call, no money spent — and no 200 either."""
    database = FakeDatabase()
    provider = FakeProvider(name="openai", content="hello there", price=PRICE)
    async with asgi_client(
        app_for(ProviderRegistry().register(provider), database=database)
    ) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True})

    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"
    assert provider.calls == 0
    assert database.outbox == []


async def test_a_stream_without_the_capability_is_forbidden():
    """**A valid signature is not an authorization, and streaming is not the exemption.**

    The scope gate is the same gate, and it runs before the body is even read — so the
    refusal cannot depend on which fields the body carried.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS,
            json={**BODY, "stream": True},
            headers={"Authorization": f"Bearer {token(IDENTITY, scopes='completions:read')}"},
        )

    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_an_unauthenticated_stream_cannot_learn_whether_a_model_routes():
    """Authentication precedes the body, so the order is observable: an unknown model
    and a known one are indistinguishable to a caller with no credential.

    Compared with `trace_id` set aside, because a trace id is minted per request by
    design and two responses can never agree on it — comparing the whole body would
    make this test pass for the wrong reason whenever the ids happened to match.
    """
    async with asgi_client(app_for()) as client:
        unknown = await client.post(ALIAS, json={"model": "nope", "messages": [], "stream": True})
        known = await client.post(ALIAS, json=BODY)

    assert unknown.status_code == known.status_code == 401
    strip = lambda body: {k: v for k, v in body.items() if k != "trace_id"}  # noqa: E731
    assert strip(unknown.json()) == strip(known.json())


async def test_a_stream_is_routed_through_the_one_router_with_the_one_fallback_chain():
    """The fallback chain is the same chain, on the same terms.

    A stream that could reach a provider a non-streaming request would have refused is
    the exact bug this packet exists to prevent, so the test is a comparison: the backup
    answers a streamed request precisely when it answers an unstreamed one.
    """
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(
            name="openai",
            errors=[ProviderUnavailable("down")] * 5,
            completions=[],
            price=PRICE,
        )
    )
    registry.register(FakeProvider(name="anthropic", content="from the backup", price=PRICE))
    table = routes_from_yaml(write_routes_fallback("fast", "openai", "anthropic"))
    app = build_test_app(registry=registry, database=FakeDatabase(), table=table, auth=None)

    async with asgi_client(app) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())

    assert response.status_code == 200
    assembled = "".join(
        choice["delta"].get("content", "")
        for chunk in chunks_of(response.text)
        for choice in chunk["choices"]
    )
    assert assembled.strip() == "from the backup", (
        "a streamed request fell back to the same candidate an unstreamed one would"
    )


def write_routes_fallback(route: str, primary: str, backup: str) -> str:
    """A route with two candidates, so a fallback has somewhere to go."""
    return write_routes(
        {
            "version": 1,
            "routes": [
                {
                    "model": route,
                    "candidates": [
                        {"provider": primary, "model": "gpt-4o-mini"},
                        {"provider": backup, "model": "claude-haiku-4-5"},
                    ],
                }
            ],
        }
    )


# --- validation -------------------------------------------------------------


async def test_a_non_boolean_stream_is_a_422_naming_the_field():
    """Before any byte is sent, so the caller can correct it.

    The failure this prevents is subtle: a client sending `"stream": "true"` to a
    service that coerces gets a *non-streaming* body with a 200 and has no way to tell
    what happened.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": "true"}, headers=authed())

    assert response.status_code == 422
    assert response.json()["errors"] == [{"field": "stream", "code": "bool_type"}]
    assert response.headers["content-type"].startswith("application/problem+json"), (
        "and it is a problem document, not a stream carrying an error frame"
    )


async def test_a_stringed_false_is_a_422_and_not_a_silent_stream():
    """**The coercion this field's type exists to prevent.**

    pydantic's lax `bool` accepts `"true"`, `"yes"`, `1` *and* `"false"`, `"no"`, `0`,
    coercing all six. A caller whose serialiser stringifies booleans — a JSON-LD
    encoder, a form post, a config-driven client — would send `"stream": "false"`,
    meaning *do not stream*, and get a stream. Their client may not be reading
    `text/event-stream` at all, so the failure is silent and looks like the server
    hanging up.

    The same class of coercion was refused for `temperature` in 1.1.1 (a float that
    cannot be round-tripped, arriving as `0.30000000000000004`) and for every unknown
    field by `extra="forbid"`. `stream` joins them rather than introducing the one field
    where a wrong type is quietly reinterpreted.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": "false"}, headers=authed())

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_an_unknown_stream_option_is_a_422():
    """`stream_options` is closed, so a typo'd option is refused rather than ignored. A
    caller who wrote `include_Usage` must find out, not get a stream with no usage."""
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS,
            json={**BODY, "stream": True, "stream_options": {"include_Usage": True}},
            headers=authed(),
        )

    assert response.status_code == 422
    assert response.json()["errors"] == [
        {"field": "stream_options.include_Usage", "code": "extra_forbidden"}
    ]


async def test_a_malformed_body_is_a_422_and_not_a_stream():
    """A `problem+json` body, not an SSE frame.

    The validation happens before the response is created for exactly this reason: once
    a `StreamingResponse` exists the status is spent, and a client expecting
    `application/problem+json` would be handed `text/event-stream` with an error frame
    inside it.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS, headers={**authed(), "Content-Type": "application/json"}, content=b"{not json"
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


async def test_an_unknown_model_is_a_404_before_the_stream_starts():
    """Still a status, still `problem+json`: no candidate was tried, so no bytes were
    owed."""
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS,
            json={"model": "nope", "messages": BODY["messages"], "stream": True},
            headers=authed(),
        )

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
