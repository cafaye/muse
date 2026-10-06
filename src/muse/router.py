"""The router: request in, completion out, with a fallback chain behind it.

Three cases define this module and the packet names all three: the primary
succeeds, the primary fails and a fallback succeeds, and everything fails. Each
gets its own section of the suite, and each asserts on the *whole* result — which
candidate served it, how many attempts each took, what the failure chain says —
because "it returned a completion" is not the property under test. "It returned the
fallback's completion after the primary was rate limited twice" is.

Five rules shape the implementation:

- **A retry is for a transient failure, and only a transient failure.** Retrying a
  rejected credential or a malformed request spends the caller's latency to arrive
  at the same answer. `muse.errors.RETRYABLE_PROVIDER_ERRORS` is that list, and it
  is explicit rather than derived from a base class, so adding a `ProviderError`
  subclass does not silently change retry behaviour.
- **The price is looked up before the call is dispatched.** A model muse cannot
  price is never called, because a completion that cannot be metered is one whose
  spend is never billed. That is why a test can assert an unpriced primary made
  *zero* provider calls.
- **An unpriced candidate is not retried.** It is skipped like any other failed
  candidate, and recorded in the chain — the alternative is asking a provider for a
  price `max_attempts` times and getting the same answer.
- **A candidate is never revisited.** Once a candidate is exhausted the router
  advances. Going back to the primary after the fallback failed would be a retry
  policy stated in two places.
- **The failure chain is data, not a formatted string.** `AllCandidatesFailed`
  carries each candidate's detail so the API layer renders it without re-deriving
  what happened, and so the suite asserts on the chain rather than a substring.

Packet `muse-03` adds the resilience layer around that, and the three additions are
ordered by how much damage getting them wrong would do:

- **A circuit breaker per provider** (`muse.breaker`), consulted *before* the
  provider is touched, so a held-back provider is never dialled. It sits outside the
  retry loop rather than inside it: a breaker that counted attempts would open
  `threshold` times faster than the threshold says.
- **A bounded budget** — attempts *and* wall clock, because a count alone does not
  bound time. The deadline is checked before each wait, so muse never begins a sleep
  that would carry the request past it.
- **Jittered backoff.** Without it every muse process that failed at the same
  instant retries at the same instant, and a recovering provider is hit by a wave
  rather than a trickle.

Everything time-shaped is injected — the sleep, the clock, the jitter source — so
the suite asserts on the *schedule the policy asked for* rather than on elapsed wall
time. That is what makes "the backoff grows, the ceiling holds, the budget stops the
loop" facts about this code rather than hopes about a machine (AGENTS.md rule 13).

Spans are created here too, through `muse.telemetry.Telemetry`. The attributes
recorded are model, provider, token counts, cost, latency, breaker state and the
error *class* — one of core's thirteen, through `muse.telemetry.record_error`,
never a prompt, a completion, or a credential, because `muse.telemetry` refuses
anything not on its allowlist and the canary test in
`tests/test_trace_propagation.py` asserts that against the exported payload.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception

from muse.breaker import BreakerRegistry, CircuitBreaker
from muse.errors import (
    AllCandidatesFailed,
    CandidateFailure,
    CircuitOpen,
    PriceUnavailable,
    ProviderError,
    ProviderIndeterminate,
    ProviderUnavailable,
    RouteNotFound,
    is_retryable,
    trips_breaker,
)
from muse.providers import (
    Completion,
    CompletionRequest,
    Message,
    Price,
    Provider,
    ProviderRegistry,
    StreamChunk,
)
from muse.redaction import Secret, redact
from muse.routes import BackoffPolicy, Candidate, RetryPolicy, Route, RouteTable
from muse.telemetry import Telemetry, record, record_error

#: A `(role, content)` pair as a caller states it. The router turns these into
#: `Message` values, so the endpoint and the router agree on one request shape
#: rather than each having its own.
MessagePair = tuple[str, str]

#: What the router is handed instead of a real sleep. A coroutine function, because
#: tenacity's async path awaits the result.
SleepFn = Callable[[float], Awaitable[None]]

#: Monotonic seconds. Injected so the budget is assertable without waiting; see the
#: module docstring.
Clock = Callable[[], float]

#: A draw in `[0, 1)` used to jitter each backoff. Injected for the same reason, and
#: so a test can pin the schedule exactly rather than asserting on a range.
Unit = Callable[[], float]


async def _asyncio_sleep(seconds: float) -> None:
    """The production sleep.

    Named and separate so a test can assert the default is a real one. A no-op
    default would make the backoff look instant in development and in the suite, and
    the bug it hides — a policy that never actually waits — is invisible in both.
    """
    await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class StreamResult:
    """One step of a stream: a chunk, or the settlement.

    Two shapes on one type rather than a union, because a caller iterating a stream
    should not have to branch on "am I done?" — and because the settlement arrives as
    a *step*, so a consumer that stops early simply never sees it. A `SettledStream`
    on its own would invite a caller to wait for it before reading chunks, which is the
    opposite of streaming.

    Exactly one field is set. `StreamResult.chunk_of` is the constructor that keeps
    that true.
    """

    chunk: StreamChunk | None = None
    settled: SettledStream | None = None

    @classmethod
    def chunk_of(cls, chunk: StreamChunk) -> StreamResult:
        return cls(chunk=chunk)

    @classmethod
    def settled_by(cls, settled: SettledStream) -> StreamResult:
        return cls(settled=settled)


@dataclass(frozen=True, slots=True)
class SettledStream:
    """How a stream ended: the completion it produced, or what stopped it.

    `error` set means the stream failed, and `completion` is then `None` — never a
    partial one. A completion assembled from the chunks that arrived before a failure
    would be a *guess* at what the caller was owed, and a guess that gets metered is a
    guess that gets billed.

    The tokens of a stream that failed *are* known and *are* dropped. That is the
    deliberate half of the metering answer: muse records a completion it can prove
    finished, and a failed stream is not one. The alternative — meter the partial —
    would require inventing a `finish_reason` and a `content` for a generation the
    vendor never finished, and rule 11's "a completion that cannot be metered is never
    dispatched" is the mirror of "a stream that did not complete is not metered".
    """

    completion: Completion | None = None
    route: str | None = None
    error: BaseException | None = None


class _StreamAttempt:
    """One candidate's streaming attempt, and the span it happens in.

    Its own object rather than a method on `Router` for one structural reason: the
    `muse.provider.call` span has to stay open for the whole walk, while the fallback
    chain has to resume at the *first chunk*. Holding the span open across a `yield`
    means the attempt is a generator that owns a context manager — which is exactly
    what a generator is for.
    """

    def __init__(
        self,
        router: Router,
        provider: Provider,
        candidate: Candidate,
        model: str,
        breaker: CircuitBreaker,
        request: CompletionRequest,
        price: Price,
        secrets: Sequence[Secret | str],
    ) -> None:
        self._router = router
        self._provider = provider
        self._candidate = candidate
        self._model = model
        self._breaker = breaker
        self._request = request
        #: Resolved by the chain *before* dispatch, for the same reason and with the
        #: same consequence as the non-streaming path: a model muse cannot price is
        #: never called, because a completion that cannot be metered is one whose
        #: spend is never billed. Carried onto the completion rather than looked up
        #: again at the end, because a second lookup could read a different row of a
        #: table that moved while the stream was open.
        self._price = price
        self._secrets = secrets

    async def chunks(self, index: int) -> AsyncIterator[StreamResult]:
        """Every chunk this candidate produces, then exactly one settlement.

        **The span stays open across the walk on purpose.** A provider call that
        fails on chunk three is a call that was made and that failed, and a span closed
        at the first chunk would report a successful call that then had a failure
        somewhere unaccounted for. Closing it when the stream ends makes the span's
        latency the whole stream's latency, which is the number an operator wants.

        Yielding the settlement rather than raising is what keeps "the stream failed"
        distinguishable from "the stream ended": a caller that stops iterating early
        (a client disconnect, a `max_tokens` the caller stops at) simply never reaches
        the settlement, and cannot mistake it for one.

        **Two guards, not one, and the split IS the fallback line.** The first chunk
        is pulled outside the walk guard so a failure there propagates to
        `Router.stream`, which is the only place a chain may advance. Everything after
        it is inside the guard and cannot propagate, because by then the caller holds
        bytes.

        One guard around the whole walk is the obvious way to write this, and it is
        silently wrong: a pre-first-chunk failure then arrives as a settlement like
        every other one, so the chain can never advance and every refused provider
        reads as a provider that started and stopped. That version is *more*
        forgiving and strictly worse — it turns "this provider refused" into "this
        provider produced a truncated answer", which reports a healthy vendor as a
        fault and costs every request the fallback it should have got.
        """
        started = self._router._clock()
        chunks: list[StreamChunk] = []
        tokens_in: int | None = None
        tokens_out: int | None = None
        finish: str | None = None
        # `detached_span`, not `span`: this block spans a `yield`, and Starlette
        # iterates a streaming body from a different task than the one that started it,
        # so an attached span would fail to detach on the way out and turn a successful
        # stream into a 500. See `Telemetry.span`'s docstring for the whole story.
        with self._router._telemetry.detached_span(
            "muse.provider.call",
            muse_provider=self._candidate.provider,
            muse_model=self._model,
            muse_candidate_index=index,
            muse_breaker_state=str(self._breaker.state),
        ) as span:
            stream = self._provider.stream(self._request)
            try:
                # Outside the guard, deliberately. Opening a generator does not run it,
                # so this is where the credential check, the vendor call, and the
                # failure that usually kills a stream all actually happen.
                #
                # The `with` here is an ATTACHED span, and deliberately the only one on
                # this path: it is what makes the outbound `traceparent` carry this
                # call's span id, which `detached_span` cannot do because it never
                # becomes current. It closes before the first yield, so it never spans a
                # suspension and cannot hit the detach failure the detached span exists
                # to avoid. The walk below continues on the detached span, which nests
                # under this one.
                with self._router._telemetry.span(
                    "muse.provider.connect",
                    muse_provider=self._candidate.provider,
                    muse_model=self._model,
                ):
                    first = await anext(stream)
            except StopAsyncIteration as error:
                # A provider that produced NOTHING: no chunk, no usage, nothing to
                # meter and nothing to show a caller.
                #
                # It has to become a `ProviderError` here rather than being allowed to
                # propagate, because `StopAsyncIteration` cannot cross an async
                # generator's boundary — the runtime rewrites it to `RuntimeError:
                # async generator raised StopAsyncIteration`, which no `except` clause
                # in `Router.stream` would recognise and which the chain would report
                # as a crash rather than a refusal. Translating it here is what lets
                # the chain record it as a candidate failure and move on.
                unavailable = ProviderUnavailable(
                    f"{self._candidate.provider} ended the stream without a chunk"
                )
                record_error(span, unavailable)
                raise unavailable from error
            except ProviderError as error:
                # Recorded on the span so the failure is traced, then propagated so the
                # chain can advance: both, and in that order.
                record_error(span, error)
                raise
            chunks.append(first)
            if first.finish_reason is not None:
                finish = first.finish_reason
            if first.tokens_in is not None:
                tokens_in = first.tokens_in
            if first.tokens_out is not None:
                tokens_out = first.tokens_out
            yield StreamResult.chunk_of(first)

            # From here the caller holds bytes, so nothing in this block can reach the
            # chain: every failure becomes a settlement rather than an exception.
            try:
                async for chunk in stream:
                    chunks.append(chunk)
                    if chunk.finish_reason is not None:
                        finish = chunk.finish_reason
                    if chunk.tokens_in is not None:
                        tokens_in = chunk.tokens_in
                    if chunk.tokens_out is not None:
                        tokens_out = chunk.tokens_out
                    record(span, muse_latency_ms=int((self._router._clock() - started) * 1000))
                    yield StreamResult.chunk_of(chunk)
            except ProviderError as error:
                # A failure past the first chunk. The chain cannot advance from here,
                # so this is reported as a settlement rather than raised — and the
                # breaker is told, because the NEXT request must not pay this fare
                # again. A provider that dies reliably mid-stream is a very common way
                # for a provider to be unhealthy, and a breaker that only saw
                # pre-first-chunk failures would learn nothing about it.
                if trips_breaker(error):
                    self._breaker.record_failure()
                record_error(span, error)
                yield StreamResult.settled_by(SettledStream(error=error))
                return
            except Exception as error:
                # Not a `ProviderError`: a defect in muse, or an exception the adapter
                # did not translate. It still ends the stream — the caller must not be
                # left waiting — but it is neither a candidate failure nor a reason to
                # touch the breaker, because a bug in muse is not the provider being
                # unhealthy.
                record_error(span, error)
                yield StreamResult.settled_by(SettledStream(error=error))
                return

            self._breaker.record_success()
            completion = Completion(
                provider=self._provider.name,
                model=self._model,
                content=_join(chunks),
                # `or 0`, not None: a stream that ended without a usage chunk HAS
                # finished, and `Completion` cannot carry "unknown". Zero under-bills;
                # inventing a count would meter tokens nobody is charged for.
                tokens_in=tokens_in or 0,
                tokens_out=tokens_out or 0,
                price=self._price,
                finish_reason=finish,
            )
            record(
                span,
                muse_tokens_in=completion.tokens_in,
                muse_tokens_out=completion.tokens_out,
                muse_cost_micros=completion.cost_micros,
            )
        yield StreamResult.settled_by(
            SettledStream(completion=completion, route=self._candidate.model or self._model)
        )


def _join(chunks: Sequence[StreamChunk]) -> str:
    """The text a caller assembled from these chunks, as one string.

    Only `str` content, and only once each: a chunk with no content contributes nothing
    (the role-only opener, the usage-only closer), and the pieces are joined in the
    order they arrived. The result is what the caller would have concatenated, so
    metering it charges for the same bytes the caller received rather than for a
    reconstruction that happens to agree.
    """
    return "".join(chunk.content for chunk in chunks if chunk.content is not None)


def backoff_delay(policy: BackoffPolicy, index: int, unit: float) -> float:
    """The wait after the failure that was attempt number `index + 1`.

    A named function rather than a lambda inside the loop, so the schedule can be
    asserted on its own — the alternative is a policy that can only be tested by
    running a whole route and reading a list of sleeps out of a side channel.
    """
    return policy.delay(index, unit)


@dataclass(frozen=True, slots=True)
class RoutedCompletion:
    """A completion plus how it was obtained.

    The two counters are separate because a dashboard needs to tell them apart. A
    route whose `candidates_tried` is climbing is falling back constantly; one whose
    `attempts` is climbing is being retried. One number for both makes the two look
    like the same problem, and they have different fixes.
    """

    completion: Completion
    #: The route the client asked for. Distinct from `completion.model`, which is the
    #: vendor's model id: a caller that configured `smart` must see `smart` back
    #: whichever vendor answered.
    route: str
    model: str
    #: How many distinct candidates were tried. 1 means the primary worked.
    candidates_tried: int
    #: How many provider calls were made in total, including retries.
    attempts: int

    @property
    def cost_micros(self) -> int:
        """What the call cost, in micro-dollars.

        Read from the completion rather than recomputed: the completion carries the
        price resolved at dispatch time, and re-deriving it here could read a
        different row of a table that moved in between.
        """
        return self.completion.cost_micros


@dataclass(frozen=True, slots=True)
class _Attempt:
    """What one candidate did.

    Either a completion or a failure, never both and never neither. Returning this
    rather than raising keeps "a candidate failed" on the normal path through a
    fallback chain instead of making it exceptional.
    """

    completion: Completion | None = None
    failure: CandidateFailure | None = None
    calls: int = 0


class Router:
    """Resolves a model to an ordered candidate chain and runs it.

    Stateless apart from its collaborators, so one router serves every request. All
    per-request state is a local in `route()` — a counter on `self` would be shared
    between concurrent requests, which is the class of bug the app-factory isolation
    tests exist to catch.

    The breaker registry is the one piece of shared mutable state here, and that is
    the point: a provider being held back is a fact about the provider, not about the
    request that noticed.
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        routes: RouteTable,
        *,
        sleep: SleepFn = _asyncio_sleep,
        clock: Clock = time.monotonic,
        unit: Unit = random.random,
        telemetry: Telemetry | None = None,
        breakers: BreakerRegistry | None = None,
    ) -> None:
        self._registry = registry
        self._routes = routes
        self._sleep = sleep
        self._clock = clock
        self._unit = unit
        self._telemetry = telemetry if telemetry is not None else Telemetry.noop()
        self._breakers = breakers if breakers is not None else BreakerRegistry()

    @property
    def routes(self) -> RouteTable:
        """The routing table, for a readiness body or a future admin surface."""
        return self._routes

    @property
    def breakers(self) -> BreakerRegistry:
        """The per-provider breakers, for a readiness body or an admin surface."""
        return self._breakers

    async def route(
        self,
        model: str,
        messages: Sequence[MessagePair],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        secrets: Sequence[Secret | str] = (),
    ) -> RoutedCompletion:
        """Serve `model`, or raise.

        `temperature` is a float HERE, at the provider boundary, and a decimal
        string on the wire. `api.RouteRequestBody.sampling_temperature()` makes
        that conversion, once, and this is the last signature before it reaches
        LiteLLM — which is why the float never appears in a contract, a log line
        or a span. The router's own callers in tests pass a float directly and
        are not going through the wire at all.

        Raises `RouteNotFound` for a model nothing routes, `AllCandidatesFailed`
        carrying the per-candidate chain when every candidate was tried, and
        `ValueError` for a malformed request — the last raised before any provider
        is called, so a bad request costs nothing.

        `secrets` are scrubbed out of every failure detail. The router is the
        boundary where provider text becomes an exception a caller and a log both
        see, and a provider that echoes the key it rejected would otherwise put a
        live credential into both.
        """
        route = self._routes.find(model)
        if route is None:
            # Raised before the span, and the model name is not recorded anywhere: it
            # is caller-supplied text, and `muse.telemetry` does not put
            # caller-supplied text on a span. An unknown model earns a status and an
            # error class (`invalid_request`), which is all it deserves — a route span
            # that claimed to have failed would drag a 404 into every error rate.
            raise RouteNotFound(model)
        # The messages are validated here, before the candidate loop, so a bad role
        # is a typed ValueError the endpoint turns into a 422 rather than a
        # provider's differently-worded 400 after a paid call.
        validated = tuple(Message(role=role, content=content) for role, content in messages)

        failures: list[CandidateFailure] = []
        attempts = 0
        with self._telemetry.span("muse.route", muse_route=route.model) as span:
            # `route.candidates` is already resolved: a `Route` holds each candidate's
            # vendor model, defaulted to the route's own at construction.
            for index, candidate in enumerate(route.candidates):
                outcome = await self._attempt(
                    candidate=candidate,
                    route=route,
                    messages=validated,
                    max_tokens=max_tokens,
                    temperature=temperature,
                    secrets=secrets,
                    index=index,
                )
                attempts += outcome.calls
                if outcome.completion is not None:
                    result = RoutedCompletion(
                        completion=outcome.completion,
                        route=route.model,
                        model=outcome.completion.model,
                        candidates_tried=len(failures) + 1,
                        attempts=attempts,
                    )
                    record(
                        span,
                        muse_model=result.model,
                        muse_provider=result.completion.provider,
                        muse_candidates_tried=result.candidates_tried,
                        muse_attempts=result.attempts,
                        muse_cost_micros=result.cost_micros,
                        muse_tokens_in=result.completion.tokens_in,
                        muse_tokens_out=result.completion.tokens_out,
                    )
                    return result
                # An outcome always carries exactly one of the two, so `failure` is not
                # None here. Asserted rather than guarded: `_attempt` builds both
                # branches itself, and a `None` would mean a new return path was added
                # without this loop being told — which would silently drop a failure from
                # the chain and make a fallback look like a single-candidate route.
                assert outcome.failure is not None, (
                    "an attempt produced neither a result nor a failure"
                )
                failures.append(outcome.failure)
        raise AllCandidatesFailed(route.model, tuple(failures))

    async def _attempt(
        self,
        *,
        candidate: Candidate,
        route: Route,
        messages: tuple[Message, ...],
        max_tokens: int | None,
        temperature: float | None,
        secrets: Sequence[Secret | str],
        index: int,
    ) -> _Attempt:
        """Run one candidate to exhaustion.

        The registry lookup is deliberately outside the retry: a name that resolves
        to nothing is a configuration error, not a provider that was briefly
        unavailable, and retrying it would just delay the same error.
        """
        provider = self._registry.get(candidate.provider)
        model = candidate.model or route.model
        breaker = self._breakers.for_provider(candidate.provider)

        # The breaker is consulted *before* the price lookup and before the provider is
        # touched at all, so an open breaker is a refusal that never becomes a request.
        # Recorded in the chain with `attempts: 0` because nothing was attempted — a
        # chain that said "1 attempt" here would be a lie an operator would chase.
        if not breaker.allow():
            with self._telemetry.span(
                "muse.provider.call",
                muse_provider=candidate.provider,
                muse_model=model,
                muse_candidate_index=index,
                muse_breaker_state=str(breaker.state),
            ) as span:
                # The 503 is what makes this span distinguishable in a trace viewer
                # from a call that was actually made and failed.
                record(span, {"http.response.status_code": 503})
                # `record_error`, not an inline attribute: it is what makes the class
                # and the span's failed status one act, and it is what puts the class
                # in core's vocabulary rather than this class's name.
                record_error(span, CircuitOpen)
            return _Attempt(
                failure=CandidateFailure(
                    provider=candidate.provider,
                    model=model,
                    error=CircuitOpen,
                    detail=(
                        f"{candidate.provider} is being held back after "
                        f"{breaker.threshold} consecutive failures"
                    ),
                    attempts=0,
                )
            )

        try:
            price = provider.cost_per_1k_tokens(model)
        except PriceUnavailable as error:
            # Skipped, not retried: the price table is not going to change during
            # this request, so a second lookup gets the same answer.
            return _Attempt(
                failure=CandidateFailure(
                    provider=candidate.provider,
                    model=model,
                    error=PriceUnavailable,
                    detail=_scrub(str(error), secrets),
                    attempts=0,
                )
            )

        request = CompletionRequest(
            model=model,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        policy = route.retry
        calls = 0
        started = self._clock()

        async def call() -> Completion:
            nonlocal calls
            calls += 1
            with self._telemetry.span(
                "muse.provider.call",
                muse_provider=candidate.provider,
                muse_model=model,
                muse_candidate_index=index,
                muse_retry_attempt=calls,
                muse_breaker_state=str(breaker.state),
            ) as span:
                began = self._clock()
                try:
                    completion = await provider.complete(request)
                except ProviderError as error:
                    # The fleet's class for this failure, never the message. A
                    # vendor's message is third-party text and a content-policy
                    # rejection quotes the offending content back, so recording it
                    # would put a prompt in a tracing backend — which is retained,
                    # searchable and readable by anyone with collector access.
                    # Re-raised rather than swallowed: the retry loop below owns the
                    # decision, this span only reports.
                    record(span, muse_latency_ms=int((self._clock() - began) * 1000))
                    record_error(span, error)
                    raise
                record(
                    span,
                    muse_tokens_in=completion.tokens_in,
                    muse_tokens_out=completion.tokens_out,
                    muse_cost_micros=completion.cost_micros,
                    muse_latency_ms=int((self._clock() - began) * 1000),
                )
            return Completion(
                provider=completion.provider,
                model=completion.model,
                content=completion.content,
                tokens_in=completion.tokens_in,
                tokens_out=completion.tokens_out,
                # The price resolved before dispatch, carried onto the result rather
                # than looked up again: a second lookup could read a different row.
                price=price,
                finish_reason=completion.finish_reason,
            )

        # One jitter draw per attempt, memoised. tenacity computes the wait *before*
        # consulting the stop condition, so both the budget check and the sleep ask
        # for the delay; if each drew its own the request could overshoot its own
        # deadline by the width of the jitter window — a bound that lies. It would
        # also consume the random stream twice per retry for no reason.
        drawn: dict[int, float] = {}

        def delay_for(attempt_number: int) -> float:
            if attempt_number not in drawn:
                drawn[attempt_number] = backoff_delay(
                    policy.backoff, attempt_number - 1, self._unit()
                )
            return drawn[attempt_number]

        def stop(retry_state) -> bool:
            """Attempts exhausted, or the budget cannot absorb another wait.

            A plain callable rather than tenacity's `stop_after_attempt` /
            `stop_after_delay` because both read the clock internally, and this
            module's whole testing approach is that the clock is the test's.

            The second condition is what makes this a *deadline* rather than a
            suggestion. Checking only "is the budget already spent" would let a 0.1s
            budget start a 0.25s wait, and the request would then finish 2.5x later
            than the ceiling it published — worse than having no budget, because it is
            a bound that lies. So a wait is only started when it lands *within* the
            budget; landing exactly on it is allowed, which is why this is `>`.

            Both checks are needed. `elapsed >= budget` alone lets a boundary attempt
            run with no time to spare; the sum alone would keep re-entering the loop
            against a clock that has already passed the budget.
            """
            if retry_state.attempt_number >= policy.max_attempts:
                return True
            elapsed = self._clock() - started
            if elapsed >= policy.budget_seconds:
                return True
            return elapsed + delay_for(retry_state.attempt_number) > policy.budget_seconds

        def wait(retry_state) -> float:
            return delay_for(retry_state.attempt_number)

        retrying = AsyncRetrying(
            stop=stop,
            wait=wait,
            retry=retry_if_exception(self._retryable(policy)),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    result = _Attempt(completion=await call(), calls=calls)
                    breaker.record_success()
                    return result
        except ProviderError as error:
            if trips_breaker(error):
                # One candidate failing is one strike, however many attempts it took:
                # counting attempts would open the breaker `threshold` times faster
                # for a route that happens to be configured with retries than for one
                # that is not.
                breaker.record_failure()
            return _Attempt(
                failure=CandidateFailure(
                    provider=candidate.provider,
                    model=model,
                    error=type(error),
                    detail=_scrub(str(error), secrets),
                    # `calls` is the truth: a script that ran out is a test bug, and
                    # a candidate that raised before any call did not happen.
                    attempts=calls,
                )
            )
        # Unreachable while `stop` has a positive attempt count, which the policy
        # validator guarantees. Stated rather than left implicit, because a silent
        # fallthrough here would return a success-shaped outcome with no completion
        # in it and the caller would dereference `None`.
        raise AssertionError(
            f"the retry loop for {candidate.provider}/{model} ended without a result"
        )

    async def stream(
        self,
        model: str,
        messages: Sequence[MessagePair],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        secrets: Sequence[Secret | str] = (),
    ) -> AsyncIterator[StreamResult]:
        """Serve `model` as a stream, or raise before the first chunk.

        **The fallback line is the first chunk, and it is structural rather than a
        check.** The chain advances only inside a `for` loop that is re-entered from
        the top; the first `yield` to the caller happens inside that loop and, once it
        has, the loop cannot run again — the generator is suspended at the yield and the
        `return` below it is only reachable by exhausting the candidate. So after the
        first chunk there is no code path from this generator to a second provider: the
        fallback is not forbidden by a flag, it is absent.

        The reason is splicing. Two providers answering one request produce two answers
        concatenated into one body — the caller sees a plausible completion that is half
        of each, with nothing marking the seam, a meter unable to attribute it and a
        dashboard unable to show it. Falling back *before* the first chunk is safe
        precisely because the caller holds no bytes yet, so the next candidate's answer
        is the whole answer rather than the second half of something.

        A failure that arrives after a chunk has been yielded cannot propagate out of
        the `for` loop as a candidate failure, because the `except` clauses sit around
        the *opening* of the attempt and not around the walk. `_StreamAttempt` reports
        it as a `SettledStream(error=...)` instead, and the chain returns. That is the
        whole mechanism, and it is why a mid-stream failure and a pre-first-chunk
        failure cannot be confused for one another.

        **One attempt per candidate; no retry loop.** Retrying a stream is only safe
        before a chunk exists, and the retry budget counts wall clock across an attempt,
        so a retry opened here could in principle have already yielded when the next
        attempt started. Rather than reason about that, the chain is the retry: a
        transient failure before the first chunk advances to the next candidate, which
        is the same repair against a provider that is not the one that just failed. An
        `Idempotency-Key` (PLAN §7, still not built) is what would make retrying one
        candidate safe, and `ProviderIndeterminate` — which may already have been
        billed — is not retried here either, for the same reason it is not retried on
        the non-streaming path without an explicit opt-in.

        `AsyncIterator[StreamResult]` rather than `AsyncIterator[StreamChunk]`: the
        settlement carries the completion and the route name that the meter needs, and
        returning them separately would mean a second channel for the caller to miss.
        """
        route = self._routes.find(model)
        if route is None:
            raise RouteNotFound(model)
        # Validated here, before the candidate loop, for the same reason and with the
        # same consequence as `route`: a bad role is a ValueError the endpoint turns
        # into a 422, rather than a provider's differently-worded 400 after a paid call.
        validated = tuple(Message(role=role, content=content) for role, content in messages)

        failures: list[CandidateFailure] = []
        # Detached for the same reason as the provider span in `_StreamAttempt.chunks`:
        # this block spans a yield to the caller, and a streaming body is resumed from a
        # different task than it was started in.
        with self._telemetry.detached_span("muse.route", muse_route=route.model) as span:
            for index, candidate in enumerate(route.candidates):
                # The breaker is consulted *before* the provider is touched, so an open
                # breaker is a refusal that never becomes a request.
                breaker = self._breakers.for_provider(candidate.provider)
                if not breaker.allow():
                    with self._telemetry.span(
                        "muse.provider.call",
                        muse_provider=candidate.provider,
                        muse_model=candidate.model or route.model,
                        muse_candidate_index=index,
                        muse_breaker_state=str(breaker.state),
                    ) as held:
                        record(held, {"http.response.status_code": 503})
                        record_error(held, CircuitOpen)
                    failures.append(
                        CandidateFailure(
                            provider=candidate.provider,
                            model=candidate.model or route.model,
                            error=CircuitOpen,
                            detail=(
                                f"{candidate.provider} is being held back after "
                                f"{breaker.threshold} consecutive failures"
                            ),
                            attempts=0,
                        )
                    )
                    continue

                provider = self._registry.get(candidate.provider)
                vendor_model = candidate.model or route.model
                try:
                    price = provider.cost_per_1k_tokens(vendor_model)
                except PriceUnavailable as error:
                    # Skipped, not retried, for the same reason as the non-streaming
                    # path: the price table is not going to change during this request.
                    failures.append(
                        CandidateFailure(
                            provider=candidate.provider,
                            model=vendor_model,
                            error=PriceUnavailable,
                            detail=_scrub(str(error), secrets),
                            attempts=0,
                        )
                    )
                    continue

                attempt = _StreamAttempt(
                    self,
                    provider,
                    candidate,
                    vendor_model,
                    breaker,
                    CompletionRequest(
                        model=vendor_model,
                        messages=validated,
                        max_tokens=max_tokens,
                        temperature=temperature,
                    ),
                    price,
                    secrets,
                )
                try:
                    # Only the *opening* is guarded. Past the first chunk the chain
                    # cannot advance, so there is nothing here to fall back to.
                    #
                    # `StopAsyncIteration` has no clause here and must not get one. A
                    # provider whose stream yields nothing at all produces that
                    # exception, but it cannot cross `_StreamAttempt.chunks` — an async
                    # generator rewrites it to `RuntimeError`, which nothing here would
                    # recognise — so the attempt translates it to `ProviderUnavailable`
                    # itself and it arrives as an ordinary candidate failure below. A
                    # clause for it would be a branch no request could ever reach.
                    results = attempt.chunks(index)
                    first = await anext(results)
                except ProviderError as error:
                    # Raised by the attempt before its first yield: no chunk has
                    # reached the caller, so the chain may still advance. This is the
                    # ONLY branch in which it may.
                    if trips_breaker(error):
                        breaker.record_failure()
                    failures.append(
                        CandidateFailure(
                            provider=candidate.provider,
                            model=vendor_model,
                            error=type(error),
                            detail=_scrub(str(error), secrets),
                            attempts=1,
                        )
                    )
                    continue

                record(
                    span,
                    muse_model=vendor_model,
                    muse_provider=provider.name,
                    muse_candidates_tried=len(failures) + 1,
                    muse_breaker_state=str(breaker.state),
                )
                yield first
                # Past this point the caller holds bytes, so the only remaining work is
                # to hand over what this candidate produced. Everything after it — a
                # failure included — arrives as a `SettledStream`.
                async for result in results:
                    yield result
                return
        raise AllCandidatesFailed(route.model, tuple(failures))

    def _retryable(self, policy: RetryPolicy) -> Callable[[BaseException], bool]:
        """The retry predicate for one route's policy.

        A closure rather than the bare `is_retryable` so `retry_indeterminate` is
        honoured per route. It is not a "retry more" switch: the same tuple decides
        everything else either way, so opting in unlocks the ambiguous read timeout
        and nothing more.
        """

        def retryable(error: BaseException) -> bool:
            if type(error) is ProviderIndeterminate:
                return policy.retry_indeterminate
            return is_retryable(error)

        return retryable

    def __repr__(self) -> str:
        return f"{type(self).__name__}(routes={list(self._routes.models())})"


def _scrub(detail: str, secrets: Sequence[Secret | str]) -> str:
    """Remove known credentials from a provider-supplied detail string.

    The empty case returns the text unchanged rather than calling `redact` with
    nothing, so a request that holds no credentials does not pay for a copy of every
    provider message it produces.
    """
    if not secrets:
        return detail
    return redact(detail, *secrets)
