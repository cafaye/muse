"""`Router.stream`: where the fallback chain stops, and what a failure does.

**The claim under test is the one this packet exists to get right.** muse walks
ordered candidates until one answers. Over a non-streaming request that is simple: a
failure advances to the next candidate and the caller never learns. Over a *stream* it
is not — once a chunk has reached the caller, the next candidate cannot be used,
because the caller already holds bytes and two answers would be spliced into one body.

So the tests here are mostly about the line:

- a failure **before** the first chunk advances the chain (the existing repair, intact);
- a failure **after** the first chunk does not, cannot, and is reported as a settlement;
- and the breaker is told either way, so the *next* request avoids the candidate.

The last one is the half that is easy to forget. A provider that fails reliably three
chunks in is a very common way for a provider to be unhealthy, and a breaker that only
saw pre-first-chunk failures would learn nothing about it — the route would keep
picking a provider that reliably dies mid-answer, for ever.

**A client that stops reading is not a failure.** `test_a_caller_that_stops_reading...`
covers it: muse cannot distinguish a disconnect from a caller that got what it wanted,
must not record it against the provider, and must not bill a completion nobody read to
the end. Nothing here simulates a socket — the generator is simply abandoned, which is
what an abandoned `StreamingResponse` does.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from muse.errors import (
    AllCandidatesFailed,
    ProviderIndeterminate,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderUnavailable,
)
from muse.providers import (
    Completion,
    CompletionRequest,
    Price,
    StreamChunk,
)
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.router import Router, StreamResult

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PRICE = Price(1000, 2000)


def ask(model: str = "fast") -> list[tuple[str, str]]:
    return [("user", "hello")]


async def chunks_of(router: Router, model: str = "fast") -> list[StreamResult]:
    """Every result the router streams, in order."""
    return [result async for result in router.stream(model, ask())]


async def completion_of(router: Router, model: str = "fast") -> Completion:
    """The completion a settled stream produced, or fail loudly if it did not."""
    results = await chunks_of(router, model)
    settled = results[-1].settled
    assert settled is not None, "a stream must always settle"
    assert settled.completion is not None, f"stream did not complete: {settled.error!r}"
    return settled.completion


class DyingMidStream:
    """A provider that answers, then fails. The defect this packet is about.

    Wraps a real `FakeProvider` so the pre-first-chunk behaviour is the double's and
    only the failure is added. Raising *after* a chunk rather than before is the whole
    point: a stub that fails at the call cannot express the splice question at all.
    """

    def __init__(
        self,
        *,
        name: str = "primary",
        after: int = 2,
        error: Exception | None = None,
        price: Price = PRICE,
        content: str = "one two three",
    ) -> None:
        self._inner = FakeProvider(name=name, content=content, price=price)
        self._after = after
        self._error = error or ProviderUnavailable(f"{name} reset the socket")
        self.name = name
        self.calls = 0
        self.requests: list[CompletionRequest] = []

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        self.requests.append(request)
        yield StreamChunk(role="assistant")
        sent = 0
        async for chunk in self._inner.stream(request):
            if chunk.content is None:
                continue
            sent += 1
            if sent > self._after:
                raise self._error
            yield chunk

    async def complete(self, request: CompletionRequest) -> Completion:
        return await self._inner.complete(request)

    async def health(self):  # pragma: no cover - never called in these tests
        return await self._inner.health()

    def cost_per_1k_tokens(self, model: str) -> Price:
        return self._inner.cost_per_1k_tokens(model)


def router_with(providers: list, table) -> Router:
    """A router over the given providers, with no sleeps and a pinned clock.

    Nothing here waits: the injected sleep and clock are the rule-14 technique, so the
    suite asserts on the schedule the policy asked for rather than on elapsed time.
    """
    from muse.breaker import BreakerRegistry

    registry = type(
        "Registry",
        (),
        {
            "get": staticmethod(lambda name: next(p for p in providers if p.name == name)),
        },
    )()
    return Router(
        registry,  # type: ignore[arg-type]
        table,
        sleep=_never_sleep,
        clock=_ticks(),
        breakers=BreakerRegistry(),
    )


async def _never_sleep(seconds: float) -> None:
    raise AssertionError(f"a stream must not sleep; asked for {seconds}s")


def _ticks():
    """A monotonic clock that advances a fixed amount per reading."""
    state = {"now": 0.0}

    def clock() -> float:
        state["now"] += 1.0
        return state["now"]

    return clock


@pytest.fixture
def table():
    """One route, two candidates, so a fallback has somewhere to go."""
    from muse.routes import Candidate, RetryPolicy, Route, RouteTable

    return RouteTable(
        version=1,
        defaults=RetryPolicy(),
        routes=[
            Route(
                model="fast",
                candidates=[
                    Candidate(provider="primary", model="gpt-4o-mini"),
                    Candidate(provider="backup", model="claude-haiku-4-5"),
                ],
            )
        ],
    )


# --- the chain, before the first chunk -------------------------------------


async def test_a_stream_advances_the_chain_before_the_first_chunk(table) -> None:
    """**The existing repair, intact.** A provider that fails before it has answered
    is exactly what the fallback chain is for, and it has produced nothing the caller
    could be confused by. So the backup answers and the caller never learns the first
    provider was tried."""
    primary = ScriptedProvider(
        name="primary",
        errors=[ProviderRateLimited("slow down")],
        completions=[],
    )
    backup = FakeProvider(name="backup", content="from the backup", price=PRICE)

    completion = await completion_of(router_with([primary, backup], table))

    assert completion.content.strip() == "from the backup"
    assert completion.provider == "backup", "the vendor that answered is recorded"
    assert primary.calls == 1


async def test_every_candidate_failing_still_raises_before_any_chunk(table) -> None:
    """`AllCandidatesFailed` with the chain, so the API layer renders one 503 for a
    stream that never began — the same 503 a non-streaming request gets."""
    failing = ScriptedProvider(
        name="primary",
        errors=[ProviderRateLimited("slow down")],
        completions=[
            Completion(
                provider="primary", model="m", content="x", tokens_in=1, tokens_out=1, price=PRICE
            )
        ],
    )
    failing._errors = [ProviderRateLimited("slow down")] * 5
    backup = ScriptedProvider(
        name="backup",
        errors=[ProviderUnavailable("down")],
        completions=[
            Completion(
                provider="backup", model="m", content="y", tokens_in=1, tokens_out=1, price=PRICE
            )
        ],
    )
    backup._errors = [ProviderUnavailable("down")] * 5

    with pytest.raises(AllCandidatesFailed) as caught:
        await chunks_of(router_with([failing, backup], table))

    assert [failure.provider for failure in caught.value.failures] == ["primary", "backup"]


async def test_an_open_breaker_is_a_refusal_that_never_opens_a_stream(table) -> None:
    """The same rule as the non-streaming path: held back before any I/O.

    Asserted on the provider's call count rather than on the breaker's state, because
    the property is "no request was made", not "a flag is set". `DyingMidStream`
    always fails, so the threshold is reached and the next request must not touch it.
    """
    primary = DyingMidStream(after=1)
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)
    router = router_with([primary, backup], table)

    # The threshold is 5 (see `muse.breaker.DEFAULT_BREAKER_THRESHOLD`); one more than
    # that, so the breaker is open before the assertion rather than during it.
    for _ in range(6):
        await chunks_of(router)

    assert router.breakers.for_provider("primary").state.name == "OPEN"
    calls_at_open = primary.calls

    await chunks_of(router)

    assert primary.calls == calls_at_open, "an open breaker must not open a stream"
    assert (await completion_of(router)).provider == "backup"


async def test_an_unpriced_model_is_never_streamed(table) -> None:
    """The price is resolved before dispatch, exactly as on the non-streaming path: a
    model muse cannot price is never called, because a completion that cannot be
    metered is one whose spend is never billed."""
    primary = ScriptedProvider(
        name="primary",
        # A table that does NOT cover the route's vendor model: per
        # `ScriptedProvider.cost_per_1k_tokens` a model missing from a non-empty table
        # is a deliberate "this provider cannot serve that model", which is exactly the
        # case being tested. (An empty dict is not — it means "no overrides".)
        prices={"some-other-model": PRICE},
        completions=[
            Completion(
                provider="primary", model="m", content="x", tokens_in=1, tokens_out=1, price=PRICE
            )
        ],
    )
    backup = FakeProvider(name="backup", content="backup", price=PRICE)

    completion = await completion_of(router_with([primary, backup], table))

    assert completion.content.strip() == "backup", "the unpriced candidate was skipped"
    assert completion.provider == "backup"
    assert primary.calls == 0, "and it was never called"


async def test_an_unknown_model_is_refused_before_any_provider(table) -> None:
    from muse.errors import RouteNotFound

    router = router_with([FakeProvider(name="primary")], table)
    with pytest.raises(RouteNotFound):
        await chunks_of(router, "no-such-model")


# --- THE LINE: a failure after the first chunk ------------------------------


async def test_a_failure_after_the_first_chunk_does_not_fall_back(table) -> None:
    """**The invariant this packet exists to hold.**

    The primary dies three chunks in. The caller already has text, so the backup is
    unreachable — and this asserts that by *counting*: if the chain advanced, `backup`
    would have been called. Two answers would otherwise be spliced into one body with
    nothing marking the seam.
    """
    primary = DyingMidStream(after=2)
    backup = FakeProvider(name="backup", content="SPLICED IN", price=PRICE)

    results = await chunks_of(router_with([primary, backup], table))

    assert backup.calls == 0, "the fallback chain must not run after the first chunk"
    assert results[-1].settled is not None
    assert results[-1].settled.completion is None, "a failed stream is not a completion"
    assert isinstance(results[-1].settled.error, ProviderUnavailable)


async def test_a_failure_after_the_first_chunk_still_tells_the_breaker(table) -> None:
    """**Does the *next* request avoid that candidate? Yes.**

    A provider that fails reliably mid-stream is a very common way for a provider to be
    unhealthy. If the breaker only saw pre-first-chunk failures it would learn nothing
    from this, and the route would keep picking a provider that reliably dies
    mid-answer, for ever.
    """
    primary = DyingMidStream(after=2)
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)
    router = router_with([primary, backup], table)

    # Past the threshold (5), so the assertion below is about a *closed-then-open*
    # transition caused by mid-stream failures alone.
    for _ in range(6):
        await chunks_of(router)

    assert router.breakers.for_provider("primary").state.name == "OPEN", (
        "a provider that dies mid-stream must be held back after the threshold"
    )


async def test_the_caller_keeps_the_chunks_that_arrived_before_the_failure(table) -> None:
    """The bytes already sent are the caller's, and dropping them would make a partial
    answer indistinguishable from no answer. The settlement that follows is what says
    "this stopped"."""
    primary = DyingMidStream(after=2, content="one two three")
    backup = FakeProvider(name="backup", content="backup", price=PRICE)

    results = await chunks_of(router_with([primary, backup], table))

    text = "".join(r.chunk.content for r in results if r.chunk and r.chunk.content)
    assert text == "one two ", "the caller keeps what genuinely arrived"
    assert results[-1].settled.error is not None, "and is told that it stopped"


async def test_a_non_provider_error_also_ends_the_stream_without_tripping_the_breaker(
    table,
) -> None:
    """A bug in muse is not the provider being unhealthy. The stream still ends — the
    caller must not be left waiting on a generator that will never yield again — but
    the breaker is not told, because a defect here would otherwise take a vendor out
    of rotation."""
    primary = DyingMidStream(after=2, error=TypeError("a bug in muse"))
    backup = FakeProvider(name="backup", content="backup", price=PRICE)
    router = router_with([primary, backup], table)

    results = await chunks_of(router, "fast")

    assert isinstance(results[-1].settled.error, TypeError)
    assert router.breakers.for_provider("primary").state.name == "CLOSED", (
        "a muse defect must not hold back a provider"
    )


async def test_an_indeterminate_failure_before_the_first_chunk_is_not_retried(table) -> None:
    """`ProviderIndeterminate` means the vendor may already have billed it. It is not
    in the retry tuple, so the chain moves on rather than re-dispatching — and here
    that means the *backup* answers, which is a different vendor, so nothing is billed
    twice."""
    primary = ScriptedProvider(
        name="primary",
        errors=[ProviderIndeterminate("may have been billed")],
        completions=[
            Completion(
                provider="primary", model="m", content="x", tokens_in=1, tokens_out=1, price=PRICE
            )
        ],
    )
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)

    completion = await completion_of(router_with([primary, backup], table))

    assert completion.provider == "backup"
    assert primary.calls == 1, "the primary is dispatched once, never twice"


# --- settlement -------------------------------------------------------------


class SaysNothing:
    """A provider whose stream is empty. A real failure mode — a vendor that closes
    the connection having sent nothing — and the one case where a stream cannot be
    told apart from a refusal by anything the caller can see."""

    name = "primary"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        return
        yield  # pragma: no cover - makes this an async generator that yields nothing

    async def complete(self, request: CompletionRequest) -> Completion:
        raise AssertionError("not reached")

    async def health(self):  # pragma: no cover - never called here
        raise AssertionError("not reached")

    def cost_per_1k_tokens(self, model: str) -> Price:
        return PRICE


async def test_a_stream_that_ends_without_a_chunk_is_a_candidate_failure(table) -> None:
    """A provider that produced nothing is indistinguishable to a caller from one
    that refused, and its usage is unknown so there is nothing to meter. Recorded as a
    refusal, which is what it is, so the chain can move on and the backup answers."""
    primary = SaysNothing()
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)

    completion = await completion_of(router_with([primary, backup], table))

    assert completion.provider == "backup", "an empty stream is a refusal, not an answer"
    assert primary.calls == 1


class WithUsage:
    """A provider whose stream carries usage and a finish_reason the way a real one
    does: a role-only opener, content, a final chunk with `finish_reason`, and a
    usage-only closer with `choices == []`.

    `FakeProvider` deliberately does NOT do this — it reports its tokens as fields
    rather than in a chunk — so the path that reads usage off the wire is exercised
    here and nowhere else. That path is the one a real vendor takes and the one a
    stream's metering depends on.
    """

    name = "primary"

    def __init__(self) -> None:
        self.calls = 0

    async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
        self.calls += 1
        yield StreamChunk(role="assistant")
        yield StreamChunk(content="Hel")
        yield StreamChunk(content="lo")
        yield StreamChunk(finish_reason="stop")
        yield StreamChunk(tokens_in=12, tokens_out=7)

    async def complete(self, request: CompletionRequest) -> Completion:
        raise AssertionError("not reached")

    async def health(self):  # pragma: no cover - never called here
        raise AssertionError("not reached")

    def cost_per_1k_tokens(self, model: str) -> Price:
        return PRICE


async def test_a_stream_meters_the_usage_the_vendor_sent_in_its_final_chunk(
    table,
) -> None:
    """The tokens a real vendor reports arrive in a *chunk*, not on a field, and that
    is the only place a stream's counts can come from.

    Asserted through the settled completion rather than through the router's internals,
    because the completion is what the meter is handed — a router that counted chunks
    and ignored the usage would produce the right number here by accident.
    """
    completion = await completion_of(router_with([WithUsage()], table))

    assert (completion.tokens_in, completion.tokens_out) == (12, 7)
    assert completion.content == "Hello", "the chunks are joined, in order"
    assert completion.finish == "stop"
    assert completion.cost_micros > 0, "and the price resolved before dispatch prices it"


async def test_a_stream_ending_without_usage_meters_zero_rather_than_guessing(
    table,
) -> None:
    """A stream that finished with no usage chunk HAS finished, so it is metered — with
    zero output tokens, not with a count invented from the text.

    Tokenising the content to invent one would be a guess dressed as a measurement, and
    a guess that gets metered is a guess that gets billed. Zero under-bills, which is
    the recoverable direction.
    """
    completion = await completion_of(router_with([FakeProvider(name="primary")], table))

    assert completion.tokens_in == 0 and completion.tokens_out == 0
    assert completion.content.strip(), "the content still reached the caller"


async def test_an_empty_stream_from_the_only_candidate_fails_the_request(table) -> None:
    """With no backup to fall back to, an empty stream is a failed request rather than
    a silent success — so the chain raises and the endpoint can render its 503."""
    from muse.routes import Candidate, RetryPolicy, Route, RouteTable

    alone = RouteTable(
        version=1,
        defaults=RetryPolicy(),
        routes=[Route(model="fast", candidates=[Candidate(provider="primary")])],
    )

    with pytest.raises(AllCandidatesFailed) as caught:
        await chunks_of(router_with([SaysNothing()], alone))

    assert len(caught.value.failures) == 1
    assert "without a chunk" in caught.value.failures[0].detail
    assert isinstance(caught.value.failures[0].error, type(ProviderUnavailable))


async def test_a_first_chunk_that_carries_usage_is_read_for_the_settlement(
    table,
) -> None:
    """A vendor that puts its usage on the FIRST chunk rather than the last.

    Unusual, and it happens: a provider that reports a cached or pre-computed usage
    emits it up front, and a stream whose only chunk is the usage chunk is a stream
    that has finished. Reading usage from anywhere but the final chunk would meter this
    one as zero tokens, and the chunk carrying the numbers would be treated as
    contentless noise.
    """

    class UsageFirst:
        name = "primary"

        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(tokens_in=5, tokens_out=3)
            yield StreamChunk(content="hello")

        async def complete(self, request: CompletionRequest) -> Completion:
            raise AssertionError("not reached")

        async def health(self):  # pragma: no cover - never called here
            raise AssertionError("not reached")

        def cost_per_1k_tokens(self, model: str) -> Price:
            return PRICE

    completion = await completion_of(router_with([UsageFirst()], table))

    assert (completion.tokens_in, completion.tokens_out) == (5, 3)
    assert completion.content == "hello"


async def test_a_first_chunk_that_carries_a_finish_reason_is_read(table) -> None:
    """The mirror of the above: a finish reason arriving before the stream ends still
    counts, rather than being overwritten by nothing later."""

    class FinishFirst:
        name = "primary"

        async def stream(self, request: CompletionRequest) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(content="hi", finish_reason="stop")

        async def complete(self, request: CompletionRequest) -> Completion:
            raise AssertionError("not reached")

        async def health(self):  # pragma: no cover - never called here
            raise AssertionError("not reached")

        def cost_per_1k_tokens(self, model: str) -> Price:
            return PRICE

    assert (await completion_of(router_with([FinishFirst()], table))).finish == "stop"


async def test_a_non_transient_failure_before_the_first_chunk_does_not_trip_the_breaker(
    table,
) -> None:
    """A 400 is muse sending something the provider is right to refuse, so it must not
    hold the provider back — the same rule the non-streaming path applies, and it
    applies to the chain's refusal branch as well as the breaker branch.

    Asserted by pushing past the threshold and checking the breaker never opened: a
    non-transient failure that counted would take the route down for every caller.
    """
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)
    # Past the threshold (5), so the assertion is about a breaker that had every
    # opportunity to open and did not.
    primary = ScriptedProvider(
        name="primary",
        errors=[ProviderInvalidRequest("muse sent something invalid")] * 6,
        completions=[],
    )
    router = router_with([primary, backup], table)

    for _ in range(6):
        await completion_of(router)

    assert primary.calls == 6, "each request really did reach the provider"
    assert router.breakers.for_provider("primary").state.name == "CLOSED", (
        "six rejected-but-valid requests must not take a provider out of rotation"
    )


async def test_a_non_transient_failure_after_the_first_chunk_does_not_trip_the_breaker(
    table,
) -> None:
    """The same rule as before the first chunk, and it matters just as much here.

    A 400 that arrives mid-stream is still muse sending something the provider is
    right to refuse. Counting it would let one caller with a malformed request take the
    route down for everybody — the exact failure rule 17 of this repository exists to
    prevent, and the fact that it arrived after a chunk is not evidence about the
    provider's health.
    """
    primary = DyingMidStream(after=2, error=ProviderInvalidRequest("muse sent something invalid"))
    backup = FakeProvider(name="backup", content="backup answer", price=PRICE)
    router = router_with([primary, backup], table)

    for _ in range(6):
        results = await chunks_of(router)
        assert results[-1].settled.error is not None

    assert primary.calls == 6, "each request really did reach the provider"
    assert router.breakers.for_provider("primary").state.name == "CLOSED", (
        "a rejected request must not take a provider out of rotation"
    )


async def test_a_complete_stream_settles_with_the_route_and_completion(table) -> None:
    """The settlement carries what the meter needs — the completion and the route the
    caller asked for — so metering needs no second channel and cannot disagree with
    what the caller was served."""
    provider = FakeProvider(name="primary", content="one two", tokens_in=9, tokens_out=4)

    completion = await completion_of(router_with([provider], table))

    assert completion.content.strip() == "one two"
    assert completion.provider == "primary"
    assert completion.finish == "stop"


async def test_a_caller_that_stops_reading_is_not_a_provider_failure(table) -> None:
    """**A disconnect mid-stream is not the provider failing.**

    muse cannot tell a client that hung up from a client that got what it wanted, so it
    must not record one against the vendor, and must not bill a completion nobody read
    to the end. The test abandons the generator, which is exactly what an abandoned
    `StreamingResponse` does — no socket, no sleeps.
    """
    primary = DyingMidStream(after=10)
    backup = FakeProvider(name="backup", content="backup", price=PRICE)
    router = router_with([primary, backup], table)

    stream = router.stream("fast", ask())
    first = await anext(stream)
    assert first.chunk is not None
    await stream.aclose()

    assert router.breakers.for_provider("primary").state.name == "CLOSED", (
        "a caller hanging up must not take the provider out of rotation"
    )
    assert backup.calls == 0
