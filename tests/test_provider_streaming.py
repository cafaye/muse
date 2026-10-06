"""The litellm adapter's streaming, against litellm's real streaming types.

**Why this file is built the way it is.** The precedent is
`tests/test_providers.py::test_the_adapter_reads_a_real_litellm_model_response`, which
exists because the adapter read `response["choices"][0].get("message")`, which works
for a dict and fails for a pydantic model — so every *real* completion raised
`ResponseShapeError` while the whole adapter's shape-handling was green against
`tests/support/litellm_stub.py`'s plain dicts. Two production-blocking defects in this
service were invisible to a green suite that way, so the streaming surface is built
against `litellm.types.utils` from the first test rather than from an imitation.

Three properties of the real `ModelResponseStream` make that more than a habit here,
and each one is invisible in the dict form:

- **`usage` is not a declared field.** The model is `extra="allow"`, so the usage
  chunk carries its `Usage` as an extra attribute. An adapter written against
  `model_fields` — or against a dict where `usage` is simply a key — would never find
  it, and a stream whose tokens cannot be found is a stream that cannot be metered.
- **`choices` is empty on the usage chunk.** `choices[0]` is a crash on the one chunk
  that carries the numbers.
- **A failure arrives while *iterating*, not while awaiting.** `acompletion(stream=True)`
  answers with a stream object; the vendor's real failure is usually three chunks in.

The last one is why there is a test here that fails *mid-walk* rather than at the call.
"""

from __future__ import annotations

from typing import Any

import pytest

from muse.errors import (
    CredentialUnavailable,
    ProviderIndeterminate,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderUnavailable,
    ResponseShapeError,
)
from muse.providers import CompletionRequest, LiteLLMProvider, Message, StreamChunk

from .support.litellm_stub import make_stub

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MODEL = "gpt-4o-mini"
OPENAI = "openai"


def ask(**kwargs) -> CompletionRequest:
    """A valid request, with fields overridden per test."""
    return CompletionRequest(
        model=MODEL, messages=(Message(role="user", content="hello"),), **kwargs
    )


def make_provider(*, api_key: str | None = "sk-test", **stub_kwargs) -> LiteLLMProvider:
    """A `LiteLLMProvider` wired to a stand-in litellm."""
    return LiteLLMProvider(name=OPENAI, api_key=api_key, litellm=make_stub(**stub_kwargs))


async def collect(provider: LiteLLMProvider, **kwargs) -> list[StreamChunk]:
    """Every chunk a provider streams, in order."""
    return [chunk async for chunk in provider.stream(ask(**kwargs))]


# --- litellm's REAL streaming types -----------------------------------------


def real_chunk(
    content: str | None = None,
    *,
    role: str | None = None,
    finish_reason: str | None = None,
) -> Any:
    """A genuine `ModelResponseStream`, built by litellm's own classes.

    Deliberately not a dict. This is the precedent
    `test_the_adapter_reads_a_real_litellm_model_response` sets: a fixture that
    imitates litellm proves the imitation behaves as expected, and this suite has
    already been bitten by that shape of test.
    """
    from litellm.types.utils import Delta, ModelResponseStream, StreamingChoices

    return ModelResponseStream(
        id="chatcmpl-real-shape",
        model=MODEL,
        choices=[
            StreamingChoices(
                index=0,
                delta=Delta(content=content, role=role),
                finish_reason=finish_reason,
            )
        ],
    )


def real_usage_chunk(prompt: int, completion: int) -> Any:
    """The final usage chunk, as litellm actually emits it: `choices == []` and the
    `Usage` riding along as an extra attribute of a `ModelResponseStream`."""
    from litellm.types.utils import ModelResponseStream, Usage

    return ModelResponseStream(
        id="chatcmpl-real-shape",
        model=MODEL,
        choices=[],
        usage=Usage(
            prompt_tokens=prompt, completion_tokens=completion, total_tokens=prompt + completion
        ),
    )


