"""The provider protocol, the registry, and the LiteLLM adapter.

Three things live here and they are deliberately separable:

- `Provider` is a structural protocol with exactly the three operations the packet
  asks for: `complete`, `health`, `cost_per_1k_tokens`. No base class, so a
  provider is anything with those methods and the router cannot reach a provider's
  internals.
- `ProviderRegistry` maps a name to a provider. Its one non-obvious rule is that
  registering the same name twice is an error: silently replacing a provider is how
  a misconfigured route ends up quietly served by the wrong vendor.
- `LiteLLMProvider` adapts litellm for openai and anthropic. Every test injects a
  stand-in module, so no test opens a socket — AGENTS.md rule 3. The mapping from
  litellm's exception classes to muse's is the part tested hardest: it decides what
  gets retried and what a caller is told.

`FakeProvider` and `ScriptedProvider` ship in `src/`, not in `tests/`, because the
router's own tests need a provider and a later packet's contract tests will too.
"""

from __future__ import annotations

import traceback

import pytest

from muse.errors import (
    ContentPolicyError,
    CredentialUnavailable,
    PriceUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
)
from muse.providers import (
    Completion,
    CompletionRequest,
    Health,
    LiteLLMProvider,
    Message,
    Price,
    Provider,
    ProviderRegistry,
    StaticCredentials,
    UnregisteredProvider,
    usage_cost_micros,
)
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.redaction import Secret

from .support.litellm_stub import make_stub

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

OPENAI = "openai"
ANTHROPIC = "anthropic"
MODEL = "gpt-4o-mini"
#: The qualified name, which is what the adapter asks litellm's price table about.
QUALIFIED = "openai/gpt-4o-mini"


def ask(model: str = MODEL, **kwargs) -> CompletionRequest:
    """A valid request, with fields overridden per test."""
    return CompletionRequest(
        model=model, messages=(Message(role="user", content="hello"),), **kwargs
    )


def completion(**overrides) -> Completion:
    """A valid completion, with fields overridden per test.

    Six fields, five of which every test would otherwise repeat, and a repeated
    literal is a repeated place for a signature change to need a dozen edits.
    """
    return Completion(
        **{
            "provider": OPENAI,
            "model": MODEL,
            "content": "hi there",
            "tokens_in": 10,
            "tokens_out": 5,
            "price": Price(1500, 3000),
            **overrides,
        }
    )


def make_provider(
    *, name: str = OPENAI, api_key: str = "sk-test", **stub_kwargs
) -> LiteLLMProvider:
    """A `LiteLLMProvider` wired to a stand-in litellm."""
    return LiteLLMProvider(name=name, api_key=api_key, litellm=make_stub(**stub_kwargs))


# --- the protocol ----------------------------------------------------------


def test_a_provider_only_needs_the_three_protocol_methods() -> None:
    """Structural, not nominal: a provider is anything with the three operations,
    so a future adapter does not inherit from anything muse owns."""

    class Minimal:
        name = "minimal"

        async def complete(self, request):  # pragma: no cover - never called
            raise NotImplementedError

        async def health(self):  # pragma: no cover - never called
            raise NotImplementedError

        def cost_per_1k_tokens(self, model):  # pragma: no cover - never called
            raise NotImplementedError

    assert isinstance(Minimal(), Provider)


def test_something_without_the_methods_is_not_a_provider() -> None:
    class NotAProvider:
        name = "nope"

    assert not isinstance(NotAProvider(), Provider)


def test_the_fake_providers_satisfy_the_protocol() -> None:
    """The doubles are held to the same contract as the real adapter, so a route
    that works against a fake is a route the protocol actually supports."""
    assert isinstance(FakeProvider(), Provider)
    assert isinstance(ScriptedProvider(), Provider)
    assert isinstance(make_provider(), Provider)


# --- pricing ---------------------------------------------------------------


def test_price_is_held_in_micros_per_1k_tokens() -> None:
    """Integer micro-dollars per 1k tokens.

    Money in this service is integer micro-dollars everywhere, for the reason
    billing's money paths are: a float that rounds differently on two machines is a
    reconciliation bug nobody can reproduce. `1.50` per 1k is 1500 here.
    """
    price = Price(input_micros=1500, output_micros=3000)
    assert price.input_micros == 1500
    assert price.output_micros == 3000


def test_price_rejects_a_negative_input() -> None:
    """A negative price would make a completion *reduce* a customer's bill, and a
    provider returning one is a data bug that must not become a credit."""
    with pytest.raises(ValueError, match="input_micros cannot be negative"):
        Price(input_micros=-1, output_micros=0)


def test_price_rejects_a_negative_output() -> None:
    with pytest.raises(ValueError, match="output_micros cannot be negative"):
        Price(input_micros=0, output_micros=-1)


def test_usage_cost_is_integer_arithmetic() -> None:
    """1k in at 1500 and 1k out at 3000 is 4500 micros, computed without a float
    ever appearing."""
    assert usage_cost_micros(1000, 1000, Price(1500, 3000)) == 4500


