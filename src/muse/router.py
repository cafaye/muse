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

The backoff uses tenacity's real `wait_exponential` with an injected `sleep`, so
the suite exercises the real backoff arithmetic without waiting on it. Tests
assert on the durations the policy asked for, which is what makes "the backoff
grows exponentially" a fact rather than a hope.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass

from tenacity import AsyncRetrying, retry_if_exception, stop_after_attempt, wait_exponential

from muse.errors import (
    AllCandidatesFailed,
    CandidateFailure,
    PriceUnavailable,
    ProviderError,
    RouteNotFound,
    is_retryable,
)
from muse.providers import (
    Completion,
    CompletionRequest,
    Message,
    ProviderRegistry,
)
from muse.redaction import Secret, redact
from muse.routes import Candidate, Route, RouteTable

#: A `(role, content)` pair as a caller states it. The router turns these into
#: `Message` values, so the endpoint and the router agree on one request shape
#: rather than each having its own.
MessagePair = tuple[str, str]

#: What the router is handed instead of a real sleep. A coroutine function, because
#: tenacity's async path awaits the result.
SleepFn = Callable[[float], Awaitable[None]]


async def _asyncio_sleep(seconds: float) -> None:
    """The production sleep.

    Named and separate so a test can assert the default is a real one. A no-op
    default would make the backoff look instant in development and in the suite, and
    the bug it hides — a policy that never actually waits — is invisible in both.
    """
    await asyncio.sleep(seconds)


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
    """

    def __init__(
        self,
        registry: ProviderRegistry,
        routes: RouteTable,
        *,
        sleep: SleepFn = _asyncio_sleep,
    ) -> None:
        self._registry = registry
        self._routes = routes
        self._sleep = sleep

    @property
    def routes(self) -> RouteTable:
        """The routing table, for a readiness body or a future admin surface."""
        return self._routes

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
            raise RouteNotFound(model)
        # The messages are validated here, before the candidate loop, so a bad role
        # is a typed ValueError the endpoint turns into a 422 rather than a
        # provider's differently-worded 400 after a paid call.
        validated = tuple(Message(role=role, content=content) for role, content in messages)

        failures: list[CandidateFailure] = []
        attempts = 0
        # `route.candidates` is already resolved: a `Route` holds each candidate's
        # vendor model, defaulted to the route's own at construction.
        for candidate in route.candidates:
            outcome = await self._attempt(
                candidate=candidate,
                route=route,
                messages=validated,
                max_tokens=max_tokens,
                temperature=temperature,
                secrets=secrets,
            )
            attempts += outcome.calls
            if outcome.completion is not None:
                return RoutedCompletion(
                    completion=outcome.completion,
                    route=route.model,
                    model=outcome.completion.model,
                    candidates_tried=len(failures) + 1,
                    attempts=attempts,
                )
            # An outcome always carries exactly one of the two, so `failure` is not
            # None here. Asserted rather than guarded: `_attempt` builds both
            # branches itself, and a `None` would mean a new return path was added
            # without this loop being told — which would silently drop a failure from
            # the chain and make a fallback look like a single-candidate route.
            assert outcome.failure is not None, "an attempt produced neither a result nor a failure"
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
    ) -> _Attempt:
        """Run one candidate to exhaustion.

        The registry lookup is deliberately outside the retry: a name that resolves
        to nothing is a configuration error, not a provider that was briefly
        unavailable, and retrying it would just delay the same error.
        """
        provider = self._registry.get(candidate.provider)
        model = candidate.model or route.model
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
        calls = 0

        async def call() -> Completion:
            nonlocal calls
            calls += 1
            completion = await provider.complete(request)
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

        retrying = AsyncRetrying(
            stop=stop_after_attempt(route.retry.max_attempts),
            wait=wait_exponential(
                multiplier=route.retry.backoff.initial, max=route.retry.backoff.maximum
            ),
            retry=retry_if_exception(is_retryable),
            sleep=self._sleep,
            reraise=True,
        )
        try:
            async for attempt in retrying:
                with attempt:
                    return _Attempt(completion=await call(), calls=calls)
        except ProviderError as error:
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
        # Unreachable while `stop_after_attempt` has a positive count, which the
        # policy validator guarantees. Stated rather than left implicit, because a
        # silent fallthrough here would return a success-shaped outcome with no
        # completion in it and the caller would dereference `None`.
        raise AssertionError(
            f"the retry loop for {candidate.provider}/{model} ended without a result"
        )

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
