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
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception

from muse.breaker import BreakerRegistry
from muse.errors import (
    AllCandidatesFailed,
    CandidateFailure,
    CircuitOpen,
    PriceUnavailable,
    ProviderError,
    ProviderIndeterminate,
    RouteNotFound,
    is_retryable,
    trips_breaker,
)
from muse.providers import (
    Completion,
    CompletionRequest,
    Message,
    ProviderRegistry,
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
