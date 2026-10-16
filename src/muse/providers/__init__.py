"""Providers: the protocol muse routes through, and the three implementations.

`Provider` is a structural protocol with exactly four operations:

- `complete(request)` — one chat completion.
- `decide(request)` — one decision over a list of questions.
- `health()` — can this provider serve a request right now.
- `cost_per_1k_tokens(model)` — what a thousand tokens of this model cost.

Three implementations ship:

- `LiteLLMProvider` — openai and anthropic, via litellm's Python API. This is the
  embedding described in PLAN §2b: the library muse embeds, not a port of.
- `FakeProvider` — a deterministic provider for tests and for the compose stack.
  It lives in `src/`, not in `tests/`, because the router's own tests and any
  later contract test need a provider, and a double that only exists inside one
  test directory cannot be reused.
- `ScriptedProvider` — a `FakeProvider` whose next result is chosen by the test.

Money is integer micro-dollars per 1k tokens, everywhere, including here. The one
place a float appears is `LiteLLMProvider.cost_per_1k_tokens`, which converts
litellm's per-token dollars once, on the way in.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from muse.errors import (
    ContentPolicyError,
    CredentialUnavailable,
    PriceUnavailable,
    ProviderAuthError,
    ProviderIndeterminate,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
)

#: The credentials surface, re-exported from `muse.providers.credentials` so a caller
#: that only wants to hand a provider a dict of keys does not have to know which module
#: the protocol lives in. Re-exported rather than redefined: a second copy of a class is
#: a second thing for a test to cover that no request path ever calls — which is exactly
#: how the static resolver ended up "tested" against a copy production never used.
from muse.providers.credentials import CredentialResolver
from muse.providers.credentials import StaticCredentials as StaticCredentials
from muse.providers.credentials import VaultCredentials as VaultCredentials
from muse.redaction import Secret, redact
from muse.telemetry import TRACEPARENT_HEADER, outbound_traceparent

#: The roles a message may have. OpenAI's set, which is a superset of what the
#: other vendors need; the adapter maps them if a vendor disagrees.
ROLES = ("system", "user", "assistant")

#: litellm's model-name prefix per vendor. Without it litellm infers the vendor
#: from the model name, which is how a request meant for one vendor gets billed to
#: another.
#: The vendors that serve OTHER vendors' models, priced by the model
#: underneath rather than by their own spelling. litellm's cost map is keyed by
#: the vendor that owns the model, so `openrouter/openai/gpt-4o-mini` is looked
#: up as `openai/gpt-4o-mini`.
AGGREGATORS = frozenset({"openrouter"})

VENDOR_PREFIXES = {
    "openai": "openai",
    "anthropic": "anthropic",
    # An aggregator, and the reason it is here rather than an afterthought: it
    # serves both vendors' models behind one credential, which is what a
    # developer without an OpenAI key and an Anthropic key both actually has.
    # `openrouter/openai/gpt-4o-mini` is litellm's spelling, so the prefix is
    # the vendor's own name and the model keeps its vendor-qualified form.
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    # An OpenAI-compatible gateway in front of DeepSeek. The prefix is
    # `openai` because that is the wire format its `/chat/completions` speaks,
    # and the base URL (VENDOR_API_BASES) is what stops the request going to
    # OpenAI's endpoint with a key opencode issued.
    "opencode": "openai",
    # ABSENT on purpose, and its absence is the statement: there is no
    # `typesafe/` prefix, because typesafe is not an OpenAI-shaped completion
    # vendor at all. Its endpoint is a decisions endpoint, its models are
    # decision models (`jev-latest`), and prefixing it would ask litellm to
    # call a completions path that does not exist for it.
}

#: The vendors that serve no completions, only decisions. `qualified_model`
#: leaves their model name alone, and cost_per_1k_tokens prices them from the
#: decision's own reported usage rather than from litellm's completion price
#: table — which has no entry for a decision model because it is not one.
DECISIONS_VENDORS = frozenset({"typesafe"})

#: The base URL a vendor is reached at, where it is not litellm's default for
#: the model's vendor prefix. opencode is an OpenAI-compatible gateway in front
#: of DeepSeek, and without this the request would go to OpenAI's endpoint with
#: a key opencode issued — a 401 that reads as a wrong key rather than a
#: missing base.
VENDOR_API_BASES = {
    "opencode": "https://opencode.ai/zen/go/v1",
}

#: The cheapest model each vendor will serve, used as the health probe. Per vendor
#: because a probe is a real request: sending anthropic's endpoint an openai model
#: name would report a healthy provider as broken.
PROBE_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
    # The cheapest model each will serve. A probe is a real request, so naming
    # openai's model here would report a healthy aggregator as broken.
    "openrouter": "openai/gpt-4o-mini",
    "deepseek": "deepseek-chat",
    # The house model behind the gateway. Spelled the way the gateway spells
    # it, which is not the way DeepSeek's own API spells it.
    "opencode": "deepseek-v4.1-flash",
}

#: Micro-dollars per dollar.
PER_DOLLAR = 1_000_000


def _read(container: Any, field: str) -> Any:
    """One field of a litellm response, whether it arrived as a mapping or an object.

    **litellm returns `ModelResponse` and friends — pydantic models, not dicts.** The
    adapter read `response["choices"][0].get("message")`, which works only for the dict
    form, and the dict form is what `tests/support/litellm_stub.py` hands it. So the
    whole of the adapter's shape handling was verified against a shape the real library
    does not produce: against a live provider every completion raised
    `ResponseShapeError`, which reads as an upstream fault and advances the fallback
    chain. The observable symptom was "every candidate failed", on every request, on
    every vendor.

    One helper rather than an `isinstance` check in each of the four places, because the
    shapes are not uniform: `ModelResponse` supports `response["choices"]`, so
    subscripting alone works for the response and fails for `usage` and for the choice,
    and a fix applied at one level would have looked right and been wrong at the next.

    `None` for absent rather than a raise, because every absence here is a
    `ResponseShapeError` naming a field and the caller decides what it means.
    """
    if isinstance(container, dict):
        return container.get(field)
    return getattr(container, field, None)


def _items(container: Any) -> list[tuple[str, Any]]:
    """The `(key, value)` pairs of a mapping answer set, whatever carried it.

    litellm's `DecisionsResponse.answers` is a `Mapping`, and a pydantic model renders
    it the same way — but a provider adapter that received it as an object with `.items`
    rather than as a dict needs the same normalisation `_read` gives one field. One
    helper here for the same reason `_read` exists at all: the shape litellm returns is
    not the shape the stub returns, and the adapter was burned by assuming it was.
    """
    if isinstance(container, dict):
        return list(container.items())

    # A SEQUENCE is the other shape an answer set arrives in, and it is the
    # OpenAI-flavoured dialect's: `OpenAIDecisionResponse.answers` is a tuple of
    # answer objects, each carrying the name it was asked under. Falling back
    # to `dict().items` for it yields nothing at all — so every answer is
    # dropped, no error is raised, and the caller receives `answers: {}` for a
    # decision that was answered. A read that silently returns nothing is the
    # worst shape a helper can have.
    if isinstance(container, Sequence):
        return [(str(_read(answer, "name")), answer) for answer in container]

    return [
        (str(name), value) for name, value in getattr(container, "items", dict().items)()
    ]


def _answer_fields(answer: Any) -> dict[str, Any]:
    """One answer as a plain mapping, from whatever litellm rendered it as.

    Verbatim pass-through of litellm's own answer object: the response is litellm's
    vocabulary, and re-declaring it here would be a second model for a litellm upgrade
    to disagree with.
    """
    if isinstance(answer, dict):
        return dict(answer)
    return dict(getattr(answer, "__dict__", {}))



@dataclass(frozen=True, slots=True)
class Price:
    """Micro-dollars per 1k tokens, input and output.

    A `float` here would put a rounding difference between two machines into
    every invoice. Integers cost a rounding rule, which is written down and
    tested; a float costs a reconciliation ticket.
    """

    input_micros: int
    output_micros: int

    def __post_init__(self) -> None:
        if self.input_micros < 0:
            raise ValueError(f"input_micros cannot be negative: {self.input_micros}")
        if self.output_micros < 0:
            raise ValueError(f"output_micros cannot be negative: {self.output_micros}")


@dataclass(frozen=True, slots=True)
class Message:
    """One message in a conversation.

    Validated here rather than at the provider, so an invalid request is rejected
    once, identically, for every vendor — rather than becoming a 400 from openai
    and a differently-worded 400 from anthropic.
    """

    role: str
    content: str

    def __post_init__(self) -> None:
        if self.role not in ROLES:
            raise ValueError(f"role must be one of {ROLES}, got {self.role!r}")
        if not self.content:
            raise ValueError("content must not be empty")

    def as_wire(self) -> dict[str, str]:
        """The `{role, content}` dict every vendor's API expects."""
        return {"role": self.role, "content": self.content}


