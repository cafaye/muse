"""The router: request in, completion out, with a fallback chain behind it.

Three cases define this module and the packet asks for all three: the primary
succeeds, the primary fails and a fallback succeeds, and everything fails. Each
has its own section below, and each is asserted on the *whole* result — which
candidate served it, how many attempts each candidate took, and what the failure
chain says — because "it returned a completion" is not the property; "it returned
the fallback's completion after the primary was rate limited twice" is.

Two rules shape everything here:

- **A retry is for a transient failure, and only a transient failure.** Retrying a
  rejected credential or a malformed request spends the caller's latency to arrive
  at the same answer. `muse.errors.RETRYABLE_PROVIDER_ERRORS` is that list and it
  is explicit, not derived from a base class, so adding a `ProviderError`
  subclass does not silently change retry behaviour.
- **The price is looked up before the call is dispatched.** A model muse cannot
  price is never called, because a completion that cannot be metered is a
  completion whose spend is never billed. This is why a fallback test can assert
  that an unpriced primary made *zero* provider calls.

The retry backoff uses tenacity with an injected sleep, so the suite exercises real
backoff arithmetic without waiting: `stop_after_attempt` and `wait_exponential` are
the real objects, and `sleep` is a function that records what it was asked to wait
for.
"""

from __future__ import annotations

import pytest

from muse.errors import (
    AllCandidatesFailed,
    PriceUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
    RouteNotFound,
)
from muse.providers import Completion, Price, ProviderRegistry, UnregisteredProvider
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.redaction import Secret
from muse.router import Router
from muse.routes import (
    BackoffPolicy,
    Candidate,
    RetryPolicy,
    Route,
    RouteTable,
    TimeoutPolicy,
    routes_from_yaml,
)

from .conftest import ROUTES_YAML, write_routes

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PRIMARY = "primary"
FALLBACK = "fallback"
#: Both doubles price at the same rate, so a metered cost in a routing test is the
#: one the test computed rather than one that moved with the model.
PRICE = Price(1000, 2000)
#: A price table holding an entry for a *different* model, so a provider is
#: unpriced for the model under test without the double needing a "price nothing"
#: mode. An empty dict would read as "no table given", which means the opposite.
UNPRICED = {"some-other-model": Price(1, 1)}


def registry_with(*providers) -> ProviderRegistry:
    """A registry holding exactly `providers`, in the order given."""
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return registry


def table(*candidates: Candidate, model: str = "fast", **policy) -> RouteTable:
    """A one-route table, so a routing test states candidates and nothing else.

    `policy` is the `Route`'s own fields (retry, timeout, description). The router's
    knobs — `sleep` — go to `Router` directly, so a routing test never has to know
    which constructor a given knob belongs to.
    """
    return RouteTable(
        version=1,
        defaults=RetryPolicy(),
        routes=(Route(model=model, candidates=candidates, **policy),),
    )


def completion(provider: str = PRIMARY, content: str = "answered", **overrides) -> Completion:
    return Completion(
        **{
            "provider": provider,
            "model": "some-model",
            "content": content,
            "tokens_in": 10,
            "tokens_out": 5,
            "price": PRICE,
            **overrides,
        }
    )


def recording_sleep() -> tuple[list[float], object]:
    """A tenacity-compatible sleep that records instead of waiting.

    Returns the list of durations and the callable. Asserting on the durations is
    how the backoff policy is tested: an exponential that is not exponential, or a
    ceiling that is not respected, shows up as a list of the wrong numbers rather
    than as a slow suite.
    """
    waited: list[float] = []

    async def sleep(seconds: float) -> None:
        waited.append(seconds)

    return waited, sleep


# --- the three cases the packet names --------------------------------------


async def test_the_primary_serves_the_request_when_it_succeeds() -> None:
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == PRIMARY
    assert primary.calls == 1
    assert fallback.calls == 0


async def test_a_failing_primary_advances_to_the_fallback() -> None:
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ProviderTimeout("upstream timed out"),)
    )
    fallback = ScriptedProvider(
        name=FALLBACK, price=PRICE, completions=(completion(FALLBACK, "from the fallback"),)
    )
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == FALLBACK
    assert result.completion.content == "from the fallback"
    assert primary.calls == 1
    assert fallback.calls == 1


