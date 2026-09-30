"""muse's error taxonomy.

Every failure the service raises deliberately is one of these, and the type says
what a caller should do about it: retry, fix the configuration, or give up. That
is the whole reason for the hierarchy — `RETRYABLE_PROVIDER_ERRORS` is what the
router retries on, and an error that is not in that tuple ends the request.

Two rules hold for every message in this module:

- **No secret, ever.** A provider SDK's exception text can echo the API key it
  rejected. `muse.redaction.redact` scrubs a credential out of provider text
  before it becomes an exception message, so a `problem+json` body and a log line
  are both safe to ship.
- **No internals.** Messages name the thing that failed and what was expected,
  not a stack trace or a SQL string. Tracebacks belong in logs, which are not
  responses.
"""

from __future__ import annotations

from dataclasses import dataclass


class MuseError(Exception):
    """Base for every error muse raises on purpose.

    Anything that is *not* a `MuseError` reaching the client is a bug in muse, and
    the API layer turns those into a bare `internal` problem rather than leaking
    the type.
    """


class ConfigError(MuseError):
    """muse cannot start with the configuration it was given.

    Raised during boot, never during a request: a service that cannot decrypt its
    vault or read its routes has nothing useful to serve, and failing at startup
    is louder than failing on the first call.
    """


class VaultKeyError(ConfigError):
    """`MUSE_VAULT_KEY` is missing, not base64, or not 32 bytes.

    Refusing to boot is the point. A vault that starts with a default key is a
    vault whose keys are readable by anyone who has read the source.
    """


class RouteConfigError(ConfigError):
    """`config/routes.yaml` is unreadable, or describes something that cannot work.

    Also a boot failure: a route pointing at an unregistered provider is a
    misconfiguration, and finding out on the first request means the first
    request is the test.
    """


class ContractError(MuseError):
    """A value does not satisfy a cafaye contract core owns."""


class InvalidEventType(ContractError):
    """An event type is not `<service>.<entity>.<action>` per core's pattern."""


class InvalidSubject(ContractError):
    """An event subject is empty or outside core's allowed character set."""


class InvalidServiceName(ContractError):
    """A service name is not lowercase kebab-case per core's pattern."""


class ProviderError(MuseError):
    """One provider failed.

    The subclasses split on one question: would the identical call, unchanged,
    plausibly succeed a moment later? If yes it is retryable and belongs in
    `RETRYABLE_PROVIDER_ERRORS`; if no, retrying spends latency to arrive at the
    same answer.
    """


class ProviderAuthError(ProviderError):
    """The provider rejected the credential. Retrying with the same key cannot help."""


class ProviderInvalidRequest(ProviderError):
    """The provider rejected the request itself. A retry sends the same mistake."""


class ContentPolicyError(ProviderError):
    """The provider refused on content policy grounds. Not this service's call to retry."""


class PriceUnavailable(ProviderError):
    """No published price for the model, so its cost cannot be metered.

    Checked *before* the call is dispatched. A completion that cannot be priced is
    a completion whose spend nobody will ever be billed for, so muse declines to
    make it rather than report a cost of zero.
    """


class CredentialUnavailable(ProviderError):
    """No credential is stored for this provider."""


class ProviderTimeout(ProviderError):
    """The provider did not answer in time. Retryable."""


class ProviderRateLimited(ProviderError):
    """The provider is throttling. Retryable, and the router backs off."""


class ProviderUnavailable(ProviderError):
    """The provider is up but failing — 5xx, connection reset, overloaded. Retryable."""


class ResponseShapeError(ProviderError):
    """The provider answered with something that is not a completion.

    Not retryable in the router's sense: the same provider answering the same way
    again is the expected outcome of an adapter bug, and the honest response is to
    fall through to the next candidate and record why.
    """


class RouteNotFound(MuseError):
    """No route is configured for the requested model."""

    def __init__(self, model: str) -> None:
        super().__init__(f"no route is configured for model {model!r}")
        self.model = model


@dataclass(frozen=True, slots=True)
class CandidateFailure:
    """Why one candidate on a route did not serve the request.

    `detail` is already redacted: it is provider-supplied text that has been
    through `redact()` with the credential that was in play.
    """

    provider: str
    model: str
    error: type[ProviderError]
    detail: str
    attempts: int


class AllCandidatesFailed(MuseError):
    """Every candidate on the route was tried and none produced a completion.

    The failures are carried on the exception rather than only in its message, so
    the API layer can report which providers were tried without re-deriving it —
    and so a test can assert the whole chain instead of a string.
    """

    def __init__(self, model: str, failures: tuple[CandidateFailure, ...]) -> None:
        tried = ", ".join(f"{failure.provider}/{failure.model}" for failure in failures)
        super().__init__(f"every candidate for model {model!r} failed (tried: {tried})")
        self.model = model
        self.failures = failures


#: The exact set the router retries. Adding a subclass of `ProviderError` without
#: deciding this is the decision — an unlisted error ends the request on the first
#: candidate, which is the conservative failure.
RETRYABLE_PROVIDER_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderTimeout,
    ProviderRateLimited,
    ProviderUnavailable,
)


def is_retryable(error: BaseException) -> bool:
    """Whether the router should try this candidate again after a backoff."""
    return isinstance(error, RETRYABLE_PROVIDER_ERRORS)