@dataclass(frozen=True, slots=True)
class CompletionRequest:
    """A request for one completion, vendor-neutral.

    `model` is the *client-facing* model name from `config/routes.yaml`, not a
    vendor model id: which vendor serves it is the router's decision, and letting
    the caller name a vendor would defeat the fallback chain entirely.
    """

    model: str
    messages: tuple[Message, ...]
    max_tokens: int | None = None
    #: A float, and the LAST float. `temperature` is a decimal string on the wire
    #: (`openapi/v1.yaml`, because Kubernetes' API conventions refuse floats at
    #: `api-conventions.md:603`) and becomes a float exactly once, in
    #: `api.RouteRequestBody.sampling_temperature()`. Everything downstream of
    #: here is the provider seam, and this type is vendor-neutral rather than a
    #: wire type, so it carries the value the way LiteLLM wants it. See
    #: `complete()` below for the one call that hands it over.
    temperature: float | None = None

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("model must not be blank")
        if not self.messages:
            raise ValueError("a completion request needs at least one message")
        if self.max_tokens is not None and self.max_tokens < 1:
            raise ValueError("max_tokens must be positive when set")


@dataclass(frozen=True, slots=True)
class Completion:
    """One completion, with the usage and price needed to meter it.

    `provider` is on the result rather than looked up afterwards because it is the
    fact the metered event records: attributing a call to the wrong vendor is the
    one error in this service that neither the caller nor the person billed can
    detect.
    """

    provider: str
    model: str
    content: str
    tokens_in: int
    tokens_out: int
    price: Price
    finish_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.provider:
            # The metered event records this exact string, so an empty one would
            # produce an event no consumer can attribute to a vendor.
            raise ValueError("a completion must name the provider that served it")

    @property
    def cost_micros(self) -> int:
        """What this completion cost, in micro-dollars."""
        return usage_cost_micros(self.tokens_in, self.tokens_out, self.price)

    @property
    def finish(self) -> str:
        """`finish_reason`, defaulting to `stop`.

        A provider that omits it is telling us it finished normally; the default of
        `unknown` would make every conforming response look anomalous in a log. The
        distinction that matters to a caller is `stop` ("the model finished") versus
        `length` ("the model was cut off"), and that one is never defaulted away.
        """
        return self.finish_reason or "stop"


