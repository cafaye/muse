"""The retry budget: what is retried, how long we wait, and when we stop.

Three separate questions, and the tests are grouped by them because they fail for
three separate reasons:

- **Which errors are retried.** The safe set is connection failures, a 408, a 429
  and a 5xx. Everything else — a rejected credential, a malformed request, a content
  policy refusal — advances to the next candidate immediately, because the identical
  retry arrives at the identical answer one backoff later.
- **What it waits.** Exponential, capped, and **jittered**. The jitter is not
  decoration: without it every muse process that failed at the same instant retries
  at the same instant, forever, and a provider recovering from an outage is hit by a
  synchronised wave rather than a trickle. The schedule is asserted as a list of
  numbers against an injected sleep, so nothing waits on a real clock.
- **When it stops.** Both a count and a wall-clock deadline. A count alone does not
  bound time: `max_attempts: 6` with a 4-second ceiling is 20 seconds of backoff
  plus however long each attempt takes, and "however long" is unbounded unless the
  provider hangs. The budget is what makes "a request must not hang past a deadline"
  a fact about the code rather than a hope about the network.

The double-billing case has its own section. A read timeout is a request the vendor
may already have completed and billed, so it is **not** in the safe set — see
`ProviderIndeterminate`.
"""

from __future__ import annotations

import pytest

from muse.errors import (
    AllCandidatesFailed,
    CircuitOpen,
    ContentPolicyError,
    CredentialUnavailable,
    PriceUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderIndeterminate,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
    is_retryable,
    is_retryable_status,
)
from muse.providers import Price
from muse.providers.fake import ScriptedProvider
from muse.router import Router, backoff_delay
from muse.routes import BackoffPolicy, Candidate, RetryPolicy

from .test_router import PRIMARY, completion, registry_with, table

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PRICE = Price(1000, 2000)