async def test_every_candidate_failing_raises_a_typed_error() -> None:
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("slow"),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, errors=(ProviderAuthError("bad key"),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert excinfo.value.model == "fast"
    assert [failure.provider for failure in excinfo.value.failures] == [PRIMARY, FALLBACK]


async def test_the_failure_chain_records_each_candidates_error() -> None:
    """The chain is the only place the whole attempt sequence exists, and it is what
    a support ticket gets. "it failed" is not actionable; "openai timed out twice
    and anthropic rejected the credential" is."""
    primary = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderTimeout("timed out"), ProviderTimeout("timed out again")),
    )
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, errors=(ProviderAuthError("nope"),))
    router = Router(
        registry_with(primary, fallback),
        table(Candidate(PRIMARY), Candidate(FALLBACK), retry=RetryPolicy(max_attempts=2)),
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    failures = excinfo.value.failures
    assert failures[0].error is ProviderTimeout
    assert failures[0].attempts == 2
    assert "timed out again" in failures[0].detail
    assert failures[1].error is ProviderAuthError
    assert failures[1].attempts == 1


async def test_the_failure_chain_never_carries_a_credential() -> None:
    """The chain is rendered into a `problem+json` body and a log line. A provider
    that echoes the key it rejected must not reach either."""
    from muse.redaction import redact

    primary = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderAuthError("Incorrect API key provided: sk-live-SECRET"),),
    )
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),), secrets=("sk-live-SECRET",))

    assert "sk-live-SECRET" not in str(excinfo.value)
    assert redact(str(excinfo.value), Secret("sk-live-SECRET")) == str(excinfo.value)


async def test_the_failure_detail_survives_untouched_when_no_credential_is_held() -> None:
    """The no-secrets path. The provider's own text is the most useful thing in the
    detail, so nothing is lost when there is nothing to scrub — and this is the
    common case for a route whose provider takes no key.
    """
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ProviderTimeout("gateway timeout after 30s"),)
    )
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert excinfo.value.failures[0].detail == "gateway timeout after 30s"


async def test_several_credentials_are_all_scrubbed_from_one_detail() -> None:
    """A route with two vendors holds two keys, and a provider message could contain
    either. Scrubbing only the first would leave the second in a log line."""
    primary = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderAuthError("tried sk-one then sk-two"),),
    )
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),), secrets=("sk-one", Secret("sk-two")))

    detail = excinfo.value.failures[0].detail
    assert "sk-one" not in detail
    assert "sk-two" not in detail


# --- candidate ordering ----------------------------------------------------


async def test_candidates_are_tried_in_the_order_the_route_lists_them() -> None:
    """Order is the route's whole meaning, so the first candidate is always tried
    first even when a later one would work."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    await router.route("fast", (("user", "hello"),))

    assert (primary.calls, fallback.calls) == (1, 0)


async def test_a_later_candidate_is_reached_only_after_the_earlier_ones_fail() -> None:
    """The second candidate is not tried "in parallel just in case": a request sent
    to two vendors is billed by two vendors."""
    first = ScriptedProvider(name="first", price=PRICE, errors=(ProviderUnavailable("down"),))
    second = ScriptedProvider(name="second", price=PRICE, errors=(ProviderUnavailable("down"),))
    third = ScriptedProvider(name="third", price=PRICE, completions=(completion("third"),))
    router = Router(
        registry_with(first, second, third),
        table(Candidate("first"), Candidate("second"), Candidate("third")),
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == "third"
    assert (first.calls, second.calls, third.calls) == (1, 1, 1)


async def test_a_second_failure_does_not_retry_the_first_candidate() -> None:
    """Once a candidate is exhausted the router moves on. Going back to the primary
    after the fallback failed is a retry policy stated in two places."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderUnavailable("down"),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, errors=(ProviderUnavailable("down"),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert (primary.calls, fallback.calls) == (1, 1)


# --- retry and backoff -----------------------------------------------------