@dataclass(frozen=True, slots=True)
class Health:
    """Whether a provider can serve a request right now."""

    healthy: bool
    detail: str = ""

    def __post_init__(self) -> None:
        if not self.detail:
            object.__setattr__(self, "detail", "ok" if self.healthy else "unhealthy")


#: The largest number of questions one decision request may carry. litellm's
#: `MAX_DECISION_QUESTIONS`, and asserted through it rather than restated here.
MAX_DECISION_QUESTIONS = 128


@dataclass(frozen=True, slots=True)
class DecisionsQuestion:
    """One question, as the caller stated it — and where it became litellm's.

    Kept vendor-neutral and *thin*: `instructions` and `criteria` are `object`,
    because what makes a criterion true is the model's business and litellm already
    validates it. A pydantic model here would be a second validator agreeing with
    litellm's, and a second validator is one that drifts — so muse hands litellm
    the question and surfaces its own refusal (rule: surface litellm's error rather
    than reimplementing it).

    `name` is the key litellm answers under, and muse assigns it rather than the
    caller: answers come back in the order asked, and a caller-supplied key is a
    collision two questions can have silently.
    """

    name: str
    type: str
    instructions: object | None = None
    criteria: object | None = None
    # The OpenAI dialect's own options fields, carried through untouched.
    # `choices` for a `choice` and `levels` for a `score`, sent exactly as the
    # caller stated them — litellm validates them, and a second copy of that
    # validation here would be a second thing to drift.
    choices: object | None = None
    levels: object | None = None

    def as_wire(self) -> dict[str, Any]:
        """The question as litellm's OpenAI-flavoured decisions endpoint takes it.

        The name is IN the dict rather than being the key a map is built under,
        because `litellm.adecisions` takes questions as a SEQUENCE of named
        questions on that path — a caller following litellm's docs reads the
        same shape. The map form litellm also accepts is its deprecated
        `systemone` spelling, and it emits a UserWarning naming the newer one.

        `predicate` is not folded onto `noul` here either: `predicate` IS the
        OpenAI dialect's spelling of the same yes/no question, so it goes
        through as itself.
        """
        question: dict[str, Any] = {
            "name": self.name,
            "type": self.type,
        }
        # `exclude_none` semantics mirroring litellm's own model dump: a question
        # may legitimately state neither field (a `choice` whose criteria *is* the
        # label list), and sending an explicit null is what a strict provider
        # rejects with a 400 about the wrong field.
        if self.instructions is not None:
            question["instructions"] = self.instructions
        if self.criteria is not None:
            question["criteria"] = self.criteria
        if self.choices is not None:
            question["choices"] = self.choices
        if self.levels is not None:
            question["levels"] = self.levels
        return question


@dataclass(frozen=True, slots=True)
class DecisionsRequest:
    """A request for one decision, vendor-neutral.

    `input` is the thing being judged and `questions` are asked of it in order;
    `model` is the client-facing name from `config/routes.yaml`, exactly as on
    `CompletionRequest` — which vendor answers is the router's decision.
    """

    model: str
    input: str
    questions: tuple[DecisionsQuestion, ...]


