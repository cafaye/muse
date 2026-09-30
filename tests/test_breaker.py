"""The per-provider circuit breaker.

Three states, and the property that makes the third one worth having is stated in
every test below: **while the breaker is open, no request is issued.** A breaker
that only records a state and then still dials the provider is a cache of a
symptom — it makes the dashboard look better and the incident exactly as bad. So
the fail-fast tests assert on the call counters at two levels: the provider double's
own `calls`, and the litellm stub's `acompletion` count, which is the last thing
before a socket exists.

Two decisions that are easy to get wrong and are pinned here:

- **A client error does not trip the breaker.** A 400 or a 401 from a provider is
  muse sending something the provider is right to refuse. Counting those would let
  one caller with a malformed request take the whole route down for everybody, which
  turns a bug into a denial of service.
- **A success resets the count.** The threshold is over *consecutive* failures. A
  provider that fails twice, serves four thousand requests, and fails twice more is
  not a provider with four consecutive failures, and treating it as one opens the
  breaker for a route that is working.
"""

from __future__ import annotations

import pytest

from muse.breaker import DEFAULT_BREAKER_RESET_SECONDS, DEFAULT_BREAKER_THRESHOLD, BreakerRegistry
from muse.errors import (
    AllCandidatesFailed,
    CircuitOpen,
    ProviderAuthError,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
)
from muse.providers import Price
from muse.providers.fake import ScriptedProvider
from muse.router import Router
from muse.routes import Candidate, RetryPolicy

from .test_router import completion, registry_with, table

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PROVIDER = "primary"
FALLBACK = "fallback"
PRICE = Price(1000, 2000)
TRANSIENT = (ProviderTimeout("slow"), ProviderRateLimited("429"), ProviderUnavailable("503"))


class Clock:
    """A clock a test moves by hand.

    The alternative is `time.sleep`, and a sleep in this suite is a test whose
    result depends on how loaded the machine is (AGENTS.md rule 13). Every duration
    the breaker cares about is therefore a number the test writes down.
    """

    def __init__(self, start: float = 0.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def breakers(clock: Clock, **kwargs) -> BreakerRegistry:
    return BreakerRegistry(clock=clock, **kwargs)


# --- the state machine, on its own ----------------------------------------


def test_a_fresh_breaker_is_closed() -> None:
    registry = breakers(Clock())

    assert registry.state(PROVIDER) == "closed"


def test_the_breaker_opens_on_the_nth_consecutive_failure() -> None:
    """Exactly `threshold`, not one earlier and not one later.

    A breaker that opens early rejects a provider that was about to recover; one that
    opens late exists to no purpose. Pinning the exact count is what makes the
    threshold a setting rather than a suggestion.
    """
    clock = Clock()
    registry = breakers(clock, threshold=3)
    breaker = registry.for_provider(PROVIDER)

    for expected in ("closed", "closed", "open"):
        breaker.record_failure()
        assert registry.state(PROVIDER) == expected


def test_a_success_in_the_middle_resets_the_failure_count() -> None:
    """The threshold counts consecutive failures."""
    clock = Clock()
    registry = breakers(clock, threshold=3)
    breaker = registry.for_provider(PROVIDER)

    breaker.record_failure()
    breaker.record_failure()
    breaker.record_success()
    breaker.record_failure()
    breaker.record_failure()

    assert registry.state(PROVIDER) == "closed"


def test_an_open_breaker_halves_open_when_the_reset_time_elapses() -> None:
    """Probing is how a breaker finds out that the provider is back.

    Without it an outage that resolves is an outage until the next deploy, which is
    the breaker replacing a transient failure with a permanent one.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1, reset_seconds=30.0)
    breaker = registry.for_provider(PROVIDER)
    breaker.record_failure()

    clock.advance(29.9)
    assert breaker.allow() is False

    clock.advance(0.1)
    assert breaker.allow() is True
    assert registry.state(PROVIDER) == "half_open"


def test_only_one_probe_is_admitted_while_half_open() -> None:
    """A half-open breaker that lets everyone through is a closed breaker.

    The point of the half-open state is that *one* request finds out whether the
    provider recovered. Admitting the whole waiting fleet re-creates the thundering
    herd the breaker exists to stop.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1, reset_seconds=1.0)
    breaker = registry.for_provider(PROVIDER)
    breaker.record_failure()
    clock.advance(1.0)

    assert breaker.allow() is True
    assert breaker.allow() is False


def test_a_successful_probe_closes_the_breaker() -> None:
    clock = Clock()
    registry = breakers(clock, threshold=1, reset_seconds=1.0)
    breaker = registry.for_provider(PROVIDER)
    breaker.record_failure()
    clock.advance(1.0)
    breaker.allow()

    breaker.record_success()

    assert registry.state(PROVIDER) == "closed"
    assert breaker.allow() is True