async def test_the_adapter_reads_a_real_litellm_stream_chunk() -> None:
    """**The shape the real library returns, not the shape the stub returns.**

    The counterpart of the model-response test one file over, for the streaming
    surface. `litellm.types.utils.ModelResponseStream` is a pydantic model: reading it
    with subscript-and-`get` as the old adapter read `ModelResponse` raises for every
    chunk. No socket — this is the library's data model, not a call.
    """
    chunks = await collect(make_provider(chunks=[real_chunk("Hel", role="assistant")]))

    assert chunks == [StreamChunk(content="Hel", role="assistant")]


async def test_the_adapter_reads_the_real_usage_chunk_with_no_choices() -> None:
    """**The chunk that carries the token counts, built from litellm's own classes.**

    This is the test the whole streaming surface turns on. The real usage chunk has
    `choices == []` and its `Usage` as an *extra* attribute — `usage` is not in
    `ModelResponseStream.model_fields` at all. So the two obvious implementations are
    both wrong: `chunk["choices"][0]` is an `IndexError`, and reading the declared
    fields misses the counts entirely. A stream whose tokens cannot be read is a
    completion whose spend is never billed, which is the exact failure
    `muse.metering`'s module docstring exists to prevent.
    """
    from litellm.types.utils import ModelResponseStream

    chunk = real_usage_chunk(12, 8)
    # The two facts this test depends on, asserted first so a failure names the drift
    # rather than appearing as a wrong number. (AGENTS.md: assert the floor before
    # asserting the absence.)
    assert "usage" not in ModelResponseStream.model_fields, (
        "litellm now DECLARES usage on ModelResponseStream; the extra-attribute "
        "reading below still works, but this test's premise has moved"
    )
    assert chunk["choices"] == []

    chunks = await collect(make_provider(chunks=[chunk]))

    assert chunks == [StreamChunk(tokens_in=12, tokens_out=8)]


async def test_the_adapter_reads_a_real_finish_reason() -> None:
    """`finish_reason` arrives only on the last chunk, and `None` elsewhere means
    "still going" rather than "finished for no reason"."""
    chunks = await collect(
        make_provider(chunks=[real_chunk("lo"), real_chunk(None, finish_reason="stop")])
    )

    assert chunks[1].content is None, "the final chunk carries a finish, not text"
    assert chunks[1].finish_reason == "stop"


async def test_the_adapter_keeps_the_role_only_first_chunk() -> None:
    """A stock OpenAI client reads the role off the first chunk, so dropping it
    changes what the caller assembles. `content is None` is not `content == ""`."""
    chunks = await collect(make_provider(chunks=[real_chunk(None, role="assistant")]))

    assert chunks == [StreamChunk(role="assistant")]


# --- the request sent upstream ----------------------------------------------


async def test_the_stream_asks_for_usage_whatever_the_caller_asked_for() -> None:
    """**muse always asks the vendor for usage, and the caller's choice is separate.**

    A stream's token counts arrive only in a final usage chunk, and muse meters every
    completion it serves — so a stream sent without `include_usage` is one whose spend
    is never billed. Whether *usage goes on the wire* is the caller's business, decided
    upstream in the API layer; whether muse asks the vendor for it is not.
    """
    stub = make_stub(chunks=[])
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)

    _ = await collect(provider)

    assert stub.captured["stream"] is True
    assert stub.captured["stream_options"] == {"include_usage": True}


async def test_the_stream_sends_the_same_request_the_non_streaming_path_sends() -> None:
    """`max_tokens` and `temperature` cross the provider seam identically either way.

    A stream that dropped `temperature`, or sent `max_tokens` on one path and not the
    other, would answer a different question from the same body — and only a caller
    comparing the two would find out.
    """
    stub = make_stub(chunks=[])
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)

    await collect(provider, max_tokens=64, temperature=0.25)

    assert stub.captured["max_tokens"] == 64
    assert stub.captured["temperature"] == 0.25