@dataclass(frozen=True, slots=True)
class Decision:
    """One served decision: every answer, plus the usage the whole call cost.

    The answers are litellm's own render, keyed by the name muse assigned the
    question — `q1`, `q2`, ... in the order asked — and passed through verbatim. A
    second copy of a litellm answer is a second thing for a litellm upgrade to
    disagree with, and the consumer is written against litellm's docs.

    `tokens_in`/`tokens_out` are on the call rather than on each answer because
    there is one usage for N answers; a caller reading answer 3 meters the whole
    call, which is what it paid for.

    `price` is here for the same reason it is on `Completion`: the router resolves
    it before dispatch and the meter reads it off the result rather than looking a
    second time at a table that may have moved in between.
    """

    provider: str
    model: str
    answers: Mapping[str, Any]
    tokens_in: int
    tokens_out: int
    price: Price

    def as_wire(self) -> dict[str, Any]:
        """The answers as litellm rendered them, with none of muse's additions."""
        return dict(self.answers)

    @property
    def cost_micros(self) -> int:
        """What this call cost, in micro-dollars."""
        return usage_cost_micros(self.tokens_in, self.tokens_out, self.price)


def usage_cost_micros(tokens_in: int, tokens_out: int, price: Price) -> int:
    """The cost of a completion, in micro-dollars.

    Integer division per component, a single rounding at the end. Rounding each
    component first and summing is how a sub-micro discrepancy becomes a visible
    one after a month of calls, and which direction it rounds is not something to
    leave to chance: a customer's usage is never rounded down.
    """
    if tokens_in < 0:
        raise ValueError(f"tokens_in cannot be negative: {tokens_in}")
    if tokens_out < 0:
        raise ValueError(f"tokens_out cannot be negative: {tokens_out}")
    total = tokens_in * price.input_micros + tokens_out * price.output_micros
    return (total + 500) // 1000


@runtime_checkable
class Provider(Protocol):
    """What the router needs from a model vendor.

    A protocol, not a base class: an adapter is anything with these four methods,
    so a future vendor does not have to inherit from anything muse owns, and the
    router cannot reach a provider's internals by accident.
    """

    name: str

    async def complete(self, request: CompletionRequest) -> Completion:
        """One chat completion, or raise a `ProviderError` subclass."""
        ...

    async def decide(self, request: DecisionsRequest) -> Decision:
        """One decision, or raise a `ProviderError` subclass.

        Same contract as `complete` and for the same reason: the router's whole
        resilience layer — the breaker, the retry budget, the fallback chain — is
        written once against the error taxonomy, so an operation that raised
        anything else would be one that bypassed all three.
        """
        ...

    async def health(self) -> Health:
        """Whether this provider can serve a request now. Never raises."""
        ...

    def cost_per_1k_tokens(self, model: str) -> Price:
        """The price of `model`, or raise `PriceUnavailable`.

        Called *before* the request is dispatched, so a model muse cannot price is
        never called at all.
        """
        ...


class ProviderRegistryError(Exception):
    """Base for registry failures."""


class UnregisteredProvider(ProviderRegistryError):
    """No provider is registered under the requested name."""


class ProviderRegistry:
    """Name to provider.

    Deliberately not a dict subclass and not a global. A registry is built at boot
    and handed to the router, so two tests can register different providers for the
    same name without either seeing the other's, and a route file can be validated
    against exactly the providers that will serve it.
    """

    def __init__(self) -> None:
        self._providers: dict[str, Provider] = {}

    def register(self, provider: Provider) -> ProviderRegistry:
        """Add a provider. Re-registering a name is an error.

        The failure this prevents: an import cycle or a deploy-order change
        registering `openai` a second time with a stub, replacing the real adapter,
        and every subsequent request silently served by the wrong thing. Failing at
        registration makes that a boot error instead of a production incident.
        """
        if provider.name in self._providers:
            raise ValueError(f"a provider named {provider.name!r} is already registered")
        self._providers[provider.name] = provider
        return self

    def get(self, name: str) -> Provider:
        """The provider registered as `name`, or raise.

        Case-sensitive on purpose. Provider names come from `config/routes.yaml`,
        which is reviewed configuration rather than user input, so normalising case
        would paper over a typo in review instead of failing on it at boot.
        """
        try:
            return self._providers[name]
        except KeyError:
            raise UnregisteredProvider(
                f"no provider is registered as {name!r}; registered: "
                f"{', '.join(sorted(self._providers)) or 'none'}"
            ) from None

    def names(self) -> tuple[str, ...]:
        """Registered names, sorted, for diagnostics and route validation."""
        return tuple(sorted(self._providers))