async def test_a_transient_failure_is_retried_on_the_same_candidate() -> None:
    """The point of `max_attempts`: a timeout is often a timeout, and the second
    attempt is the same request to a provider that has just recovered."""
    primary = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderTimeout("slow"),),
        completions=(completion(),),
    )
    router = Router(
        registry_with(primary), table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=2))
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == PRIMARY
    assert primary.calls == 2


async def test_a_permanent_failure_is_not_retried() -> None:
    """A rejected credential is a rejected credential. Two attempts at it means two
    chances to trip the provider's auth-failure rate limit, for a guaranteed
    identical answer."""
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ProviderAuthError("bad key"), ProviderTimeout("slow"))
    )
    router = Router(
        registry_with(primary), table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=3))
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert primary.calls == 1


@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ProviderRateLimited("429"), id="rate-limited"),
        pytest.param(ProviderTimeout("slow"), id="timeout"),
        pytest.param(ProviderUnavailable("503"), id="unavailable"),
    ],
)
async def test_every_transient_error_is_retried(error: ProviderError) -> None:
    """All three of the transient classes, explicitly. A test that only covered
    timeouts would let a rate limit start failing requests instead of retrying."""
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(error,), completions=(completion(),)
    )
    router = Router(
        registry_with(primary), table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=2))
    )

    assert (await router.route("fast", (("user", "hello"),))).completion.provider == PRIMARY
    assert primary.calls == 2


async def test_the_backoff_grows_exponentially() -> None:
    """`wait_exponential` from tenacity, asserted on the durations it asked for. A
    linear or flat backoff would pass a test that only counted attempts, and a flat
    backoff against a provider that is already overloaded is a hot loop."""
    primary = ScriptedProvider(
        name=PRIMARY,
        price=PRICE,
        errors=(ProviderTimeout("1"), ProviderTimeout("2"), ProviderTimeout("3")),
    )
    waited, sleep = recording_sleep()
    router = Router(
        registry_with(primary),
        table(
            Candidate(PRIMARY),
            retry=RetryPolicy(max_attempts=3, backoff=BackoffPolicy(initial=0.25, maximum=2.0)),
        ),
        sleep=sleep,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert waited == [0.25, 0.5]


async def test_the_backoff_respects_its_ceiling() -> None:
    """Unbounded exponential backoff against a provider that is down for minutes is
    a request that hangs. The ceiling is what bounds it."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("nope"),) * 6)
    waited, sleep = recording_sleep()
    router = Router(
        registry_with(primary),
        table(
            Candidate(PRIMARY),
            retry=RetryPolicy(max_attempts=6, backoff=BackoffPolicy(initial=1.0, maximum=4.0)),
        ),
        sleep=sleep,
    )

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert waited == [1.0, 2.0, 4.0, 4.0, 4.0]
    assert max(waited) <= 4.0


async def test_a_single_attempt_never_sleeps() -> None:
    """The default. A route that has not measured a real incident should not be
    adding backoff to every failure."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("slow"),))
    waited, sleep = recording_sleep()
    router = Router(registry_with(primary), table(Candidate(PRIMARY)), sleep=sleep)

    with pytest.raises(AllCandidatesFailed):
        await router.route("fast", (("user", "hello"),))

    assert waited == []


async def test_a_successful_candidate_sleeps_nothing() -> None:
    """Backoff is between failed attempts, never after a success."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    waited, sleep = recording_sleep()
    router = Router(
        registry_with(primary),
        table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=3)),
        sleep=sleep,
    )

    await router.route("fast", (("user", "hello"),))

    assert waited == []


# --- price before dispatch -------------------------------------------------


async def test_an_unpriced_primary_is_skipped_without_being_called() -> None:
    """The check that makes unmetered spend impossible. `PriceUnavailable` is raised
    before `complete`, so the provider sees zero requests and the fallback serves
    the call."""
    primary = ScriptedProvider(name=PRIMARY, prices=UNPRICED)
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    result = await router.route("fast", (("user", "hello"),))

    assert primary.calls == 0
    assert result.completion.provider == FALLBACK


async def test_an_unpriced_model_reaches_the_failure_chain_rather_than_a_zero_price() -> None:
    """`PriceUnavailable`, not a zero cost. A zero would flow into the metered event
    and make the call free, silently, for the whole history of the model."""
    primary = ScriptedProvider(name=PRIMARY, prices=UNPRICED)
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert excinfo.value.failures[0].error is PriceUnavailable


async def test_every_candidate_unpriced_fails_rather_than_serving_a_free_completion() -> None:
    """No candidate may serve a completion muse cannot price. The alternative is a
    `ZeroDivisionError`-shaped hole in the billing path."""
    primary = ScriptedProvider(name=PRIMARY, prices=UNPRICED)
    fallback = ScriptedProvider(name=FALLBACK, prices=UNPRICED)
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert [failure.error for failure in excinfo.value.failures] == [
        PriceUnavailable,
        PriceUnavailable,
    ]


# --- unknown models and providers ------------------------------------------


async def test_an_unrouted_model_raises_before_any_provider_is_called() -> None:
    """`RouteNotFound`, not a 503. A request naming a model nobody routes is a
    caller mistake with a caller-fixable answer, and the distinction is the whole
    value of a typed error."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY), model="fast"))

    with pytest.raises(RouteNotFound) as excinfo:
        await router.route("nonexistent", (("user", "hello"),))

    assert excinfo.value.model == "nonexistent"
    assert primary.calls == 0


async def test_an_unregistered_provider_is_a_configuration_error() -> None:
    """`UnregisteredProvider` is not a `ProviderError`, so it is not caught by the
    candidate loop and does not become a 503. A route naming a provider that does
    not exist is a boot-time mistake that should never have reached a request —
    `RouteTable.validate` is the check that catches it properly."""
    router = Router(ProviderRegistry(), table(Candidate(PRIMARY)))

    with pytest.raises(UnregisteredProvider):
        await router.route("fast", (("user", "hello"),))


# --- the result ------------------------------------------------------------


async def test_the_result_reports_the_route_and_the_vendor_model_separately() -> None:
    """Two fields, not one. `route` is what a caller configured and must stay
    meaningful when a fallback changes vendor; `model` is the vendor's own model id,
    which is what an operator needs to find the call in a provider's dashboard.

    The route is `smart` and the candidate serves `claude-sonnet-4-5`, so the two
    genuinely differ here — a single field could not satisfy both readers.
    """
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, completions=(completion(model="claude-sonnet-4-5"),)
    )
    router = Router(
        registry_with(primary),
        table(Candidate(PRIMARY, model="claude-sonnet-4-5"), model="smart"),
    )

    result = await router.route("smart", (("user", "hello"),))

    assert result.route == "smart"
    assert result.model == "claude-sonnet-4-5"
    assert result.completion.provider == PRIMARY