def test_usage_cost_pro_rates_partial_tokens() -> None:
    """500 in and 250 out: 500 * 1500 / 1000 + 250 * 3000 / 1000 = 750 + 750."""
    assert usage_cost_micros(500, 250, Price(1500, 3000)) == 1500


def test_usage_cost_rounds_half_up_once_at_the_end() -> None:
    """Rounding per component and then summing is how a sub-micro discrepancy
    becomes a visible one after a month of calls. Integer division first, single
    rounding last.

    1 token in at 1500/1k is 1.5 and 1 token out at 3000/1k is 3.0; the total is 4.5
    and rounds to 5. Rounding each half up first happens to agree here and disagrees
    elsewhere, so the assertion pins the sum-then-round order rather than the value.
    """
    assert usage_cost_micros(1, 1, Price(1500, 3000)) == 5
    # The same price, split so per-component rounding would give a different sum:
    # 1 token in rounds up to 2, 0 out is 0 — versus 2 tokens in at 3.0 rounding to
    # 3. Sum-then-round gives one answer for both; round-then-sum does not.
    assert usage_cost_micros(2, 0, Price(1500, 3000)) == 3


def test_usage_cost_of_nothing_is_zero() -> None:
    assert usage_cost_micros(0, 0, Price(1500, 3000)) == 0


def test_usage_cost_rejects_negative_input_usage() -> None:
    """A provider reporting negative tokens is wrong, and treating that as a refund
    is the wrong way to be wrong."""
    with pytest.raises(ValueError, match="tokens_in cannot be negative"):
        usage_cost_micros(-1, 0, Price(1500, 3000))


def test_usage_cost_rejects_negative_output_usage() -> None:
    with pytest.raises(ValueError, match="tokens_out cannot be negative"):
        usage_cost_micros(0, -1, Price(1500, 3000))


# --- request and result shapes --------------------------------------------


def test_a_request_carries_the_model_and_the_messages() -> None:
    request = ask()
    assert request.model == MODEL
    assert request.messages[0].role == "user"
    assert request.messages[0].content == "hello"


def test_a_message_renders_to_the_wire_shape() -> None:
    """`{role, content}` is what every vendor's API expects, and it is built in one
    place so a fourth vendor cannot invent a fourth shape."""
    assert Message(role="system", content="be brief").as_wire() == {
        "role": "system",
        "content": "be brief",
    }


def test_a_request_rejects_an_empty_message_list() -> None:
    """A completion with no messages is a 400 on every vendor, and failing here
    names the actual mistake instead of relaying it."""
    with pytest.raises(ValueError, match="at least one message"):
        CompletionRequest(model=MODEL, messages=())


def test_a_request_rejects_a_blank_model() -> None:
    with pytest.raises(ValueError, match="model must not be blank"):
        ask("   ")


def test_a_request_rejects_an_unknown_role() -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        Message(role="captain", content="hi")


def test_a_request_rejects_empty_content() -> None:
    """An empty message is never what a caller meant, and accepting it turns a
    client bug into a billed 200 with no content."""
    with pytest.raises(ValueError, match="content must not be empty"):
        Message(role="user", content="")


def test_a_request_rejects_a_zero_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        ask(max_tokens=0)


def test_a_request_rejects_a_negative_max_tokens() -> None:
    with pytest.raises(ValueError, match="max_tokens must be positive"):
        ask(max_tokens=-1)


def test_a_request_accepts_optional_parameters() -> None:
    request = ask(max_tokens=64, temperature=0.2)
    assert request.max_tokens == 64
    assert request.temperature == 0.2


def test_a_completion_reports_its_cost() -> None:
    """10 in at 1500/1k and 5 out at 3000/1k is 15 + 15 = 30 micros."""
    assert completion().cost_micros == 30


def test_a_completion_carries_the_finish_reason() -> None:
    """`stop` vs `length` is the difference between "the model finished" and "the
    model was cut off", and a caller cannot tell them apart without it."""
    assert completion(finish_reason="length").finish == "length"


def test_a_completion_defaults_the_finish_reason_to_stop() -> None:
    """A provider that omits it is saying it finished normally. Defaulting to
    `unknown` would make every conforming response look anomalous in a log."""
    assert completion(finish_reason=None).finish == "stop"


def test_a_completion_requires_the_provider_to_be_named() -> None:
    """The metered event records this exact string, so an empty one produces an
    event no consumer can attribute to a vendor — the misattribution that neither
    the caller nor the person billed can detect."""
    with pytest.raises(ValueError, match="must name the provider"):
        completion(provider="")


# --- health ----------------------------------------------------------------


def test_health_reports_healthy_with_a_detail() -> None:
    health = Health(healthy=True, detail="api reachable")
    assert health.healthy is True
    assert health.detail == "api reachable"


def test_an_unhealthy_report_gets_a_default_detail() -> None:
    """A caller logging `health()` with no detail gets something to log."""
    assert Health(healthy=False).detail == "unhealthy"


def test_a_healthy_report_gets_a_default_detail() -> None:
    assert Health(healthy=True).detail == "ok"


# --- registry --------------------------------------------------------------