class LiteLLMProvider:
    """openai and anthropic through litellm's Python API.

    Two design points worth stating:

    - **The litellm module is injected, not imported at module scope.** The import
      costs seconds and pulls in a model price table and a cloud SDK. Injecting it
      also means the tests exercise the real adapter without a network: the stub
      raises the same exception types the real module raises, so the mapping below
      is the code under test, not a mock of it.
    - **Exception mapping is explicit and ordered.** `ContextWindowExceededError`
      subclasses `BadRequestError` upstream, so the specific check has to come
      first. A mis-ordered mapping does not crash; it files a too-long prompt as a
      malformed request and sends an operator to look at the wrong thing.
    """

    def __init__(
        self,
        name: str,
        api_key: str | Secret | None = None,
        litellm: Any = None,
        *,
        credentials: CredentialResolver | None = None,
    ) -> None:
        # Two kinds of vendor and two tables to be in: a completion vendor is in
        # `VENDOR_PREFIXES`, and a decisions-only vendor is in
        # `DECISIONS_VENDORS` instead — deliberately absent from the prefix map,
        # because prefixing a decisions vendor would build a completions path
        # that does not exist for it. Validating against one table only is what
        # made a decisions-only vendor unconstructible.
        if name not in VENDOR_PREFIXES and name not in DECISIONS_VENDORS:
            raise ValueError(
                f"{name!r} is not a supported vendor; supported: "
                f"{', '.join(sorted(set(VENDOR_PREFIXES) | DECISIONS_VENDORS))}"
            )
        if api_key is None and credentials is None:
            # Neither: a provider with no way to get a key would fail on its first
            # call, in production, with a message about a missing constructor
            # argument. Said here instead.
            raise ValueError(
                f"a provider for {name!r} needs either an api_key or a credentials resolver"
            )
        self.name = name
        self._static_key = Secret(api_key) if isinstance(api_key, str) else api_key
        self._credentials = credentials
        self._litellm = litellm

    async def _key(self) -> Secret | None:
        """This provider's key, or `None` when it has none configured.

        Resolved per call rather than cached at construction, so a rotated key takes
        effect on the next request. Caching would need invalidation, and a cache that
        cannot be invalidated from outside the process is a credential that needs a
        deploy to rotate.
        """
        if self._static_key is not None:
            return self._static_key
        # No `if self._credentials is None` guard: the constructor refuses a provider
        # with neither a key nor a resolver, so a resolver is always present here and a
        # guard for it would be a branch nothing can reach.
        try:
            return await self._credentials.credential_for(self.name)
        except CredentialUnavailable:
            # "No key for this vendor" is a state, not an error, as far as the adapter
            # is concerned: `complete` and `health` each turn it into the error their
            # caller needs, and a provider that cannot serve a request should not
            # raise on the way to saying so.
            return None

    @property
    def litellm(self) -> Any:
        """The litellm module, imported on first use.

        Deferred rather than imported at module scope for the reason in the class
        docstring, and because a test that injects a stub must not be overridden by
        a real import happening later.
        """
        if self._litellm is None:
            import litellm as module

            self._litellm = module
        return self._litellm

    def qualified_model(self, model: str) -> str:
        """`gpt-4o-mini` -> `openai/gpt-4o-mini`, under whichever vendor this is.

        The prefix is what tells litellm which vendor to call. Without it litellm
        infers the vendor from the model name, so a model name that happens to be
        routable by two vendors goes to whichever one litellm guessed.

        It is THIS vendor's prefix, not a check for any slash. An aggregator
        serves several vendors' models behind one credential, and
        `openai/gpt-4o-mini` under openrouter is a request for openrouter's
        endpoint — litellm's spelling is `openrouter/openai/gpt-4o-mini`, and a
        name that already carries a slash is exactly the aggregator's case. The
        one exemption is a model already carrying THIS vendor's own prefix.
        """
        if self.name in DECISIONS_VENDORS:
            # `typesafe/jev-latest`, not `jev-latest`: the prefix is how litellm
            # knows WHICH provider to call, and without it the request fails
            # with `LLM Provider NOT provided` — an error naming a model rather
            # than naming the missing qualifier. This is still not a completions
            # path: the call is `adecisions`, and the prefix qualifies the
            # provider rather than the operation.
            return f"{self.name}/{model}"
        prefix = VENDOR_PREFIXES[self.name]
        if model.startswith(f"{prefix}/"):
            return model
        return f"{prefix}/{model}"

    def priceable_model(self, model: str) -> str:
        """The name to look a model's price up under.

        An aggregator is priced by the model it is serving, not by its own
        spelling of it: litellm's model-cost map has `openai/gpt-4o-mini` in it
        and has no entry for `openrouter/openai/gpt-4o-mini`, because the
        aggregator's own price is a markup on the vendor's rather than a fact
        litellm publishes. So the vendor prefix is stripped and the lookup is
        made against the model underneath.

        A model vendor looks up under its own qualified name, which is what it
        always did — the strip only applies when THIS vendor is an aggregator,
        and the set of those is declared rather than inferred.
        """
        if self.name not in AGGREGATORS:
            return self.qualified_model(model)
        qualified = self.qualified_model(model)
        _, _, underlying = qualified.partition("/")
        # A bare model with no vendor in it stays as it is: there is nothing to
        # strip, and quoting the unqualified name is the honest answer.
        return underlying if underlying else qualified

    def cost_per_1k_tokens(self, model: str) -> Price:
        """The published price of `model`, in micro-dollars per 1k tokens.

        The single place in muse where a float touches money: litellm quotes
        dollars per token, and this converts once, on the way in, to the integer
        unit everything else uses.
        """
        if self.name in DECISIONS_VENDORS:
            # litellm's cost map is a COMPLETION price table and has no entry for
            # a decision model. Pricing one from it would raise
            # `PriceUnavailable` for a call that cost a known amount — the
            # decision's own reported usage is the price, and the meter reads it
            # off the response rather than looking it up here.
            return Price(0, 0)
        try:
            info = self.litellm.get_model_info(self.priceable_model(model))
            price = Price(
                input_micros=_per_1k_micros(info["input_cost_per_token"]),
                output_micros=_per_1k_micros(info["output_cost_per_token"]),
            )
        except PriceUnavailable:
            raise
        except Exception as error:  # litellm's price table is third-party data
            raise PriceUnavailable(
                f"no published price for {self.name}/{model}: {error}"
            ) from error
        return price

    async def complete(self, request: CompletionRequest) -> Completion:
        """One completion, or a `ProviderError` subclass describing why not."""
        key = await self._key()
        if not key:
            # Checked before the call: no request, no spend, no rate-limit counter
            # spent on a credential that was never going to work.
            raise CredentialUnavailable(f"no credential is configured for provider {self.name!r}")
        kwargs: dict[str, Any] = {
            "model": self.qualified_model(request.model),
            "messages": [message.as_wire() for message in request.messages],
            "api_key": key.reveal(),
        }
        if self.name in VENDOR_API_BASES:
            kwargs["api_base"] = VENDOR_API_BASES[self.name]
        headers = self._outbound_headers()
        if headers:
            kwargs["extra_headers"] = headers
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            # THE ONLY PLACE a caller-supplied temperature is a float. It arrives
            # from the wire as a decimal string and is converted once, in
            # `api.RouteRequestBody.sampling_temperature()`, because this is the
            # seam where a float is unavoidable: LiteLLM declares
            # `temperature: float | None`. Omitted rather than sent as `None`
            # because some vendors reject an explicit null.
            kwargs["temperature"] = request.temperature
        try:
            response = await self.litellm.acompletion(**kwargs)
        except Exception as error:  # a third-party exception taxonomy, mapped below
            raise self._translate(error, key) from error
        return self._to_completion(request.model, response)

    async def decide(self, request: DecisionsRequest) -> Decision:
        """One decision, or a `ProviderError` subclass describing why not.

        `litellm.adecisions` — the decisions half of the library this service already
        embeds. The whole point of calling it rather than a completion with a JSON
        schema bolted on is the vocabulary: a `noul` answer *is* a calibrated
        probability, and a caller reading `0.93` off this service should get the same
        sentence litellm's own proxy would have written.

        The questions are handed over keyed by a muse-assigned name rather than as a
        bare sequence. litellm's decisions endpoint takes a `state` plus a named
        question map, so the name is where the answer comes back under; assigning it
        here means two questions with the same label cannot silently collapse into
        one answer, and the answer order is the question order regardless.
        """
        key = await self._key()
        if not key:
            # Checked before the call: no request, no spend, no rate-limit counter
            # spent on a credential that was never going to work.
            raise CredentialUnavailable(f"no credential is configured for provider {self.name!r}")
        kwargs: dict[str, Any] = {
            "model": self.qualified_model(request.model),
            # `input=` and a LIST of named questions: litellm's OpenAI-flavoured
            # decisions dialect. The map form is its deprecated `systemone`
            # spelling and emits a UserWarning per call, on a path that runs on
            # every mention.
            "input": request.input,
            "questions": [question.as_wire() for question in request.questions],
            "api_key": key.reveal(),
        }
        if self.name in VENDOR_API_BASES:
            kwargs["api_base"] = VENDOR_API_BASES[self.name]
        headers = self._outbound_headers()
        if headers:
            kwargs["extra_headers"] = headers
        try:
            response = await self.litellm.adecisions(**kwargs)
        except Exception as error:  # a third-party exception taxonomy, mapped below
            raise self._translate(error, key) from error
        return self._to_decision(request.model, response)

    def _to_decision(self, model: str, response: Any) -> Decision:
        """Normalise litellm's decision response, or raise `ResponseShapeError`.

        litellm answers every question it was asked and returns the usage on the
        call rather than on each answer. Both are checked here for the reason
        `_to_completion` checks its own: a decision with no answers, or one whose
        token counts are unknown, cannot be metered — and an unmetered token spend
        is the one failure this service exists to prevent.
        """
        answers = _read(response, "answers")
        if not answers:
            raise ResponseShapeError(f"{self.name} returned a decision with no answers")
        usage = _read(response, "usage")
        if not usage:
            raise ResponseShapeError(
                f"{self.name} returned a decision with no usage; a decision whose "
                "token counts are unknown cannot be metered"
            )
        return Decision(
            provider=self.name,
            model=model,
            answers={str(name): _answer_fields(answer) for name, answer in _items(answers)},
            tokens_in=int(_read(usage, "input_tokens") or 0),
            tokens_out=int(_read(usage, "output_tokens") or 0),
            # The router's price, resolved before dispatch and carried across the
            # call: `cost_per_1k_tokens` is the one place a price is read, and a
            # second read here would be a second chance to disagree with it.
            price=self.cost_per_1k_tokens(model),
        )

    def _outbound_headers(self) -> dict[str, str]:
        """The tracing headers to send with this call.

        Read from the ambient OpenTelemetry context rather than passed in, so the
        `Provider` protocol does not grow a `traceparent` argument that every future
        adapter would have to remember to honour — and so a provider that does not
        implement it cannot get it wrong.

        Empty when nothing is tracing, rather than a fabricated header. Inventing a
        trace id would drop muse's spans into whatever trace the vendor assigned, and
        would make "we propagate the caller's trace" untrue in the one configuration
        nobody configured.
        """
        header = outbound_traceparent()
        return {TRACEPARENT_HEADER: header} if header else {}

    def _to_completion(self, model: str, response: Any) -> Completion:
        """Normalise litellm's response, or raise `ResponseShapeError`.

        Every branch here is a shape that occurs in practice — a provider error page
        arriving as a 200, a truncated stream, a tool call with null content. None of
        them may become an `IndexError` or a `TypeError` three frames down: an
        upstream problem has to be reported as one.
        """
        choices = _read(response, "choices")
        if not choices:
            raise ResponseShapeError(f"{self.name} returned a response with no choices")
        choice = choices[0]
        message = _read(choice, "message")
        if not message:
            raise ResponseShapeError(
                f"{self.name} returned a choice with no message; a completion that "
                "cannot be read is not metered, because its usage is unknown"
            )
        content = _read(message, "content")
        if content is None:
            # A tool call has null content. v1 has no tool support, so reporting it
            # as empty text would hand the caller a silent wrong answer.
            raise ResponseShapeError(
                f"{self.name} returned a message with no content; tool calls are not "
                "supported by this service"
            )
        usage = _read(response, "usage")
        if not usage:
            raise ResponseShapeError(
                f"{self.name} returned a response with no usage; a completion whose "
                "token counts are unknown cannot be metered"
            )
        return Completion(
            provider=self.name,
            model=model,
            content=content,
            tokens_in=int(_read(usage, "prompt_tokens") or 0),
            tokens_out=int(_read(usage, "completion_tokens") or 0),
            price=self.cost_per_1k_tokens(model),
            finish_reason=_read(choice, "finish_reason"),
        )

    def _translate(self, error: Exception, key: Secret) -> Exception:
        """Map a litellm exception onto a muse one.

        The credential is scrubbed out of the message on the way through: a provider
        that echoes the key it rejected would otherwise put a live credential in
        every log line and every error body downstream of here.

        `sanitise` does the scrubbing *on the original exception*, not just on the
        copy returned here. That is the part that is easy to get wrong — see its
        docstring.
        """
        detail = _sanitise(error, key)
        name = type(error).__name__
        module = self.litellm
        if name == "ContextWindowExceededError":
            return ProviderInvalidRequest(f"context window exceeded: {detail}")
        if _is(module, "AuthenticationError", error):
            if _looks_like_missing_credential(detail):
                return CredentialUnavailable(
                    f"no credential is configured for provider {self.name!r}: {detail}"
                )
            return ProviderAuthError(f"{self.name} rejected the credential: {detail}")
        if _is(module, "RateLimitError", error):
            return ProviderRateLimited(f"{self.name} rate limited the request: {detail}")
        if _is(module, "Timeout", error):
            # A 408 is the *server* saying the request never arrived complete, which
            # is a definite "not processed" and therefore safe to repeat. Any other
            # timeout is a deadline on our side of a request that was already in
            # flight: the vendor may have completed and billed it, so retrying it can
            # charge the customer twice for one request. See ProviderIndeterminate.
            if _status_of(error) == 408:
                return ProviderTimeout(f"{self.name} returned 408: {detail}")
            return ProviderIndeterminate(
                f"{self.name} did not answer in time and the request may have been "
                f"processed: {detail}"
            )
        if _is(module, "ServiceUnavailableError", error):
            return ProviderUnavailable(f"{self.name} is unavailable: {detail}")
        if _is(module, "APIConnectionError", error):
            # A reset socket or a DNS failure is transient from the caller's point
            # of view, and the fallback candidate is exactly the right answer to it.
            return ProviderUnavailable(f"could not reach {self.name}: {detail}")
        if _is(module, "ContentPolicyViolationError", error):
            return ContentPolicyError(f"{self.name} refused on content policy: {detail}")
        if (
            _is(
                module,
                "BadRequestError",
                error,
            )
            or _is(module, "NotFoundError", error)
            or _is(module, "InvalidRequestError", error)
        ):
            return ProviderInvalidRequest(f"{self.name} rejected the request: {detail}")
        if _is(module, "InternalServerError", error):
            return ProviderUnavailable(f"{self.name} returned a server error: {detail}")
        # The default is the retryable error: an error muse has not seen is most
        # likely a transport problem on a route it does not recognise, and the
        # fallback is recorded in the failure chain either way, so nothing is
        # silent about it.
        return ProviderUnavailable(f"{self.name} failed ({name}): {detail}")

    async def health(self) -> Health:
        """A one-token completion as the probe.

        Not a dedicated endpoint: a health check that passes while completions fail
        is worse than no health check, and every provider here is guaranteed to have
        the one endpoint that matters. `max_tokens: 1` keeps the probe's cost
        negligible, and `health()` never raises — a raising health check takes down
        whoever asked, and readiness is where a dependency failure belongs.
        """
        key = await self._key()
        if not key:
            return Health(
                healthy=False,
                detail=f"no credential is configured for provider {self.name!r}",
            )
        try:
            await self.litellm.acompletion(
                model=self.qualified_model(PROBE_MODELS[self.name]),
                messages=[{"role": "user", "content": "ping"}],
                api_key=key.reveal(),
                max_tokens=1,
            )
        except Exception as error:  # a probe reports a failure, it does not raise
            # No in-place scrub here, unlike the error path: this detail is returned
            # as a string and nothing chains the original exception, so no second
            # copy of the message is left holding the key.
            return Health(healthy=False, detail=redact(str(error), key))
        return Health(healthy=True, detail="ok")