class Timeline:
    """A clock and a sleep that share one timeline.

    The sleep records the duration it was asked for *and advances the clock by it*.
    That is what makes a deadline assertable: the budget test is a statement about
    which attempt the schedule would stop on, not a measurement of how long the
    machine took. No test in this file sleeps (AGENTS.md rule 13).
    """

    def __init__(self) -> None:
        self.now = 0.0
        self.waited: list[float] = []

    def clock(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.waited.append(seconds)
        self.now += seconds


def router_for(
    provider: ScriptedProvider,
    *,
    timeline: Timeline,
    unit: float = 1.0,
    **policy,
) -> Router:
    return Router(
        registry_with(provider),
        table(Candidate(provider.name), **policy),
        sleep=timeline.sleep,
        clock=timeline.clock,
        unit=lambda: unit,
    )


# --- which errors are retried ---------------------------------------------


@pytest.mark.parametrize(
    ("error", "retried"),
    [
        # The safe set: the vendor refused before doing any work, or failed at the
        # transport layer. Re-sending cannot bill twice because nothing was billed.
        pytest.param(ProviderTimeout("408 request timeout"), True, id="408"),
        pytest.param(ProviderRateLimited("429 too many requests"), True, id="429"),
        pytest.param(ProviderUnavailable("503 overloaded"), True, id="503"),
        pytest.param(ProviderUnavailable("500 internal error"), True, id="500"),
        pytest.param(ProviderUnavailable("connection reset"), True, id="connection-error"),
        # The unsafe set: the request was accepted and its outcome is unknown.
        pytest.param(ProviderIndeterminate("read timeout"), False, id="read-timeout"),
        # The permanent set: the identical retry gets the identical answer.
        pytest.param(ProviderAuthError("401 bad key"), False, id="401"),
        pytest.param(ProviderInvalidRequest("400 malformed"), False, id="400"),
        pytest.param(ProviderInvalidRequest("403 forbidden"), False, id="403"),
        pytest.param(ProviderInvalidRequest("404 unknown model"), False, id="404"),
        pytest.param(ProviderInvalidRequest("413 too long"), False, id="413"),
        pytest.param(ContentPolicyError("refused"), False, id="content-policy"),
        pytest.param(CredentialUnavailable("no key"), False, id="no-credential"),
        pytest.param(ResponseShapeError("no choices"), False, id="response-shape"),
        pytest.param(PriceUnavailable("no price"), False, id="no-price"),
    ],
)
def test_the_retryable_set_is_exactly_the_safe_one(error: ProviderError, retried: bool) -> None:
    assert is_retryable(error) is retried


def _every_provider_error_subclass() -> set[type[ProviderError]]:
    """Every subclass of `ProviderError`, at any depth.

    Walked recursively rather than with `__subclasses__()`, which returns *direct*
    subclasses only. `CircuitOpen` is a `ProviderUnavailable`, so a flat walk misses
    it entirely — and a test that cannot see a subclass cannot assert anything about
    it, which is precisely the gap AGENTS.md rule 12 exists to close.
    """
    found: set[type[ProviderError]] = set()
    pending = list(ProviderError.__subclasses__())
    while pending:
        subclass = pending.pop()
        if subclass in found:
            continue
        found.add(subclass)
        pending.extend(subclass.__subclasses__())
    return found


def test_every_provider_error_subclass_has_a_decided_verdict() -> None:
    """AGENTS.md rule 12: the tuple is explicit so that adding a subclass does not
    silently change retry behaviour. That only holds if *every* subclass has been
    decided — so this asserts the real verdicts rather than trusting that nobody has
    added one since the tuple was written.

    `CircuitOpen` is the interesting entry. It subclasses `ProviderUnavailable`, so an
    `isinstance` check would retry a held-back provider — the one case where a retry
    is least useful, since the router already knows the answer. `is_retryable` tests
    the exact type instead, and this pins that.
    """
    assert {
        subclass: is_retryable(subclass("probe")) for subclass in _every_provider_error_subclass()
    } == {
        ProviderTimeout: True,
        ProviderRateLimited: True,
        ProviderUnavailable: True,
        CircuitOpen: False,
        ProviderIndeterminate: False,
        ProviderAuthError: False,
        ProviderInvalidRequest: False,
        ContentPolicyError: False,
        PriceUnavailable: False,
        CredentialUnavailable: False,
        ResponseShapeError: False,
    }


@pytest.mark.parametrize("status", [408, 429, 500, 502, 503, 504])
def test_the_transient_status_codes_are_the_retryable_ones(status: int) -> None:
    assert is_retryable_status(status) is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 409, 413, 422, 451])
def test_no_other_4xx_is_retryable(status: int) -> None:
    """The rule the packet states: of the 4xx family, only 408 and 429.

    408 and 429 are the two that say "I did not do the work" — the first because
    the request never arrived complete, the second because the vendor is telling you
    to come back. Every other 4xx is an answer.
    """
    assert is_retryable_status(status) is False


def test_an_unknown_status_is_not_assumed_transient() -> None:
    """None (the SDK gave no status) and 599 (a status muse has never heard of) are
    both treated as unproven.

    Guessing "probably transient" for an unrecognised failure is how a malformed
    request ends up in a retry loop, and how a new vendor's 4xx gets three chances to
    bill the same request.
    """
    assert is_retryable_status(None) is False
    assert is_retryable_status(599) is False
    assert is_retryable_status(200) is False


# --- the backoff schedule --------------------------------------------------


def test_the_schedule_doubles_then_holds_at_the_ceiling() -> None:
    """The ceiling is what stops a down provider from hanging the request forever."""
    policy = BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0)

    assert [backoff_delay(policy, index, 1.0) for index in range(6)] == [
        0.25,
        0.5,
        1.0,
        2.0,
        2.0,
        2.0,
    ]


def test_each_delay_is_no_smaller_than_the_previous_one() -> None:
    """Monotonic growth, at the *floor* of the jitter window.

    Asserted at the floor because that is where a bad jitter could break it: an
    implementation that subtracted a random amount, or one that used full jitter
    (`delay = random * exponential`, floor 0), would let a later delay undercut an
    earlier one and turn the backoff into a hot loop against a provider that is
    already overloaded.
    """
    policy = BackoffPolicy(initial=0.1, maximum=8.0, jitter=1.0)

    schedule = [backoff_delay(policy, index, 0.0) for index in range(8)]

    assert schedule == sorted(schedule)
    assert max(schedule) <= 8.0