async def test_a_stream_without_a_credential_is_refused_before_any_request() -> None:
    """No request, no spend, no rate-limit counter spent on a credential that was
    never going to work — the same check `complete()` makes, for the same reason."""
    provider = make_provider(api_key="")
    with pytest.raises(CredentialUnavailable, match=OPENAI):
        await collect(provider)


# --- failures, and WHERE they arrive ---------------------------------------


async def test_a_failure_while_obtaining_the_stream_is_mapped() -> None:
    """A failure at the call itself: the same taxonomy `complete()` maps, because the
    mapping is one table and a stream must not be the path that skips it."""
    with pytest.raises(ProviderRateLimited, match="rate limited"):
        await collect(make_provider(chunks=[], raises="RateLimitError"))


async def test_a_failure_mid_walk_is_mapped_too() -> None:
    """**The failure that arrives while ITERATING, not while awaiting.**

    This is the case a coroutine-shaped implementation gets wrong. `acompletion(
    stream=True)` answers with a stream object; a vendor's real fault is usually a
    socket reset three chunks in. Wrapping only the `await` lets it escape as a raw
    litellm exception: the router cannot advance past a class it does not recognise,
    and `_translate`'s credential scrub never runs over vendor text. The adapter is an
    async generator precisely so the `try` can span the walk.
    """
    with pytest.raises(ProviderRateLimited):
        await collect(make_provider(chunks=[real_chunk("par"), "boom:RateLimitError"]))


async def test_a_mid_walk_failure_reaches_the_default_mapping_too() -> None:
    """An exception class the table has no row for still arrives as the retryable
    default, mid-walk, exactly as it would at the call. Same table, same answer —
    which is the whole point of not having a second mapping for streams."""
    with pytest.raises(ProviderUnavailable):
        await collect(make_provider(chunks=[real_chunk("par"), "boom:APIConnectionError"]))


async def test_a_failure_mid_walk_scrubs_the_credential() -> None:
    """The scrub runs on the mid-walk path too, for the same reason: a vendor that
    echoes the key it rejected writes it to every log downstream otherwise."""
    with pytest.raises(ProviderRateLimited) as caught:
        await collect(make_provider(chunks=[real_chunk("par"), "boom:RateLimitError", "sk-test"]))

    assert "sk-test" not in str(caught.value)


async def test_a_mid_walk_timeout_is_indeterminate_not_a_retryable_timeout() -> None:
    """A deadline hit after chunks were already sent may already have been billed by
    the vendor, so it is `ProviderIndeterminate` — not in the retry tuple. Identical to
    the non-streaming case, because it is the same `_translate`."""
    with pytest.raises(ProviderIndeterminate):
        await collect(make_provider(chunks=[real_chunk("par"), "boom:Timeout"]))


# --- shape refusals ---------------------------------------------------------


async def test_a_chunk_with_neither_content_nor_usage_is_refused() -> None:
    """A chunk that carries neither text nor numbers is not a completion, and a
    completion that cannot be metered is one whose spend is never billed. Refused
    rather than skipped, exactly as `_to_completion` refuses a response with no
    usage."""
    with pytest.raises(ResponseShapeError, match="neither content nor usage"):
        await collect(make_provider(chunks=[{"choices": [], "usage": None}]))


async def test_a_chunk_with_a_choice_but_no_delta_is_refused() -> None:
    """The streaming counterpart of "a choice with no message": a choice carrying no
    delta has nothing to hand the caller, and skipping it would silently drop content
    the caller is owed."""
    with pytest.raises(ResponseShapeError, match="no delta"):
        await collect(make_provider(chunks=[{"choices": [{"index": 0}], "usage": None}]))


async def test_a_request_rejected_by_the_vendor_is_mapped() -> None:
    """A malformed request is the vendor's `BadRequestError` and muse's
    `ProviderInvalidRequest` — the mapping the whole table exists for, reached over a
    stream."""
    with pytest.raises(ProviderInvalidRequest):
        await collect(make_provider(chunks=[], raises="BadRequestError"))