def _is(module: Any, class_name: str, error: Exception) -> bool:
    """Whether `error` is an instance of `module.<class_name>`, if that class exists.

    Name-based rather than a hard import so a test's stub needs only the classes it
    is exercising, and so a future litellm release that renames one of these
    degrades to the default mapping instead of an `AttributeError` at import.
    """
    cls = getattr(module, class_name, None)
    return isinstance(cls, type) and isinstance(error, cls)


def _status_of(error: Exception) -> int | None:
    """The HTTP status an upstream exception carries, if it carries one.

    Read defensively because the attribute is third-party: litellm attaches
    `status_code` to most of its exceptions, but not to all, and a transport error
    has no HTTP layer at all. `None` means "unproven" and is never treated as
    transient — see `muse.errors.is_retryable_status`.
    """
    status = getattr(error, "status_code", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


_MISSING_CREDENTIAL_HINTS = (
    "no api key",
    "api key not set",
    "api_key",
    "credential",
    "no credentials",
    "not set",
)


def _looks_like_missing_credential(detail: str) -> bool:
    """Whether an auth failure is really an absent key rather than a wrong one.

    The two need different responses — one is a deploy mistake, the other an
    operator action — and litellm reports both through `AuthenticationError`.
    """
    lowered = detail.lower()
    return any(hint in lowered for hint in _MISSING_CREDENTIAL_HINTS)


def _sanitise(error: Exception, secret: Secret) -> str:
    """`error`'s message with the credential scrubbed out, ready to report.

    The scrub is applied to the original exception *in place*, not only to the string
    this function returns. That distinction is the whole reason this function exists
    rather than a bare `redact(str(error), secret)`.

    `complete` raises the mapped error `from error`, which keeps the vendor's
    exception as the `__cause__` — and every traceback renderer in the ecosystem
    (`traceback.format_exception`, `logging.exception`, an APM agent, Sentry) prints
    that cause together with its own message. Scrubbing only the reported message
    therefore leaves the plaintext key in the chained cause, so a `logger.exception`
    downstream writes the live credential to disk while the reported error body
    looks clean. Rewriting the cause's message closes that hole.

    The cause keeps its class and its traceback frames, which is the part of a
    provider stack trace worth having; only the credential is removed. The message
    is left exactly as-is when it carries no credential, so an untouched upstream
    message is not silently re-worded on its way to the logs.
    """
    detail = redact(str(error), secret)
    if detail != str(error):
        error.args = (detail,)
    return detail


def _per_1k_micros(per_token: Any) -> int:
    """Dollars per token -> micro-dollars per 1k tokens, rounded up.

    Rounds *up* rather than to nearest, deliberately: a per-1k price below one
    micro-dollar becomes 1, not 0. Zero means free, and a rounding-down bug here is
    invisible until a month's usage does not add up to the provider's invoice.
    """
    from decimal import ROUND_CEILING, Decimal

    try:
        dollars = Decimal(str(per_token))
    except Exception as error:
        # litellm's price table is third-party data that has shipped with nulls and
        # strings. A float cast of one of those raises, and the honest translation
        # of "this row is unusable" is "no price for this model".
        raise PriceUnavailable(f"unusable price entry {per_token!r}: {error}") from error
    micros = dollars * Decimal(1000) * Decimal(PER_DOLLAR)
    return int(micros.to_integral_value(rounding=ROUND_CEILING))