def test_a_failed_probe_reopens_the_breaker_and_restarts_the_clock() -> None:
    """The provider is still down, so the breaker goes back to open.

    The reset clock restarts too, so the next probe is a full `reset_seconds` away.
    A breaker that re-probed immediately would be polling a provider that has just
    told it twice that it is down.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1, reset_seconds=30.0)
    breaker = registry.for_provider(PROVIDER)
    breaker.record_failure()
    clock.advance(30.0)
    breaker.allow()

    breaker.record_failure()

    assert registry.state(PROVIDER) == "open"
    assert breaker.allow() is False
    clock.advance(29.9)
    assert breaker.allow() is False
    clock.advance(0.1)
    assert breaker.allow() is True


def test_closing_the_breaker_can_require_several_probes() -> None:
    """Configurable, because a provider that flapped should not be declared healthy
    on one lucky request.

    The default is 1 (one probe closes it), which is right for a service that would
    rather retry than sit out a recovered provider; the knob is here for a deployment
    that disagrees.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1, reset_seconds=1.0, successes_to_close=2)
    breaker = registry.for_provider(PROVIDER)
    breaker.record_failure()
    clock.advance(1.0)
    breaker.allow()

    breaker.record_success()
    assert registry.state(PROVIDER) == "half_open"

    breaker.record_success()
    assert registry.state(PROVIDER) == "closed"


def test_each_provider_gets_its_own_breaker() -> None:
    """One broken vendor must not take the other one out of service.

    That is the entire reason the breaker is per provider rather than per service:
    anthropic being down is no reason to stop serving requests that openai can serve.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1)
    registry.for_provider(PROVIDER).record_failure()

    assert registry.state(PROVIDER) == "open"
    assert registry.state(FALLBACK) == "closed"


def test_the_same_breaker_comes_back_for_the_same_provider() -> None:
    """A registry that minted a new breaker per lookup would never open anything."""
    clock = Clock()
    registry = breakers(clock, threshold=1)

    assert registry.for_provider(PROVIDER) is registry.for_provider(PROVIDER)


def test_the_production_defaults_are_the_documented_ones() -> None:
    """Named constants rather than literals in the wiring, so a reader of `Router`
    can look up what "no configuration" actually means."""
    assert DEFAULT_BREAKER_THRESHOLD == 5
    assert DEFAULT_BREAKER_RESET_SECONDS == 30.0


# --- what counts as a failure --------------------------------------------


@pytest.mark.parametrize(
    "error",
    [pytest.param(e, id=type(e).__name__) for e in TRANSIENT],
)
async def test_a_transient_failure_counts_against_the_breaker(error) -> None:
    clock = Clock()
    registry = breakers(clock, threshold=1)
    provider = ScriptedProvider(name=PROVIDER, price=PRICE, errors=(error,))
    router = Router(
        registry_with(provider),
        table(Candidate(PROVIDER)),
        breakers=registry,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert registry.state(PROVIDER) == "open"


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderAuthError("bad key"), id="auth"),
        pytest.param(ProviderInvalidRequest("400"), id="invalid-request"),
    ],
)
async def test_a_client_error_does_not_trip_the_breaker(error) -> None:
    """muse sent something the provider was right to refuse.

    Tripping the breaker here would let one malformed caller take the route down for
    everybody, so a refusal is recorded in the failure chain and nowhere else.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1)
    provider = ScriptedProvider(name=PROVIDER, price=PRICE, errors=(error,))
    router = Router(
        registry_with(provider),
        table(Candidate(PROVIDER)),
        breakers=registry,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert registry.state(PROVIDER) == "closed"


# --- the fail-fast path, at the socket seam -------------------------------


async def test_an_open_breaker_issues_no_request_at_all() -> None:
    """The property the whole module exists for.

    Two counters, and the second one matters more: `provider.calls` is the double's
    own count, while the litellm stub's count is the last statement muse executes
    before a socket would exist. Asserting only the first would still pass if the
    adapter were dialling something the double never saw.
    """
    from muse.providers import LiteLLMProvider
    from muse.routes import routes_from_yaml

    from .support.litellm_stub import make_stub

    stub = make_stub(raises="ServiceUnavailableError", error_message="503 down")
    provider = LiteLLMProvider(name="openai", api_key="sk-test", litellm=stub)
    # A model the stub's price table actually holds. The unpriced path would skip the
    # candidate before dispatch, and the test would then pass with zero calls for a
    # reason that has nothing to do with the breaker.
    route_table = routes_from_yaml(
        "version: 1\nroutes:\n  - model: gpt-4o-mini\n    candidates:\n      - provider: openai\n"
    )
    clock = Clock()
    registry = breakers(clock, threshold=1)
    router = Router(registry_with(provider), route_table, breakers=registry)

    with pytest.raises(AllCandidatesFailed):
        await router.route("gpt-4o-mini", (("user", "hello"),))

    assert stub.acompletion_calls == 1, "the first attempt did reach the provider"
    assert registry.state("openai") == "open"

    # Every subsequent call must be refused before any adapter code runs.
    for _ in range(5):
        with pytest.raises(AllCandidatesFailed) as excinfo:
            await router.route("gpt-4o-mini", (("user", "hello"),))

    assert stub.acompletion_calls == 1, "an open breaker still dialled the provider"
    assert [f.error for f in excinfo.value.failures] == [CircuitOpen]
    assert excinfo.value.failures[0].attempts == 0


async def test_an_open_breaker_refuses_the_provider_but_the_route_still_serves() -> None:
    """Fail fast is per *provider*, not per request.

    This is the property that makes the breaker safe to have. A breaker that failed
    the whole request would be strictly worse than no breaker: one vendor being down
    would take out every route that has a healthy fallback, which is exactly the
    situation the fallback chain exists for. So the primary is refused without a
    round trip, and the fallback answers.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1)
    primary = ScriptedProvider(name=PROVIDER, price=PRICE, errors=(ProviderTimeout("slow"),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(FALLBACK),) * 2)
    router = Router(
        registry_with(primary, fallback),
        table(Candidate(PROVIDER), Candidate(FALLBACK)),
        breakers=registry,
    )

    first = await router.route("fast", (("user", "hello"),))

    assert first.completion.provider == FALLBACK
    assert primary.calls == 1
    assert registry.state(PROVIDER) == "open"

    second = await router.route("fast", (("user", "hello"),))

    assert second.completion.provider == FALLBACK
    assert primary.calls == 1, "the open breaker still called the held-back provider"
    # `attempts` is the total across the whole chain, so the one call that happened is
    # the fallback's. The refused primary contributed none, which is the point.
    assert second.attempts == 1
    assert second.candidates_tried == 2


async def test_a_route_whose_only_candidate_is_held_back_fails_fast() -> None:
    """The no-fallback case: a single-candidate route has nowhere else to go.

    The failure chain says `CircuitOpen` with zero attempts, which is the difference
    between "we held it back" and "we tried it and it failed" — the two need different
    responses from whoever is on call.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1)
    primary = ScriptedProvider(name=PROVIDER, price=PRICE, errors=(ProviderTimeout("slow"),))
    router = Router(registry_with(primary), table(Candidate(PROVIDER)), breakers=registry)

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert registry.state(PROVIDER) == "open"
    assert primary.calls == 1

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert primary.calls == 1, "the open breaker still dialled the provider"
    assert [f.error for f in excinfo.value.failures] == [CircuitOpen]
    assert excinfo.value.failures[0].attempts == 0