async def test_a_candidate_inheriting_the_route_model_reports_it_as_both() -> None:
    """The common case, where the two names agree — asserted so the field is not
    quietly empty on a route that does not override its candidates' models."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(model="fast"),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY), model="fast"))

    result = await router.route("fast", (("user", "hello"),))

    assert result.route == "fast"
    assert result.model == "fast"


async def test_the_result_counts_the_candidates_it_tried() -> None:
    """One number, asserted on, because it is what a dashboard plots and what a
    fallback that has started firing constantly would show."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("slow"),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    assert (await router.route("fast", (("user", "hello"),))).candidates_tried == 2


async def test_the_result_counts_the_total_provider_attempts() -> None:
    """Distinct from `candidates_tried`: two attempts at one candidate is a retry,
    and a metered event wants to be able to distinguish that from a fallback."""
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ProviderTimeout("slow"),), completions=(completion(),)
    )
    router = Router(
        registry_with(primary), table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=2))
    )

    result = await router.route("fast", (("user", "hello"),))

    assert result.candidates_tried == 1
    assert result.attempts == 2


async def test_the_result_carries_the_metered_cost() -> None:
    """10 in at 1000/1k and 5 out at 2000/1k is 10 + 10 = 20 micros. The router
    reports it so the endpoint does not recompute it from a different price table."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    assert (await router.route("fast", (("user", "hello"),))).cost_micros == 20


# --- request shaping -------------------------------------------------------


async def test_the_router_builds_a_request_from_the_messages() -> None:
    """The router is where `(role, content)` tuples become a `CompletionRequest`, so
    a bad message is rejected here rather than at a provider."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    await router.route("fast", (("system", "be brief"), ("user", "hello")))

    request = primary.requests[0]
    assert [message.role for message in request.messages] == ["system", "user"]