async def test_a_stream_carries_the_traceparent_out_when_something_is_tracing() -> None:
    """Trace propagation is read from the ambient context, not passed in, so it has to
    work on the streaming path for the same reason it works on the non-streaming one —
    a vendor's own telemetry joining a trace that the streamed call dropped would put
    two halves of one request in two traces."""
    from muse.telemetry import TRACEPARENT_HEADER

    from .support.tracing import recording_telemetry

    telemetry, _ = recording_telemetry()
    stub = make_stub(chunks=[])
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)

    with telemetry.span("muse.test"):
        _ = await collect(provider)

    assert TRACEPARENT_HEADER in stub.captured["extra_headers"]


async def test_a_stream_sends_no_traceparent_when_nothing_is_tracing() -> None:
    """Empty rather than fabricated: an invented trace id would drop muse's spans into
    whatever trace the vendor assigned, making "we propagate the caller's trace"
    untrue in the one configuration nobody configured."""
    stub = make_stub(chunks=[])
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)

    _ = await collect(provider)

    assert "extra_headers" not in stub.captured


# --- the doubles ------------------------------------------------------------


async def test_the_fake_provider_streams_the_content_it_would_have_returned() -> None:
    """The double and the real adapter must agree about what a completion *is*, or a
    router test written against the double proves something the production adapter
    does not do."""
    from muse.providers.fake import FakeProvider

    provider = FakeProvider(name="fake", content="one two three", tokens_in=7, tokens_out=4)
    chunks = [chunk async for chunk in provider.stream(ask())]
    completion = await provider.complete(ask())

    streamed = "".join(chunk.content for chunk in chunks if chunk.content is not None)
    assert streamed.strip() == completion.content
    assert chunks[0].role == "assistant", "the role-only opener is not optional"
    # The finish chunk is second-to-last: the usage chunk is last, which is what a real
    # provider sends and what the router's settlement reads.
    assert chunks[-2].finish_reason == "stop"
    assert chunks[-1].tokens_in == 7 and chunks[-1].tokens_out == 4, (
        "a double that omitted usage would teach the router to meter streamed "
        "completions as zero tokens"
    )


async def test_the_scripted_provider_streams_and_raises_the_same_script() -> None:
    """One script, both paths, consumed the same way — so a test that writes a script
    for a route exercises streaming and non-streaming with it, and the two cannot
    disagree about what a candidate did.

    The error is raised *before* the first chunk, which is the only point a fallback
    chain may still advance. That is a property of the double, stated here so a
    successor who scripts an error mid-stream knows the router will not fall back.
    """
    from muse.errors import ProviderRateLimited
    from muse.providers import Completion, Price
    from muse.providers.fake import ScriptedProvider

    scripted = Completion(
        provider="scripted",
        model=MODEL,
        content="one two",
        tokens_in=3,
        tokens_out=2,
        price=Price(10, 20),
        finish_reason="length",
    )
    provider = ScriptedProvider(
        name="scripted", errors=[ProviderRateLimited("nope")], completions=[scripted]
    )

    with pytest.raises(ProviderRateLimited):
        _ = await collect(provider)

    chunks = [chunk async for chunk in provider.stream(ask())]
    assert [chunk.content for chunk in chunks if chunk.content] == ["one two"]
    assert chunks[-2].finish_reason == "length", "the scripted finish crosses over"
    assert chunks[-1].tokens_out == 2, "and the scripted usage crosses over too"


async def test_the_scripted_provider_refuses_a_stream_past_the_end_of_its_script() -> None:
    """The same loud failure `complete()` raises, for the same reason: an exhausted
    script is a test bug, and silently replaying nothing would look like an empty
    answer the caller is owed content in."""
    from muse.providers.fake import ScriptedProvider

    provider = ScriptedProvider(name="scripted", completions=[])
    with pytest.raises(AssertionError, match="no scripted completion left"):
        _ = await collect(provider)