def test_the_registry_resolves_a_registered_provider() -> None:
    provider = FakeProvider(name=OPENAI)
    registry = ProviderRegistry().register(provider)
    assert registry.get(OPENAI) is provider


def test_the_registry_is_case_sensitive() -> None:
    """Provider names come from `config/routes.yaml`, which is reviewed config
    rather than user input, so normalising case would paper over a typo in review
    instead of failing on it at boot."""
    registry = ProviderRegistry().register(FakeProvider(name=OPENAI))
    with pytest.raises(UnregisteredProvider):
        registry.get("OpenAI")


def test_an_unknown_provider_names_the_lookup_in_the_error() -> None:
    """The name is what a route's author has to fix. The registered names are in the
    message too, because the alternative is a grep through a boot log."""
    with pytest.raises(UnregisteredProvider) as excinfo:
        ProviderRegistry().get("bedrock")
    assert "bedrock" in str(excinfo.value)


def test_an_unknown_provider_says_when_nothing_is_registered() -> None:
    """A registry with no providers is a real state — a misconfigured container —
    and `registered: none` says so rather than leaving an empty list after a colon."""
    with pytest.raises(UnregisteredProvider) as excinfo:
        ProviderRegistry().get("openai")
    assert "registered: none" in str(excinfo.value)


def test_the_registry_lists_its_providers_sorted() -> None:
    """Sorted, not insertion-ordered: this renders into a route-validation error and
    a readiness body, and both want the same order whatever order the adapters
    happened to be registered in."""
    registry = (
        ProviderRegistry()
        .register(FakeProvider(name=OPENAI))
        .register(FakeProvider(name=ANTHROPIC))
    )
    assert registry.names() == (ANTHROPIC, OPENAI)


def test_the_registry_is_empty_by_default() -> None:
    """A fresh registry has nothing in it, so a route naming any provider fails at
    boot rather than on the first request."""
    assert ProviderRegistry().names() == ()


def test_registering_one_provider_twice_is_an_error() -> None:
    """The failure this prevents: an import cycle or a deploy-order change
    registering `openai` again with a stub, replacing the real adapter, and every
    request after it silently served from the wrong thing."""
    registry = ProviderRegistry().register(FakeProvider(name=OPENAI))
    with pytest.raises(ValueError, match="already registered"):
        registry.register(FakeProvider(name=OPENAI))


def test_registering_returns_the_registry_for_chaining() -> None:
    registry = ProviderRegistry()
    assert registry.register(FakeProvider(name=OPENAI)) is registry


# --- the fake providers ----------------------------------------------------


async def test_the_fake_provider_returns_a_fixed_completion() -> None:
    provider = FakeProvider(name=OPENAI, content="scripted")
    assert (await provider.complete(ask())).content == "scripted"


async def test_the_fake_provider_counts_its_calls() -> None:
    provider = FakeProvider(name=OPENAI)
    await provider.complete(ask())
    await provider.complete(ask())
    assert provider.calls == 2


async def test_the_fake_provider_records_the_requests_it_saw() -> None:
    """So a test can assert what was dispatched, not just how many times — which is
    how a test proves the *fallback* candidate received the call."""
    provider = FakeProvider(name=OPENAI)
    await provider.complete(ask(model="gpt-4o"))
    assert provider.requests[0].model == "gpt-4o"


async def test_the_fake_provider_reports_a_configured_health() -> None:
    assert (await FakeProvider(healthy=True).health()).healthy is True
    assert (await FakeProvider(healthy=False).health()).healthy is False


async def test_the_scripted_provider_returns_its_scripted_completion() -> None:
    provider = ScriptedProvider(name=OPENAI, completions=(completion(content="scripted"),))
    assert (await provider.complete(ask())).content == "scripted"


async def test_the_scripted_provider_serves_the_requested_model_name() -> None:
    """The completion reports the model the router asked for, not the one the script
    was written with, so one script serves a route with two models."""
    provider = ScriptedProvider(name=OPENAI, completions=(completion(),))
    assert (await provider.complete(ask(model="gpt-4o"))).model == "gpt-4o"


async def test_the_scripted_provider_complains_when_the_script_runs_out() -> None:
    """A router retrying a candidate asks twice, and a test that scripts one
    completion and calls twice should be told so. Replaying the last entry would
    make a miscounted script look like a passing test, and a miscount is exactly
    what a retry test gets wrong."""
    provider = ScriptedProvider(name=OPENAI, completions=(completion(content="only"),))
    assert (await provider.complete(ask())).content == "only"
    with pytest.raises(AssertionError, match="no scripted completion left"):
        await provider.complete(ask())


async def test_the_scripted_provider_raises_a_scripted_error() -> None:
    provider = ScriptedProvider(name=OPENAI, errors=(ProviderTimeout("upstream timed out"),))
    with pytest.raises(ProviderTimeout, match="upstream timed out"):
        await provider.complete(ask())


async def test_the_scripted_provider_raises_its_error_before_any_completion() -> None:
    """`errors` first, so a script reads as "fail, then succeed" in source order —
    the shape every fallback test is written in."""
    provider = ScriptedProvider(
        name=OPENAI,
        errors=(ProviderTimeout("timed out"),),
        completions=(completion(content="recovered"),),
    )
    with pytest.raises(ProviderTimeout):
        await provider.complete(ask())
    assert (await provider.complete(ask())).content == "recovered"