def test_jitter_stays_inside_its_window_and_never_exceeds_the_ceiling() -> None:
    """Every draw lands in `[ceiling * (1 - jitter), ceiling]`.

    The upper bound being exactly the ceiling is the important half: jitter widens
    the *distribution*, it must not push a delay past the cap, or the cap stops
    being a cap.
    """
    policy = BackoffPolicy(initial=1.0, maximum=4.0, jitter=0.5)

    for index in range(6):
        ceiling = min(4.0, 1.0 * 2**index)
        for unit in (0.0, 0.25, 0.5, 0.75, 1.0):
            delay = backoff_delay(policy, index, unit)
            assert ceiling * 0.5 <= delay <= ceiling


def test_jitter_spreads_a_real_distribution_rather_than_a_fixed_delay() -> None:
    """The production jitter source is random, and the suite checks it really varies.

    A "jitter" that returned a constant would satisfy every schedule assertion above
    and still be a thundering herd, so this is the one test that touches the real
    source. It is seeded, so it is deterministic: 200 draws from a fixed seed cannot
    flake.
    """
    import random

    policy = BackoffPolicy(initial=1.0, maximum=10.0, jitter=0.5)
    source = random.Random(1234)

    draws = {backoff_delay(policy, 3, source.random()) for _ in range(200)}

    assert len(draws) > 100
    assert all(4.0 <= draw <= 8.0 for draw in draws)


def test_zero_jitter_is_the_plain_exponential() -> None:
    """`jitter: 0` means the pre-jitter schedule exactly, and it is still supported.

    A deployment that wants a reproducible schedule (a test, or an incident being
    reproduced on purpose) sets the knob to zero rather than patching the policy.
    """
    policy = BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0)

    assert [backoff_delay(policy, i, unit) for i in range(4) for unit in (0.0, 1.0)] == [
        0.25,
        0.25,
        0.5,
        0.5,
        1.0,
        1.0,
        2.0,
        2.0,
    ]


def test_full_jitter_can_draw_a_zero_delay() -> None:
    """`jitter: 1` is full jitter, whose floor is zero.

    Allowed on purpose and pinned so the trade-off is a decision rather than an
    accident: full jitter spreads callers best, at the cost of occasionally retrying
    immediately. It is not the default because a zero delay before the last attempt
    is a burst, not a backoff.
    """
    policy = BackoffPolicy(initial=1.0, maximum=8.0, jitter=1.0)

    assert backoff_delay(policy, 2, 0.0) == 0.0


async def test_the_router_sleeps_the_schedule_it_computed() -> None:
    """The schedule function and the loop that uses it are asserted separately.

    Asserting only the function would leave the loop free to ignore it; asserting
    only the loop would leave the function free to be wrong. Both, against the same
    numbers.
    """
    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 6)
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(
            max_attempts=5,
            backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
            budget_seconds=600.0,
        ),
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert timeline.waited == [0.25, 0.5, 1.0, 2.0]
    assert provider.calls == 5


async def test_the_jitter_source_is_drawn_once_per_attempt() -> None:
    """The deadline is checked against the same number that is actually slept.

    tenacity computes the wait *before* it consults the stop condition, so each failed
    attempt asks for its delay once and the stop check reuses it. If those were two
    independent draws the budget would be validated against one number and the
    request would sleep another — overshooting its own deadline by up to the width of
    the jitter window, which is a bound that lies.

    The count is `provider.calls`, not `len(waited)`: the last attempt's delay is
    drawn and then discarded by the stop, because the stop is what tenacity asks
    *after* computing it. That is a tenacity ordering, not a muse one, and it is
    asserted rather than assumed.
    """
    draws: list[float] = []
    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 8)
    router = Router(
        registry_with(provider),
        table(
            Candidate(PRIMARY),
            retry=RetryPolicy(
                max_attempts=5,
                backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.5),
                budget_seconds=600.0,
            ),
        ),
        sleep=timeline.sleep,
        clock=timeline.clock,
        unit=lambda: draws.append(0.5) or 0.5,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 5
    assert len(draws) == 5, "the jitter source was consumed more than once per attempt"
    # unit=0.5 with jitter=0.5 sits three quarters up the window, so the schedule is
    # 0.75 of the exponential: 0.1875, 0.375, 0.75, 1.5.
    assert timeline.waited == [0.1875, 0.375, 0.75, 1.5]