async def test_the_router_passes_the_route_model_to_the_provider() -> None:
    """The provider gets the *candidate's* model, not the route's. They usually
    differ — the route is `smart`, the candidate is `claude-sonnet-4-5` — and
    sending the route name to a vendor is a 404."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary),
        RouteTable(
            version=1,
            defaults=RetryPolicy(),
            routes=(
                Route(model="smart", candidates=(Candidate(PRIMARY, model="claude-sonnet-4-5"),)),
            ),
        ),
    )

    await router.route("smart", (("user", "hello"),))

    assert primary.requests[0].model == "claude-sonnet-4-5"


async def test_an_empty_message_list_is_rejected_before_any_provider_is_called() -> None:
    """Validated at the router so the failure is a typed 422 and not a provider's
    differently-worded 400."""
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(ValueError, match="at least one message"):
        await router.route("fast", ())

    assert primary.calls == 0


async def test_the_router_rejects_an_unknown_role() -> None:
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    with pytest.raises(ValueError, match="role must be one of"):
        await router.route("fast", (("captain", "hello"),))


async def test_optional_parameters_reach_the_provider() -> None:
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, completions=(completion(),))
    router = Router(registry_with(primary), table(Candidate(PRIMARY)))

    await router.route("fast", (("user", "hello"),), max_tokens=64, temperature=0.1)

    assert primary.requests[0].max_tokens == 64
    assert primary.requests[0].temperature == 0.1


# --- an unusual response ---------------------------------------------------


async def test_a_malformed_response_falls_through_to_the_next_candidate() -> None:
    """`ResponseShapeError` is not retried — the same adapter will produce the same
    shape — but it must not end the request either. An upstream that answered with
    something unreadable is exactly what a fallback is for."""
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ResponseShapeError("no choices"),)
    )
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(),))
    router = Router(
        registry_with(primary, fallback), table(Candidate(PRIMARY), Candidate(FALLBACK))
    )

    assert (await router.route("fast", (("user", "hello"),))).completion.provider == FALLBACK
    assert primary.calls == 1


async def test_a_malformed_response_is_not_retried_on_the_same_candidate() -> None:
    primary = ScriptedProvider(
        name=PRIMARY, price=PRICE, errors=(ResponseShapeError("no choices"), ProviderTimeout("x"))
    )
    router = Router(
        registry_with(primary), table(Candidate(PRIMARY), retry=RetryPolicy(max_attempts=3))
    )

    with pytest.raises(AllCandidatesFailed) as excinfo:
        await router.route("fast", (("user", "hello"),))

    assert excinfo.value.failures[0].attempts == 1


# --- routes.yaml -----------------------------------------------------------


def test_the_committed_routes_file_loads() -> None:
    """The example in the repository is the one a deploy reads, so it is under test
    rather than trusted. A typo in it is a 503 on the first request otherwise."""
    table = routes_from_yaml(ROUTES_YAML.read_text())
    assert table.models() == ("fast", "smart")


def test_the_committed_routes_file_is_valid_against_the_registry() -> None:
    """Every provider it names is one the registry can resolve — checked here rather
    than at boot so a bad name is a test failure instead of a container that will
    not start."""
    table = routes_from_yaml(ROUTES_YAML.read_text())
    registry = registry_with(FakeProvider(name="openai"), FakeProvider(name="anthropic"))
    table.validate(registry)


def test_the_committed_routes_file_gives_every_route_a_fallback() -> None:
    """A route with one candidate is a single vendor with extra syntax. The file
    ships two, and the test says so rather than leaving it to review."""
    table = routes_from_yaml(ROUTES_YAML.read_text())
    assert all(len(route.candidates) >= 2 for route in table.routes)


def test_routes_load_from_yaml() -> None:
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "defaults": {"max_attempts": 2},
                "routes": [
                    {
                        "model": "fast",
                        "candidates": [
                            {"provider": "openai", "model": "gpt-4o-mini"},
                            {"provider": "anthropic"},
                        ],
                    }
                ],
            }
        )
    )
    route = table.routes[0]
    assert route.model == "fast"
    assert [candidate.provider for candidate in route.candidates] == ["openai", "anthropic"]


def test_a_candidate_without_a_model_inherits_the_routes_model() -> None:
    """The common case where the client-facing name and the vendor's name agree.
    Making it explicit everywhere would be noise in every route."""
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "routes": [{"model": "gpt-4o-mini", "candidates": [{"provider": "openai"}]}],
            }
        )
    )
    assert table.routes[0].candidates[0].model == "gpt-4o-mini"


def test_a_route_inherits_the_default_retry_policy() -> None:
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "defaults": {"max_attempts": 4},
                "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}],
            }
        )
    )
    assert table.routes[0].retry.max_attempts == 4


def test_a_route_overrides_the_default_retry_policy() -> None:
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "defaults": {"max_attempts": 1},
                "routes": [
                    {
                        "model": "f",
                        "candidates": [{"provider": "openai"}],
                        "max_attempts": 5,
                    }
                ],
            }
        )
    )
    assert table.routes[0].retry.max_attempts == 5


def test_a_route_overrides_the_default_backoff_and_timeout() -> None:
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "defaults": {"backoff_initial_seconds": 0.25, "timeout_seconds": 30.0},
                "routes": [
                    {
                        "model": "f",
                        "candidates": [{"provider": "openai"}],
                        "backoff_initial_seconds": 1.5,
                        "timeout_seconds": 90.0,
                    }
                ],
            }
        )
    )
    route = table.routes[0]
    assert route.retry.backoff.initial == 1.5
    assert route.timeout == TimeoutPolicy(seconds=90.0)


def test_defaults_are_used_when_no_defaults_block_exists() -> None:
    """A minimal file with no `defaults` still loads, with the conservative
    defaults: one attempt, no retry."""
    table = routes_from_yaml(
        write_routes(
            {"version": 1, "routes": [{"model": "f", "candidates": [{"provider": "openai"}]}]}
        )
    )
    assert table.routes[0].retry.max_attempts == 1
    assert table.defaults.max_attempts == 1


async def test_a_router_built_from_yaml_routes_end_to_end() -> None:
    """The path a deploy actually takes: a YAML file, a registry, and a request.
    Asserted end to end so the loader and the router cannot each be right about a
    different shape."""
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "routes": [
                    {
                        "model": "fast",
                        "candidates": [
                            {"provider": PRIMARY, "model": "model-a"},
                            {"provider": FALLBACK, "model": "model-b"},
                        ],
                    }
                ],
            }
        )
    )
    primary = ScriptedProvider(name=PRIMARY, price=PRICE, errors=(ProviderTimeout("slow"),))
    fallback = ScriptedProvider(name=FALLBACK, price=PRICE, completions=(completion(FALLBACK),))
    router = Router(registry_with(primary, fallback), table)

    result = await router.route("fast", (("user", "hello"),))

    assert result.completion.provider == FALLBACK
    assert fallback.requests[0].model == "model-b"


# --- construction ----------------------------------------------------------


def test_the_default_sleep_is_a_real_sleep() -> None:
    """The production path. A router constructed without an injected sleep must
    actually wait, or the backoff policy is decoration in production only."""
    import inspect

    from muse.router import _asyncio_sleep

    assert inspect.iscoroutinefunction(_asyncio_sleep)


def test_a_router_reports_its_route_table() -> None:
    """So `/readyz` or a future admin surface can render the table without reaching
    into the router's internals."""
    routes = table(Candidate(PRIMARY))
    router = Router(ProviderRegistry(), routes)
    assert router.routes is routes


def test_a_router_repr_names_its_routes() -> None:
    """A `repr` that does not say what a router is configured with is a `repr` that
    costs a debugger a step. The route names are the whole of its configuration."""
    routes = table(Candidate(PRIMARY), model="fast")
    assert "fast" in repr(Router(ProviderRegistry(), routes))


def test_a_router_with_no_routes_reprs_as_empty() -> None:
    assert "[]" in repr(
        Router(ProviderRegistry(), RouteTable(version=1, defaults=RetryPolicy(), routes=()))
    )