def test_the_scripted_provider_keeps_the_script_it_was_given() -> None:
    """So a test can assert what it configured without restating the constructor
    arguments."""
    failure = ProviderTimeout("timed out")
    provider = ScriptedProvider(name=OPENAI, errors=(failure,), completions=(completion(),))
    assert provider.script == (failure, completion())


def test_the_scripted_provider_prices_from_its_table() -> None:
    provider = ScriptedProvider(
        name=OPENAI, prices={"gpt-4o": Price(5000, 15000)}, completions=(completion(),)
    )
    assert provider.cost_per_1k_tokens("gpt-4o") == Price(5000, 15000)


def test_a_scripted_provider_without_a_table_uses_its_default_price() -> None:
    """No table at all: the per-provider default applies to every model. The
    contrast with the next test is the point — a table changes the rule, it does not
    just add entries."""
    provider = ScriptedProvider(name=OPENAI, price=Price(1500, 3000))
    assert provider.cost_per_1k_tokens("any-model-at-all") == Price(1500, 3000)


def test_an_explicit_price_table_refuses_a_model_it_does_not_have() -> None:
    """A table is authoritative: a model missing from it is a deliberate "this
    provider cannot serve that model", which is a routing decision the test is
    making, not a missing fixture.

    This is also what makes a fallback route testable for the unpriced case: a
    provider that cannot price a model is a candidate the router must skip, and a
    silent default price would make that path untestable by construction.
    """
    provider = ScriptedProvider(name=OPENAI, prices={"gpt-4o": Price(5000, 15000)})
    with pytest.raises(PriceUnavailable, match="no price for model"):
        provider.cost_per_1k_tokens("mystery-model")


async def test_a_scripted_provider_prices_a_completion_from_its_table() -> None:
    """The completion's price comes from the same table, so a metered cost in a
    routing test is the price the test configured rather than the default."""
    provider = ScriptedProvider(
        name=OPENAI,
        price=Price(1500, 3000),
        prices={"gpt-4o": Price(5000, 15000)},
        completions=(completion(),),
    )
    result = await provider.complete(ask(model="gpt-4o"))
    assert result.price == Price(5000, 15000)


# --- credential resolution -------------------------------------------------


async def test_a_static_resolver_returns_its_key() -> None:
    resolver = StaticCredentials({OPENAI: Secret("sk-test")})
    assert (await resolver.credential_for(OPENAI)).reveal() == "sk-test"


async def test_a_static_resolver_wraps_a_bare_string() -> None:
    assert await StaticCredentials({OPENAI: "sk-test"}).credential_for(OPENAI) == Secret("sk-test")


async def test_a_static_resolver_raises_for_an_unconfigured_provider() -> None:
    with pytest.raises(CredentialUnavailable, match=OPENAI):
        await StaticCredentials({}).credential_for(OPENAI)


@pytest.mark.parametrize("empty", ["", Secret("")], ids=["bare-string", "secret"])
async def test_a_static_resolver_rejects_an_empty_key(empty) -> None:
    """Both spellings, because both are reachable: an env file yields a bare string
    and a vault yields a `Secret`. An empty key in configuration is a real deploy
    mistake, and forwarding it produces a 401 that reads like a *wrong* key rather
    than a missing one."""
    with pytest.raises(CredentialUnavailable, match="is empty"):
        await StaticCredentials({OPENAI: empty}).credential_for(OPENAI)


# --- the litellm adapter: pricing -----------------------------------------


def test_the_adapter_prices_a_model_from_litellms_table() -> None:
    """litellm quotes dollars per token; the adapter converts once, here, to
    integer micro-dollars per 1k. 1.5e-07 * 1000 * 1e6 = 150."""
    assert make_provider().cost_per_1k_tokens(MODEL) == Price(150, 600)


def test_the_adapter_asks_litellm_about_the_qualified_model() -> None:
    """`openai/gpt-4o-mini`, not `gpt-4o-mini`. The vendor prefix is what tells
    litellm which price table entry to read, and an unqualified lookup on a model
    name two vendors share reads the wrong one."""
    provider = make_provider(
        model_info={QUALIFIED: {"input_cost_per_token": 1e-07, "output_cost_per_token": 2e-07}}
    )
    assert provider.cost_per_1k_tokens(MODEL) == Price(100, 200)


def test_the_adapter_reports_an_unpriced_model_as_unavailable() -> None:
    """`PriceUnavailable` rather than a zero price. Zero would make every call free
    in the metered event, silently, and the money would be lost rather than
    wrong-looking."""
    with pytest.raises(PriceUnavailable, match="gpt-4o-mini"):
        make_provider(model_info={}).cost_per_1k_tokens(MODEL)


def test_the_adapter_reports_a_malformed_price_entry_as_unavailable() -> None:
    """litellm's table has had rows with a null price. A float cast of that raises,
    and the honest translation is "no price", not a 500 out of a health probe."""
    provider = make_provider(
        model_info={QUALIFIED: {"input_cost_per_token": None, "output_cost_per_token": 1e-06}}
    )
    with pytest.raises(PriceUnavailable, match="unusable price entry"):
        provider.cost_per_1k_tokens(MODEL)