async def test_a_jittered_never_sleeps_past_the_budget() -> None:
    """The property the memoisation exists to protect, asserted end to end.

    With the jitter source returning a *different* number every time, an
    implementation that drew independently for the budget check and for the sleep
    would still produce a plausible-looking schedule — and would still be able to
    overshoot. So the assertion is the one that matters: the total time spent waiting
    never exceeds the budget, whatever the source returned.
    """
    import random

    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 30)
    router = Router(
        registry_with(provider),
        table(
            Candidate(PRIMARY),
            retry=RetryPolicy(
                max_attempts=20,
                backoff=BackoffPolicy(initial=0.25, maximum=4.0, jitter=1.0),
                budget_seconds=1.0,
            ),
        ),
        sleep=timeline.sleep,
        clock=timeline.clock,
        unit=random.Random(7).random,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert timeline.now <= 1.0, "a jittered wait overshot the budget it was checked against"
    assert provider.calls < 20, "the attempt cap, not the budget, ended the loop"


async def test_the_router_actually_jitters_by_default() -> None:
    """The default policy randomises, and a run produces more than one delay.

    A seeded source keeps this deterministic: the schedule is not compared to fixed
    numbers, only checked for variation and for the ceiling it may not cross.
    """
    import random

    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 7)
    router = Router(
        registry_with(provider),
        table(
            Candidate(PRIMARY),
            retry=RetryPolicy(
                max_attempts=6,
                backoff=BackoffPolicy(initial=0.25, maximum=2.0),
                budget_seconds=600.0,
            ),
        ),
        sleep=timeline.sleep,
        clock=timeline.clock,
        unit=random.Random(99).random,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert len(set(timeline.waited)) > 1
    assert all(0.125 <= wait <= 2.0 for wait in timeline.waited)


# --- 429 and 503 are retried to the cap -----------------------------------


@pytest.mark.parametrize(
    ("error", "status"),
    [
        pytest.param(ProviderRateLimited("429 too many requests"), 429, id="429"),
        pytest.param(ProviderUnavailable("503 overloaded"), 503, id="503"),
        pytest.param(ProviderUnavailable("500 internal"), 500, id="500"),
        pytest.param(ProviderUnavailable("connection reset"), None, id="connection"),
    ],
)
async def test_a_transient_error_is_retried_up_to_the_cap_and_then_stops(
    error: ProviderError, status: int | None
) -> None:
    """`max_attempts` is a hard ceiling, and it counts the first try.

    The brief's "retry 429 and 503 up to the cap, then stop" is this assertion: five
    calls, five waits, and a failure chain that says five attempts.
    """
    timeline = Timeline()
    provider = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=[error] * 20, completions=(completion(),)
    )
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(
            max_attempts=5,
            backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
            budget_seconds=600.0,
        ),
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 5
    assert len(timeline.waited) == 4, "one wait fewer than attempts: no wait after the last"
    assert excinfo.value.failures[0].attempts == 5
    assert excinfo.value.failures[0].error is type(error)
    assert status in (None, 408, 429, 500, 502, 503, 504)


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderInvalidRequest("400 malformed"), id="400"),
        pytest.param(ProviderAuthError("401 bad key"), id="401"),
        pytest.param(ProviderInvalidRequest("403 forbidden"), id="403"),
    ],
)
async def test_a_4xx_that_is_not_408_or_429_is_never_retried(error: ProviderError) -> None:
    """One attempt, no backoff, and the route moves on immediately."""
    timeline = Timeline()
    provider = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(error,) + (ProviderTimeout("x"),) * 5
    )
    router = router_for(
        provider, timeline=timeline, retry=RetryPolicy(max_attempts=5, budget_seconds=600.0)
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 1
    assert timeline.waited == []
    assert excinfo.value.failures[0].attempts == 1
    assert excinfo.value.failures[0].error is type(error)


# --- the wall-clock budget -------------------------------------------------


async def test_the_budget_stops_the_loop_before_the_attempt_cap() -> None:
    """The count alone is not a deadline.

    Six attempts with a 2-second ceiling is ~20 seconds of backoff before the last
    attempt is even sent, so a route configured that way hangs a caller's request for
    longer than the budget it declared. Here the budget is 0.75s and `max_attempts` is
    20, so the cap is demonstrably *not* what ends the loop.

    The arithmetic is stated exactly because it *is* the property. The budget is
    checked *before* each wait, so the loop sleeps 0.25 (t=0.25), sleeps 0.50
    (t=0.75), runs the attempt that began at the budget line, and stops. Three
    attempts, two waits, and 0.75s of waiting spent — not a millisecond more. The
    attempt at the boundary is deliberate: the budget bounds muse's own *waiting*, and
    refusing to dispatch an attempt whose latency is already sunk would waste it.
    """
    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 30)
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(
            max_attempts=20,
            backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
            budget_seconds=0.75,
        ),
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 3
    assert timeline.waited == [0.25, 0.5]
    assert timeline.now == pytest.approx(0.75)
    assert excinfo.value.failures[0].attempts == 3