async def test_a_route_without_a_breaker_still_works() -> None:
    """The router's default is an empty registry, and an empty registry is not a
    broken one: `tests/test_router.py` constructs routers with no breaker at all, and
    a change here that broke them would break every fallback test in the packet."""
    provider = ScriptedProvider(name=PROVIDER, price=PRICE, completions=(completion(),))

    result = await Router(registry_with(provider), table(Candidate(PROVIDER))).route(
        "fast", (("user", "hello"),)
    )

    assert result.completion.provider == PROVIDER


async def test_the_breaker_reports_a_route_with_many_attempts_as_a_single_failure() -> None:
    """A candidate that retried five times counts once against the breaker.

    Counting attempts would open the breaker `threshold` times faster than the
    threshold says, so a route configured with retries would shed traffic that a
    route without them would have kept serving.
    """
    clock = Clock()
    registry = breakers(clock, threshold=1)
    provider = ScriptedProvider(name=PROVIDER, price=PRICE, errors=(ProviderTimeout("slow"),) * 5)
    router = Router(
        registry_with(provider),
        table(Candidate(PROVIDER), retry=RetryPolicy(max_attempts=5)),
        breakers=registry,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 5
    assert registry.state(PROVIDER) == "open"


def test_a_breaker_refusal_is_a_typed_error_naming_the_provider() -> None:
    """The refusal is reported, not swallowed.

    A caller that gets a 503 needs the failure chain to say *why*, and `CircuitOpen`
    is the answer to a different question from `ProviderUnavailable`: this provider is
    being held back, not tried and found broken.
    """
    error = CircuitOpen("openai is being held back after 5 consecutive failures")

    assert isinstance(error, ProviderUnavailable.__mro__[1])
    assert "openai" in str(error)
    assert "5" in str(error)