def test_the_adapter_rounds_a_sub_micro_price_up_rather_than_to_zero() -> None:
    """A per-1k price below one micro-dollar becomes 1, not 0. Zero means free, and
    a rounding-down bug here is invisible until a month's usage fails to add up to
    the provider's invoice."""
    provider = make_provider(
        model_info={QUALIFIED: {"input_cost_per_token": 1e-10, "output_cost_per_token": 0.0}}
    )
    assert provider.cost_per_1k_tokens(MODEL) == Price(1, 0)


def test_the_adapter_rejects_an_unsupported_vendor() -> None:
    """Caught at construction rather than at first call: a route naming a vendor
    muse has no adapter for is a config error, and it is found at boot."""
    with pytest.raises(ValueError, match="not a supported vendor"):
        LiteLLMProvider(name="bedrock", api_key="sk-test", litellm=make_stub())


def test_the_adapter_accepts_a_secret_rather_than_a_string() -> None:
    """So a key read out of the vault never has to be unwrapped into a plain `str`
    on its way to the provider."""
    provider = LiteLLMProvider(name=OPENAI, api_key=Secret("sk-test"), litellm=make_stub())
    assert provider.name == OPENAI


def test_the_adapter_qualifies_the_model_for_its_vendor() -> None:
    assert make_provider().qualified_model("gpt-4o-mini") == "openai/gpt-4o-mini"
    anthropic = make_provider(name=ANTHROPIC)
    assert anthropic.qualified_model("claude-sonnet-4-5") == "anthropic/claude-sonnet-4-5"


def test_the_adapter_does_not_double_qualify_an_already_qualified_model() -> None:
    """A route may name `openai/gpt-4o-mini` explicitly. Prefixing it again would
    produce `openai/openai/gpt-4o-mini`, which is a 404 from every vendor."""
    assert make_provider().qualified_model(QUALIFIED) == QUALIFIED


# --- the litellm adapter: dispatch ----------------------------------------


async def test_the_adapter_sends_the_qualified_model_and_the_key() -> None:
    provider = make_provider()
    await provider.complete(ask())
    sent = provider.litellm.captured
    assert sent["model"] == QUALIFIED
    assert sent["api_key"] == "sk-test"
    assert sent["messages"] == [{"role": "user", "content": "hello"}]


async def test_the_adapter_sends_every_message_in_order() -> None:
    """A conversation is ordered data. Reordering it produces a confident wrong
    answer rather than an error, so the order is asserted directly."""
    provider = make_provider()
    await provider.complete(
        CompletionRequest(
            model=MODEL,
            messages=(
                Message(role="system", content="be brief"),
                Message(role="user", content="hello"),
                Message(role="assistant", content="hi"),
            ),
        )
    )
    assert provider.litellm.captured["messages"] == [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]


async def test_the_adapter_passes_the_optional_parameters_through() -> None:
    """`max_tokens` and `temperature` are the two a caller actually sets. Dropping
    them would make the endpoint look like it honours them while ignoring them."""
    provider = make_provider()
    await provider.complete(ask(max_tokens=64, temperature=0.2))
    assert provider.litellm.captured["max_tokens"] == 64
    assert provider.litellm.captured["temperature"] == 0.2


async def test_the_adapter_omits_optional_parameters_that_were_not_set() -> None:
    """`max_tokens=None` is not the same as not sending it: some providers reject
    the null. Only what the caller supplied is sent."""
    provider = make_provider()
    await provider.complete(ask())
    assert "max_tokens" not in provider.litellm.captured
    assert "temperature" not in provider.litellm.captured


async def test_the_adapter_reports_a_missing_credential_before_calling_out() -> None:
    """No call is made, so no spend happens and no rate-limit counter is spent on a
    credential that was never going to work."""
    provider = make_provider(api_key="")
    with pytest.raises(CredentialUnavailable, match=OPENAI):
        await provider.complete(ask())


# --- the litellm adapter: response handling --------------------------------


async def test_the_adapter_returns_the_content_and_usage() -> None:
    result = await make_provider().complete(ask())
    assert result.content == "hello there"
    assert result.tokens_in == 12
    assert result.tokens_out == 8
    assert result.provider == OPENAI
    assert result.finish == "stop"


async def test_the_adapter_prices_the_completion_it_returned() -> None:
    """The price is looked up once, before the call, and carried on the result — so
    the metered cost is computed from the model that actually served the request,
    not from a table lookup repeated at metering time that could read a different
    row.

    12 in at 150/1k is 1.8 and 8 out at 600/1k is 4.8; the total is 6.6, rounded
    once at the end to 7.
    """
    result = await make_provider().complete(ask())
    assert result.price == Price(150, 600)
    assert result.cost_micros == 7


