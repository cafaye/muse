"""Providers: the protocol muse routes through, and the three implementations.

`Provider` is a structural protocol with exactly three operations:

- `complete(request)` — one chat completion.
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

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from muse.errors import (
    ContentPolicyError,
    CredentialUnavailable,
    PriceUnavailable,
    ProviderAuthError,
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

#: The roles a message may have. OpenAI's set, which is a superset of what the
#: other vendors need; the adapter maps them if a vendor disagrees.
ROLES = ("system", "user", "assistant")

#: litellm's model-name prefix per vendor. Without it litellm infers the vendor
#: from the model name, which is how a request meant for one vendor gets billed to
#: another.
VENDOR_PREFIXES = {
    "openai": "openai",
    "anthropic": "anthropic",
}

#: The cheapest model each vendor will serve, used as the health probe. Per vendor
#: because a probe is a real request: sending anthropic's endpoint an openai model
#: name would report a healthy provider as broken.
PROBE_MODELS = {
    "openai": "gpt-4o-mini",
    "anthropic": "claude-haiku-4-5",
}

#: Micro-dollars per dollar.
PER_DOLLAR = 1_000_000


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

    A protocol, not a base class: an adapter is anything with these three methods,
    so a future vendor does not have to inherit from anything muse owns, and the
    router cannot reach a provider's internals by accident.
    """

    name: str

    async def complete(self, request: CompletionRequest) -> Completion:
        """One chat completion, or raise a `ProviderError` subclass."""
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
        if name not in VENDOR_PREFIXES:
            raise ValueError(
                f"{name!r} is not a supported vendor; supported: "
                f"{', '.join(sorted(VENDOR_PREFIXES))}"
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
        """`gpt-4o-mini` -> `openai/gpt-4o-mini`.

        The prefix is what tells litellm which vendor to call. Without it litellm
        infers the vendor from the model name, so a model name that happens to be
        routable by two vendors goes to whichever one litellm guessed.
        """
        if "/" in model:
            return model
        return f"{VENDOR_PREFIXES[self.name]}/{model}"

    def cost_per_1k_tokens(self, model: str) -> Price:
        """The published price of `model`, in micro-dollars per 1k tokens.

        The single place in muse where a float touches money: litellm quotes
        dollars per token, and this converts once, on the way in, to the integer
        unit everything else uses.
        """
        try:
            info = self.litellm.get_model_info(self.qualified_model(model))
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
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        if request.temperature is not None:
            kwargs["temperature"] = request.temperature
        try:
            response = await self.litellm.acompletion(**kwargs)
        except Exception as error:  # a third-party exception taxonomy, mapped below
            raise self._translate(error, key) from error
        return self._to_completion(request.model, response)

    def _to_completion(self, model: str, response: Any) -> Completion:
        """Normalise litellm's response, or raise `ResponseShapeError`.

        Every branch here is a shape that occurs in practice — a provider error page
        arriving as a 200, a truncated stream, a tool call with null content. None of
        them may become an `IndexError` or a `TypeError` three frames down: an
        upstream problem has to be reported as one.
        """
        try:
            choices = response["choices"]
        except (TypeError, KeyError) as error:
            raise ResponseShapeError(
                f"{self.name} returned a response with no choices: {error}"
            ) from error
        if not choices:
            raise ResponseShapeError(f"{self.name} returned a response with no choices")
        choice = choices[0]
        message = choice.get("message") if isinstance(choice, dict) else None
        if not message or "content" not in message:
            raise ResponseShapeError(
                f"{self.name} returned a choice with no message; a completion that "
                "cannot be read is not metered, because its usage is unknown"
            )
        content = message["content"]
        if content is None:
            # A tool call has null content. v1 has no tool support, so reporting it
            # as empty text would hand the caller a silent wrong answer.
            raise ResponseShapeError(
                f"{self.name} returned a message with no content; tool calls are not "
                "supported by this service"
            )
        usage = response.get("usage") if isinstance(response, dict) else None
        if not usage:
            raise ResponseShapeError(
                f"{self.name} returned a response with no usage; a completion whose "
                "token counts are unknown cannot be metered"
            )
        return Completion(
            provider=self.name,
            model=model,
            content=content,
            tokens_in=int(usage.get("prompt_tokens", 0)),
            tokens_out=int(usage.get("completion_tokens", 0)),
            price=self.cost_per_1k_tokens(model),
            finish_reason=choice.get("finish_reason"),
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
            return ProviderTimeout(f"{self.name} timed out: {detail}")
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