async def test_a_budget_smaller_than_the_first_backoff_stops_immediately() -> None:
    """The deadline is checked *before* the first wait, not after it.

    A budget of 0.1s against a 0.25s initial backoff means there is no time to wait
    even once, so the request fails on its first attempt and never sleeps. An
    implementation that checked the budget after the sleep would wait 0.25s — already
    over budget — and make the caller's request 2.5x longer than the deadline it
    published.
    """
    timeline = Timeline()
    provider = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),) * 5)
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(
            max_attempts=10,
            backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
            budget_seconds=0.1,
        ),
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 1
    assert timeline.waited == []


async def test_a_success_before_the_budget_never_notices_it() -> None:
    """A budget is a ceiling on failure, not a quota the request must spend.

    The second attempt succeeds, so the loop returns having slept once and having
    stayed well inside the budget. If the budget were enforced as "sleep the full
    amount" this would be a request that waits to fail at nothing.
    """
    timeline = Timeline()
    provider = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ProviderTimeout("x"),), completions=(completion(),)
    )
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(
            max_attempts=3,
            backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
            budget_seconds=30.0,
        ),
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.attempts == 2
    assert timeline.waited == [0.25]
    assert timeline.now == 0.25


async def test_the_budget_is_per_candidate_and_not_shared_across_the_chain() -> None:
    """Each candidate gets its own budget, and the route as a whole is bounded by the
    sum of them.

    A shared budget would be worse in the exact case it is meant to help: the primary
    burns the whole thing retrying, and the fallback — which is the candidate most
    likely to work — is then refused before it is tried once.
    """
    timeline = Timeline()
    primary = ScriptedProvider(name="primary", price=PRICE, errors=(ProviderTimeout("x"),) * 9)
    fallback = ScriptedProvider(
        name="fallback", price=PRICE, errors=(ProviderTimeout("y"),), completions=(completion(),)
    )
    router = Router(
        registry_with(primary, fallback),
        table(
            Candidate("primary"),
            Candidate("fallback"),
            retry=RetryPolicy(
                max_attempts=4,
                backoff=BackoffPolicy(initial=0.25, maximum=2.0, jitter=0.0),
                budget_seconds=0.75,
            ),
        ),
        sleep=timeline.sleep,
        clock=timeline.clock,
        unit=lambda: 1.0,
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == "fallback"
    # The primary's own budget stops it after three attempts (see the arithmetic in
    # `test_the_budget_stops_the_loop_before_the_attempt_cap`) rather than at the
    # route-wide cap of 4 — which is the evidence that the budget was not shared.
    assert primary.calls == 3
    assert fallback.calls == 2, "the fallback did not get a budget of its own"


# --- the double-billing case ----------------------------------------------


async def test_an_indeterminate_outcome_is_not_retried_by_default() -> None:
    """The packet's most important safety rule.

    A read timeout means the request was sent and the answer did not arrive. The
    vendor may have completed and billed it. Retrying therefore risks paying twice
    for one caller's request, and without an idempotency key there is no way to ask
    the vendor which of the two happened. So the default is one attempt.
    """
    timeline = Timeline()
    provider = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderIndeterminate("read timed out"),) + (ProviderTimeout("x"),) * 5,
    )
    router = router_for(
        provider, timeline=timeline, retry=RetryPolicy(max_attempts=5, budget_seconds=600.0)
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 1
    assert timeline.waited == []
    assert excinfo.value.failures[0].error is ProviderIndeterminate
    assert excinfo.value.failures[0].attempts == 1


async def test_an_indeterminate_outcome_is_retried_when_the_route_opts_in() -> None:
    """The knob exists, and it is per route rather than global.

    Global would be wrong in both directions: a deployment that accepts the
    double-bill risk on a 4-cent model does not accept it on the expensive one, and
    the route is exactly the unit that knows the price.
    """
    timeline = Timeline()
    provider = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderIndeterminate("read timed out"),) + (ProviderTimeout("x"),) * 5,
    )
    router = router_for(
        provider,
        timeline=timeline,
        retry=RetryPolicy(max_attempts=3, retry_indeterminate=True, budget_seconds=600.0),
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert provider.calls == 3
    assert excinfo.value.failures[0].attempts == 3


async def test_opting_in_does_not_unlock_the_permanent_errors() -> None:
    """The knob is about *ambiguity*, not about retrying harder.

    `retry_indeterminate` is not a "retry more" switch: a rejected credential and a
    malformed request are still one attempt each, because their outcomes are known
    and they will not change.
    """
    for error in (ProviderAuthError("401"), ProviderInvalidRequest("400")):
        timeline = Timeline()
        provider = ScriptedProvider(
            name=PRIMARY, price=PRICE, errors=(error,) + (ProviderTimeout("x"),) * 5
        )
        router = router_for(
            provider,
            timeline=timeline,
            retry=RetryPolicy(max_attempts=5, retry_indeterminate=True, budget_seconds=600.0),
        )

        with pytest.raises(AllCandidatesFailed):
            await router.route("fast", (("user", "hello"),))

        assert provider.calls == 1


# --- configuration ---------------------------------------------------------


def test_the_default_budget_bounds_a_route_that_configured_nothing() -> None:
    """A route that says nothing gets a deadline, not an open-ended loop.

    `max_attempts` defaults to 1 so the count is already safe; the budget default
    exists for the route an operator *does* turn retries on without thinking about
    the wall clock.
    """
    policy = RetryPolicy()

    assert policy.max_attempts == 1
    assert policy.budget_seconds > 0
    assert policy.retry_indeterminate is False
    assert policy.backoff.jitter == 0.5


def test_a_route_cannot_declare_a_budget_it_has_no_time_for() -> None:
    """`budget_seconds: 0` would mean "never retry", which is what `max_attempts: 1`
    is for.

    Accepting both would give an operator two spellings of one setting, and the
    difference is invisible until a route silently stops retrying.
    """
    with pytest.raises(ValueError, match="budget_seconds must be positive"):
        RetryPolicy(budget_seconds=0.0)


def test_jitter_outside_the_unit_interval_is_refused() -> None:
    """A jitter above 1 would push delays past the ceiling, which is the one thing
    the ceiling is for."""
    with pytest.raises(ValueError, match="jitter must be between 0 and 1"):
        BackoffPolicy(jitter=1.5)
    with pytest.raises(ValueError, match="jitter must be between 0 and 1"):
        BackoffPolicy(jitter=-0.1)