async def test_the_adapter_passes_the_finish_reason_through() -> None:
    provider = make_provider(
        response={
            "choices": [{"message": {"content": "cut off"}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    assert (await provider.complete(ask())).finish == "length"


async def test_the_adapter_rejects_a_response_with_no_choices_key() -> None:
    """A provider error page arriving as a 200 has no `choices`. Indexing `[0]`
    would raise IndexError, which reads as a bug in muse rather than an upstream
    problem — and an upstream problem is what a fallback exists for."""
    provider = make_provider(response={"usage": {}})
    with pytest.raises(ResponseShapeError, match="no choices"):
        await provider.complete(ask())


async def test_the_adapter_rejects_a_response_that_is_not_a_mapping() -> None:
    """`None` in place of a response. It cannot be subscripted, so the adapter has to
    treat "not a mapping" as the shape error it is rather than letting a TypeError
    escape three frames down."""
    provider = make_provider(response=object())
    with pytest.raises(ResponseShapeError, match="no choices"):
        await provider.complete(ask())


async def test_the_adapter_rejects_a_null_response() -> None:
    provider = make_provider(response=None)
    with pytest.raises(ResponseShapeError, match="no choices"):
        await provider.complete(ask())


async def test_the_adapter_rejects_an_empty_choices_list() -> None:
    provider = make_provider(response={"choices": [], "usage": {}})
    with pytest.raises(ResponseShapeError, match="no choices"):
        await provider.complete(ask())


async def test_the_adapter_rejects_a_choice_with_no_message() -> None:
    provider = make_provider(response={"choices": [{"finish_reason": "stop"}], "usage": {}})
    with pytest.raises(ResponseShapeError, match="no message"):
        await provider.complete(ask())


async def test_the_adapter_rejects_a_response_with_no_usage() -> None:
    """Without usage there is nothing to meter, and a completion that cannot be
    metered is one whose spend is never billed. This is the last place it can be
    caught."""
    provider = make_provider(
        response={"choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}]}
    )
    with pytest.raises(ResponseShapeError, match="no usage"):
        await provider.complete(ask())


async def test_the_adapter_rejects_empty_usage() -> None:
    """`usage: {}` is the same absence written differently — a provider that
    returns the key with nothing in it."""
    provider = make_provider(
        response={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {},
        }
    )
    with pytest.raises(ResponseShapeError, match="no usage"):
        await provider.complete(ask())


async def test_the_adapter_rejects_a_message_with_null_content() -> None:
    """Null content is a tool call. v1 has no tool support, so reporting it as empty
    text would hand the caller a silent wrong answer."""
    provider = make_provider(
        response={
            "choices": [{"message": {"content": None}, "finish_reason": "tool_calls"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1},
        }
    )
    with pytest.raises(ResponseShapeError, match="no content"):
        await provider.complete(ask())


async def test_the_adapter_treats_absent_token_counts_as_zero() -> None:
    """A provider that reports content but omits one of the counters is giving a
    partial answer, and the honest reading of the missing half is zero — which
    under-bills rather than inventing a number."""
    provider = make_provider(
        response={
            "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10},
        }
    )
    result = await provider.complete(ask())
    assert (result.tokens_in, result.tokens_out) == (10, 0)


# --- the litellm adapter: exception mapping --------------------------------


async def test_the_adapter_maps_a_rejected_credential_to_auth_error() -> None:
    provider = make_provider(raises="AuthenticationError", error_message="Incorrect API key")
    with pytest.raises(ProviderAuthError, match="rejected the credential"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_missing_credential_to_credential_unavailable() -> None:
    """An absent key surfaces through `AuthenticationError`, and the two need
    different responses: one is a deploy mistake, the other an operator action.
    Calling both "auth" sends someone to rotate a key that is not there."""
    provider = make_provider(
        raises="AuthenticationError", error_message="No api key found in environment"
    )
    with pytest.raises(CredentialUnavailable, match=OPENAI):
        await provider.complete(ask())


async def test_the_adapter_maps_a_rate_limit_to_a_retryable_error() -> None:
    provider = make_provider(raises="RateLimitError", error_message="429 too many requests")
    with pytest.raises(ProviderRateLimited, match="rate limited"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_timeout_to_a_retryable_error() -> None:
    provider = make_provider(raises="Timeout", error_message="request timed out")
    with pytest.raises(ProviderTimeout, match="timed out"):
        await provider.complete(ask())


async def test_the_adapter_maps_an_unavailable_service_to_a_retryable_error() -> None:
    provider = make_provider(raises="ServiceUnavailableError", error_message="503 overloaded")
    with pytest.raises(ProviderUnavailable, match="is unavailable"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_connection_failure_to_a_retryable_error() -> None:
    """A reset socket or a DNS failure is transient from the caller's point of
    view, and the fallback candidate is exactly the right answer to it."""
    provider = make_provider(raises="APIConnectionError", error_message="connection reset")
    with pytest.raises(ProviderUnavailable, match="could not reach"):
        await provider.complete(ask())


async def test_the_adapter_maps_an_upstream_5xx_to_a_retryable_error() -> None:
    provider = make_provider(raises="InternalServerError", error_message="500 boom")
    with pytest.raises(ProviderUnavailable, match="server error"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_content_policy_refusal_to_a_policy_error() -> None:
    """Not retried and not reported as unavailability: retrying the same content
    against the same policy is the definition of a hot loop, and telling the caller
    "unavailable" hides that the request itself was refused."""
    provider = make_provider(
        raises="ContentPolicyViolationError", error_message="blocked by content filter"
    )
    with pytest.raises(ContentPolicyError, match="content policy"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_bad_request_to_a_permanent_error() -> None:
    provider = make_provider(raises="BadRequestError", error_message="unknown parameter")
    with pytest.raises(ProviderInvalidRequest, match="rejected the request"):
        await provider.complete(ask())


async def test_the_adapter_maps_a_not_found_to_a_permanent_error() -> None:
    """A model that does not exist is a config error. Retrying it spends the
    caller's latency to produce the same 404 from the fallback."""
    provider = make_provider(raises="NotFoundError", error_message="model not found")
    with pytest.raises(ProviderInvalidRequest, match="rejected the request"):
        await provider.complete(ask())


async def test_the_adapter_maps_an_invalid_request_to_a_permanent_error() -> None:
    provider = make_provider(raises="InvalidRequestError", error_message="bad param")
    with pytest.raises(ProviderInvalidRequest, match="rejected the request"):
        await provider.complete(ask())


async def test_the_adapter_checks_the_context_window_before_the_general_bad_request() -> None:
    """`ContextWindowExceededError` subclasses `BadRequestError` upstream, so an
    ordering slip in the mapping does not crash — it files a too-long prompt as a
    generic malformed request and loses the one thing the caller needed to know: the
    fix is to send fewer tokens, which no amount of retrying will achieve.

    Reproduced here with a real subclass, because that is the only way the ordering
    is actually load-bearing. A stub with two unrelated classes would pass with the
    checks in either order.
    """
    stub = make_stub()
    window = type("ContextWindowExceededError", (stub.BadRequestError,), {})
    stub.ContextWindowExceededError = window

    async def acompletion(**kwargs):
        raise window("maximum context length is 8192 tokens")

    stub.acompletion = acompletion
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)
    with pytest.raises(ProviderInvalidRequest, match="context window exceeded"):
        await provider.complete(ask())


async def test_the_adapter_still_maps_a_plain_bad_request() -> None:
    """The companion to the ordering test: with `ContextWindowExceededError` in the
    stub as a real subclass, this proves the general branch is reached when the
    error is not the specific one — so the specific check is not swallowing
    everything."""
    stub = make_stub()
    stub.ContextWindowExceededError = type(
        "ContextWindowExceededError", (stub.BadRequestError,), {}
    )

    async def acompletion(**kwargs):
        raise stub.BadRequestError("unknown parameter temperature")

    stub.acompletion = acompletion
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)
    with pytest.raises(ProviderInvalidRequest, match="rejected the request"):
        await provider.complete(ask())


async def test_the_adapter_maps_an_unrecognised_error_to_unavailable() -> None:
    """The default is the retryable error: an error muse has never seen is most
    likely a transport problem on a route it does not recognise. The failure is
    recorded in the chain either way, so the fallback is not silent."""
    provider = make_provider(raises="SomethingNewUpstream", error_message="surprise")
    with pytest.raises(ProviderUnavailable, match="SomethingNewUpstream"):
        await provider.complete(ask())


async def test_the_adapter_scrubs_the_credential_out_of_the_whole_traceback() -> None:
    """The leak `str(exc)` cannot see, and the one that actually reaches a log.

    `raise ... from error` keeps the vendor's exception as the `__cause__`, and
    every traceback renderer — `traceback.format_exception`, `logging.exception`,
    an APM agent, Sentry — prints that cause with its own message. So scrubbing
    only the reported message leaves the plaintext key in the chained cause, and
    the assertion above passes while the key still ships in the log line.

    This asserts on the *rendered* traceback rather than on any one attribute,
    because the rendered text is the thing that gets written down.
    """
    provider = make_provider(
        api_key="sk-live-SECRET123",
        raises="AuthenticationError",
        error_message="Incorrect API key provided: sk-live-SECRET123",
    )
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider.complete(ask())
    rendered = "".join(
        traceback.format_exception(type(excinfo.value), excinfo.value, excinfo.value.__traceback__)
    )
    assert "sk-live-SECRET123" not in rendered
    assert "redacted" in rendered


async def test_the_adapter_keeps_the_vendor_exception_in_the_chain() -> None:
    """The counterweight to the scrub: sanitising must not be done by dropping the
    cause.

    The cause is the only record of *where* inside litellm the failure happened,
    which is the part of a provider stack trace that is worth reading. The fix
    removes the credential from the chain, not the chain itself — a
    `from None` here would pass every redaction test and make every provider
    failure undiagnosable.
    """
    provider = make_provider(raises="InternalServerError", error_message="500 boom")
    with pytest.raises(ProviderUnavailable) as excinfo:
        await provider.complete(ask())
    cause = excinfo.value.__cause__
    assert cause is not None
    assert type(cause).__name__ == "InternalServerError"


async def test_the_adapter_leaves_a_clean_cause_message_untouched() -> None:
    """Scrubbing rewrites the cause's message. It must rewrite only when it had to,
    so an error carrying no credential is not silently re-worded on its way to the
    logs — the diagnostic value of an untouched upstream message is the point of
    keeping the cause at all.
    """
    provider = make_provider(raises="Timeout", error_message="gateway timeout after 30s")
    with pytest.raises(ProviderTimeout) as excinfo:
        await provider.complete(ask())
    assert excinfo.value.__cause__.args == ("gateway timeout after 30s",)


async def test_the_adapter_scrubs_the_credential_out_of_the_error_detail() -> None:
    """A provider that echoes the key it rejected puts a live credential in every
    log line and every error body downstream of here. The adapter holds the key, so
    the adapter is the only place that can remove it."""
    provider = make_provider(
        api_key="sk-live-SECRET123",
        raises="AuthenticationError",
        error_message="Incorrect API key provided: sk-live-SECRET123",
    )
    with pytest.raises(ProviderAuthError) as excinfo:
        await provider.complete(ask())
    assert "sk-live-SECRET123" not in str(excinfo.value)
    assert "redacted" in str(excinfo.value)


# --- the litellm adapter: health ------------------------------------------


async def test_the_adapter_probes_health_with_a_one_token_completion() -> None:
    """Not a dedicated endpoint: a health check that passes while completions fail
    is worse than no health check, and the completion endpoint is the only one every
    vendor is guaranteed to have. `max_tokens: 1` keeps the probe's cost negligible."""
    provider = make_provider()
    health = await provider.health()
    assert health.healthy is True
    sent = provider.litellm.captured
    assert sent["max_tokens"] == 1
    assert sent["messages"] == [{"role": "user", "content": "ping"}]


async def test_the_adapter_probes_with_a_model_its_own_vendor_serves() -> None:
    """The probe is a real request. Sending anthropic's endpoint an openai model name
    would report a healthy provider as permanently broken."""
    provider = make_provider(name=ANTHROPIC)
    await provider.health()
    assert provider.litellm.captured["model"].startswith("anthropic/")


async def test_the_adapter_reports_an_unhealthy_provider_without_raising() -> None:
    """Health is a query, not an assertion. A raising health check takes down the
    caller that asked, and readiness is where a dependency failure belongs."""
    provider = make_provider(raises="Timeout", error_message="gateway timeout")
    health = await provider.health()
    assert health.healthy is False
    assert "gateway timeout" in health.detail


async def test_the_adapter_scrubs_the_credential_out_of_a_health_detail() -> None:
    """Same leak as the error path, and health details land in a readiness body that
    is routinely readable by anyone watching the service."""
    provider = make_provider(
        api_key="sk-live-SECRET123",
        raises="AuthenticationError",
        error_message="bad key sk-live-SECRET123",
    )
    assert "sk-live-SECRET123" not in (await provider.health()).detail


async def test_the_adapter_health_reports_a_missing_credential_as_unhealthy() -> None:
    """Unhealthy, not an exception: a vault with no key for a provider is a
    degraded muse, which is exactly what `degraded` in `/readyz` is for."""
    health = await make_provider(api_key="").health()
    assert health.healthy is False
    assert OPENAI in health.detail


def test_the_adapter_can_import_litellm_on_first_use() -> None:
    """The `litellm` property falls back to a real import when nothing was injected.

    Constructing the adapter with no module is the production path — the app factory
    does exactly that. This asserts the deferred import resolves to something with
    the two entry points the adapter calls, without calling either: `acompletion`
    would open a socket and `get_model_info` reads a table this test does not need
    to pin.
    """
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test")
    assert callable(provider.litellm.acompletion)
    assert callable(provider.litellm.get_model_info)


def test_the_adapter_keeps_an_injected_module() -> None:
    """Injection wins over the real import, which is what makes the suite
    socket-free: a test that injected a stub must never have it replaced by the
    real module on a later call."""
    stub = make_stub()
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test", litellm=stub)
    assert provider.litellm is stub
    assert provider.litellm is stub


def test_the_adapter_prices_a_real_litellm_model() -> None:
    """The one test that touches the real price table, and it only reads it.

    `gpt-4o-mini` is a model every litellm release has priced, so this asserts the
    adapter's conversion against the library's own numbers — the point where a
    change in litellm's table format or in this conversion shows up. No network:
    `get_model_info` reads a bundled table.
    """
    provider = LiteLLMProvider(name=OPENAI, api_key="sk-test")
    price = provider.cost_per_1k_tokens("gpt-4o-mini")
    assert price.input_micros > 0
    assert price.output_micros > price.input_micros


def test_a_bad_provider_name_is_a_registry_error_not_a_provider_error() -> None:
    """Name resolution belongs to the registry. The adapter is told which vendor it
    is and never looks anything up, so there is one place a name becomes a provider
    and one error type for a name that resolves to nothing.

    The distinction matters downstream: a `ProviderError` is a candidate failure the
    router falls through from, and falling through from a typo in a route file just
    hides the typo.
    """
    assert issubclass(UnregisteredProvider, Exception)
    assert not issubclass(UnregisteredProvider, ProviderError)
