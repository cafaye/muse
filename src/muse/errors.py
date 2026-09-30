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

    Refusing to boot is the point. A vault that starts with a default key is a vault
    whose keys are readable by anyone who has read the source. The message names the
    variable and never its value, because a boot error is the one message guaranteed
    to be read aloud and pasted into a ticket.
    """


class VaultConfigError(ConfigError):
    """A vault operation was asked for something it cannot do: an unusable provider
    name, or an empty key.

    An empty key is refused rather than stored. Forwarded to a provider it comes back
    as a 401, which reads like a *wrong* key and sends an operator to rotate a
    credential that does not exist.
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


class ProviderIndeterminate(ProviderError):
    """The request was dispatched and the outcome is unknown.

    A read timeout: the bytes went out, the answer did not come back, and the vendor
    may have completed and billed the call. This is the one failure where a retry can
    **bill the customer twice for one request**, and it is separated from
    `ProviderTimeout` for exactly that reason.

    Not in `RETRYABLE_PROVIDER_ERRORS`. A route that wants it anyway says
    `retry_indeterminate: true`, per route, because the decision is a trade between
    availability and duplicate spend and only the route knows the price. The correct
    fix is an idempotency key (PLAN §7 assigns one to muse), which does not exist
    yet — so the safe default is one attempt.

    Note the deliberate asymmetry with `ProviderTimeout`: a 408 is the *server*
    saying the request never arrived complete, which is a definite "not processed" and
    therefore retryable. A timeout on our side of a request that was already in flight
    is not the same statement.
    """


class CircuitOpen(ProviderUnavailable):
    """The breaker is holding this provider back.

    A `ProviderUnavailable` so it reads correctly in a failure chain — the request
    could not be served by this provider, which is the same operational fact — while
    remaining distinguishable from a provider that was actually tried and failed. The
    difference is the whole point: one means "hold back", the other means "try the
    next candidate".
    """


class ResponseShapeError(ProviderError):
    """The provider answered with something that is not a completion.

    Not retryable in the router's sense: the same provider answering the same way
    again is the expected outcome of an adapter bug, and the honest response is to
    fall through to the next candidate and record why.
    """


class MeteringError(MuseError):
    """A completion cannot be turned into a metered event.

    Raised before the insert, while the call is still attributable to this request.
    The alternatives are both worse: a zero-cost event makes the spend invisible, and
    a negative one makes it a credit in somebody's invoice.
    """


class VaultDecryptError(MuseError):
    """A stored value could not be decrypted.

    Deliberately does not distinguish "wrong key" from "tampered ciphertext" from
    "row written by a future version": all three mean the same thing to an operator —
    this row is unreadable — and GCM's guarantee is precisely that you cannot tell
    them apart without the key. The message names the provider, which is the part they
    can act on, and never the ciphertext, which is the part they must not paste into a
    ticket.
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
#:
#: Each member is here because a repeat of the identical request provably did not
#: reach a billable state:
#:
#: - `ProviderTimeout` — a 408. The server is saying the request never arrived
#:   complete, so nothing was processed. A read timeout is *not* this; see
#:   `ProviderIndeterminate`.
#: - `ProviderRateLimited` — a 429. The vendor is asking to be left alone; a request
#:   refused for rate is not a request served.
#: - `ProviderUnavailable` — a 5xx, a reset socket, a DNS failure, an overload. The
#:   call failed at the transport or the server, not at the work.
#:
#: What is deliberately absent, and why:
#:
#: - `ProviderIndeterminate` — the request may already have been completed and
#:   billed. Retrying it is how one caller's request is paid for twice.
#: - `ProviderAuthError` / `ProviderInvalidRequest` / `ContentPolicyError` /
#:   `CredentialUnavailable` — the identical retry gets the identical answer, and each
#:   spends latency and rate-limit budget to get there.
#: - `ResponseShapeError` — the provider answered, and answered the same way it will
#:   answer again. That is an adapter bug, and retrying it hides the bug.
#: - `PriceUnavailable` — checked before dispatch; a model muse cannot price is never
#:   called, so there is nothing to retry.
RETRYABLE_PROVIDER_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderTimeout,
    ProviderRateLimited,
    ProviderUnavailable,
)

#: The HTTP statuses a repeat of the identical request can survive.
#:
#: This is the packet's rule stated as data: of the 4xx family only 408 and 429, and
#: the 5xx family in full. 408 and 429 are the two 4xx that mean "I did not do the
#: work" — the first because the request never arrived complete, the second because
#: the vendor is asking to be left alone. Every other 4xx is an *answer*.
#:
#: An unrecognised status is not in this set. Guessing "probably transient" for
#: something unrecognised is how a malformed request ends up in a retry loop, and how
#: a new vendor's 4xx gets several chances to bill the same call.
RETRYABLE_STATUS_CODES: frozenset[int] = frozenset({408, 429, 500, 502, 503, 504})


def is_retryable(error: BaseException) -> bool:
    """Whether the router should try this candidate again after a backoff.

    Note the *type* test rather than a walk of the class hierarchy, which is what
    keeps `CircuitOpen` (a `ProviderUnavailable` subclass) from being retried: a
    provider being held back is exactly the case where a retry is least useful.
    """
    return type(error) in RETRYABLE_PROVIDER_ERRORS


def is_retryable_status(status: int | None) -> bool:
    """Whether an HTTP status on its own justifies another attempt.

    `None` — no status, because the SDK raised something that carried none — is
    treated as unproven rather than as transient.
    """
    return status in RETRYABLE_STATUS_CODES


def trips_breaker(error: BaseException) -> bool:
    """Whether a failure counts against the provider's circuit breaker.

    The transient set only. The breaker exists to stop muse adding load to a provider
    that cannot take it; a 400 or a 401 is muse sending something the provider is
    right to refuse, and counting those would let one caller with a malformed request
    take the route down for everybody.

    `CircuitOpen` is excluded explicitly: it *is* the breaker, so letting it feed
    itself would open a held-back provider further open and reset its timer on every
    refused call — a provider could then never be re-probed.
    """
    return type(error) in RETRYABLE_PROVIDER_ERRORS or type(error) is ProviderIndeterminate
