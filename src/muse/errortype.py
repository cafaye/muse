"""muse's error classes, and the fleet vocabulary they are reported under.

`error.type` used to be `type(error).__name__`. That is a fine identifier and a
useless one: it answers "which class" in muse's own vocabulary, so the same failure is
`ProviderAuthError` here and something else everywhere else, and a fleet-wide error
view needs a mapping table before it can be a query (PLAN §7b). core-05 closed the
vocabulary to **thirteen values** — twelve classes plus the OpenTelemetry fallback
`_OTHER` — and made `error.type` an `enum` on all three signals, so a span carrying
anything else is rejected.

This module is the whole of muse's side of that change:

- **`error_types()` reads core's `traces.schema.json`.** Not a Python list of thirteen
  strings. A second copy of the vocabulary is a second source of truth, and core would
  change the enum while muse went on emitting a value the schema had stopped accepting —
  which is the drift this exists to prevent. The file under `muse/schemas/` is a
  byte-identical copy of core's, because a service's SDK setup has to read the spec
  without a checkout of core on hand; `tests/test_error_vocabulary.py` asserts the copy
  is byte-identical to core's whenever `MUSE_CORE_SCHEMAS` points at one, which is the
  only thing that stops a vendored copy from rotting.
- **`mapping` is the one table.** Every class `muse.errors` defines appears in it,
  because the failure being caught is the exception class added in six months with no
  entry, and `tests/test_error_vocabulary.py` asserts that over the module rather than
  over a list somebody remembered to update.
- **`error_type()` refuses what the table does not have.** `_OTHER` is in the
  vocabulary and reachable — `ProviderIndeterminate` is mapped onto it — but it is
  never a fallback. `dict.get(cls, "_OTHER")` is the wrong shape: it makes forgetting a
  mapping silent, and the result is a dashboard full of `_OTHER` that nobody can act on.
  So the lookup raises `UnclassifiedError` and the caller decides what to do about a
  request it must still serve (`muse.telemetry.record_error`).

Two rules about how the lookup behaves:

- **Exact type, never the hierarchy.** The same discipline as `errors.is_retryable`: a
  subclass does not inherit its parent's class, because a new `ProviderUnavailable`
  subclass reporting `dependency_unavailable` without anyone deciding it is the silence
  this module exists to remove. `CircuitOpen` and `ProviderIndeterminate` are both
  subclasses of other mapped classes and both have their own entries for exactly that
  reason.
- **A class name, never a message.** These values reach a tracing backend, and a
  vendor's content-policy rejection quotes the offending content back — so an exception
  message is a prompt by another route. Everything raised from here names a class.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from functools import cache
from pathlib import Path

from muse.errors import (
    AllCandidatesFailed,
    AuthError,
    CircuitOpen,
    ConfigError,
    ContentPolicyError,
    ContractError,
    CredentialUnavailable,
    InsufficientScope,
    InvalidErrorType,
    InvalidEventType,
    InvalidServiceName,
    InvalidSubject,
    MeteringError,
    MissingAccount,
    MuseError,
    PriceUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderIndeterminate,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
    RouteConfigError,
    RouteNotFound,
    SigningKeysUnavailable,
    Unauthenticated,
    UnclassifiedError,
    VaultConfigError,
    VaultDecryptError,
    VaultKeyError,
)

#: core's trace-signal schema, vendored byte-identically. See the module docstring, and
#: `tests/test_error_vocabulary.py::test_the_vendored_schema_is_byte_identical_to_core`
#: for the drift check that keeps it a copy rather than a fork.
SCHEMA_PATH = Path(__file__).parent / "schemas" / "telemetry" / "traces.schema.json"

#: The OpenTelemetry fallback, named once so the two places that use it are not also the
#: places that retype it. `ProviderIndeterminate` is mapped onto it deliberately, and
#: `muse.telemetry.record_error` records it as a last resort — both go through this
#: name, and both are checked against core's enum when `mapping` is built below, so
#: there is one string here and one spelling of it.
FALLBACK = "_OTHER"


@cache
def error_types() -> frozenset[str]:
    """core's error classes, read from the schema rather than restated here.

    Cached because `mapping` calls it while this module loads, and a caller in a hot
    path should not re-parse a 16KB document to learn a fact that cannot change while
    the process runs.
    """
    document = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    return frozenset(document["$defs"]["tracesAttributes"]["properties"]["error.type"]["enum"])


def checked_mapping(
    table: Mapping[type[BaseException], str], vocabulary: frozenset[str]
) -> dict[type[BaseException], str]:
    """`table`, or `InvalidErrorType` if any value is outside `vocabulary`.

    A function rather than an assertion inside the module body so that it can be
    *tested*: a check that can only fail by breaking the import is a check nothing
    exercises, and an untested check is a comment.
    """
    unknown = sorted(
        f"{cls.__name__} -> {value}" for cls, value in table.items() if value not in vocabulary
    )
    if unknown:
        raise InvalidErrorType(
            "these classes map to values that are not in core's error vocabulary "
            f"({len(error_types())} classes): " + "; ".join(unknown)
        )
    return dict(table)


#: Every class muse raises, and the fleet class it is reported under.
#:
#: Most rows are a lookup — the name says the thing. These are the ones that were a
#: judgement, with the reason inline; core-05's reasoning is carried forward from
#: `core/docs/observability.md` "What is not migrated yet" rather than re-derived, and
#: the vocabulary's own definitions are in that file's class table.
_TABLE: dict[type[BaseException], str] = {
    # --- a bug in muse, or a deployment that cannot serve at all --------------------
    # The base means "a bug in muse" by its own docstring, and everything below it is
    # either that or a request the caller made and a human has to correct.
    MuseError: "internal_error",
    # A config error is raised during boot and never during a request, so there is no
    # caller in the picture: `invalid_request` would say "ask whoever called us", and
    # there is nobody. A missing `MUSE_VAULT_KEY` is a total outage, which is what
    # `internal_error` is for. (core-05 said `invalid_request`; see the report — this
    # is the one row where the worker diverged, and it is one line to flip.)
    ConfigError: "internal_error",
    VaultKeyError: "internal_error",
    RouteConfigError: "internal_error",
    ContractError: "internal_error",
    # The contract is core's and the values are muse's own — an envelope we built with a
    # bad subject is our bug, not a caller's correction.
    InvalidEventType: "internal_error",
    InvalidSubject: "internal_error",
    InvalidServiceName: "internal_error",
    # A completion we cannot turn into a metered event. Our invariant, not a peer's.
    MeteringError: "internal_error",
    # The stored value is unreadable: wrong key, tampered ciphertext, or a row written by
    # a future version. All three mean the same thing to an operator and none of them is
    # the caller's fault.
    VaultDecryptError: "internal_error",
    # The two vocabulary failures themselves are ours by definition — one is a class
    # with no entry, the other an entry pointing outside core's enum.
    UnclassifiedError: "internal_error",
    InvalidErrorType: "internal_error",
    # `VaultConfigError` is the one member of the config family that answers to a caller
    # rather than to a boot: "store this empty key" is a request that failed validation,
    # and the fix is a correction by whoever asked.
    VaultConfigError: "invalid_request",
    # The caller named a model nothing routes. A 404 and a caller-side correction.
    RouteNotFound: "invalid_request",
    # --- the provider, by what it actually did -------------------------------------
    ProviderError: "internal_error",
    # A third party refused our credential. The responder owns the key.
    ProviderAuthError: "provider_auth",
    # "Accepted the call and refused the request: bad parameters, or its content
    # policy" — core's own words for `provider_rejected`. Deliberately not
    # `invalid_request`: the caller's bytes are what the vendor rejected, but the rule
    # they were rejected under is the vendor's, which is exactly what separates this
    # from `policy_denied`.
    ProviderInvalidRequest: "provider_rejected",
    # The vendor's content policy, and the class the redaction boundary is about: a
    # content-policy rejection quotes the offending content back. Recorded as a class
    # with no text in it, and never folded into `policy_denied`, which is about *our*
    # authorization.
    ContentPolicyError: "provider_rejected",
    ProviderRateLimited: "rate_limited",
    ProviderTimeout: "timeout",
    # A dependency answered and said it is broken. Note the conflation this row hides:
    # muse also maps a connection failure onto this class, and core has a separate
    # `connection_failed` precisely because "did it go out at all?" changes the
    # responder. Splitting the class is a resilience change, not a vocabulary one — see
    # the report.
    ProviderUnavailable: "dependency_unavailable",
    # Deliberately `_OTHER`, carried forward from core-05. It is the class that exists
    # so `_OTHER` is not a dumping ground: the request was dispatched and the outcome is
    # unknown, so it is not a timeout (which is a definite "not processed") and not a
    # connection failure (which is a definite "never left"). Nothing in the thirteen
    # fits, and inventing a fourteenth for it would be worse than using the fallback.
    ProviderIndeterminate: FALLBACK,
    # We chose not to call. Our breaker, not a failure of the peer.
    CircuitOpen: "circuit_open",
    # The provider answered and the answer was not a completion. An adapter bug, and
    # core's `internal_error` is "a broken invariant".
    ResponseShapeError: "internal_error",
    # --- our own rule, and the caller tripped it -------------------------------------
    # A model muse cannot price is one whose spend would never be billed, so muse
    # declines the call. The rule is cafaye's own and its rate is a misconfiguration
    # signal rather than an incident, which is what `policy_denied` is for. `internal_error`
    # was the alternative and it would page on a missing price-table row.
    PriceUnavailable: "policy_denied",
    # --- the caller's credential (packet muse-06) ---------------------------------
    # The base means "we refused a credential", and every subclass below is a more
    # specific version of that. `policy_denied` for all three because core's definition
    # is exactly this: "a rule cafaye itself owns refused the operation — authorization".
    #
    # A rate of these is a business signal (a mis-scoped integration, a credential
    # somebody is guessing at) and not an incident, which is precisely why they are not
    # `internal_error`: an auth failure rate on the incident dashboard would train
    # everyone to ignore that page.
    AuthError: "policy_denied",
    # The token is absent, forged, expired, or otherwise not usable. A caller-side
    # correction: get a fresh credential.
    Unauthenticated: "policy_denied",
    # The token is *good* and lacks the capability this operation needs. Separated from
    # `Unauthenticated` because the responder differs: this one is fixed by asking for a
    # scope, not by re-authenticating.
    InsufficientScope: "policy_denied",
    # A verified token with no tenant. Deliberately NOT `invalid_request` and not
    # `internal_error`: the request is well-formed and muse is not broken, so the class
    # that would say either is wrong, and `policy_denied` says what actually happened —
    # a cafaye-owned rule (core's `account_id` requirement) refused the operation.
    MissingAccount: "policy_denied",
    # identity's key set could not be fetched. The one auth failure that is NOT the
    # caller's fault and NOT an authorization decision, so it gets the class that means
    # "a dependency is broken": muse is up, identity is not. Reporting this as
    # `policy_denied` would put an outage on the same dashboard as a fraud signal.
    SigningKeysUnavailable: "dependency_unavailable",
    # --- the whole route --------------------------------------------------------------
    # No credential is stored for this provider. Ours to fix, never the caller's.
    CredentialUnavailable: "internal_error",
    # Every candidate on the route was tried and none served it: "the vendor is down",
    # which is the definition core gives `dependency_unavailable`.
    AllCandidatesFailed: "dependency_unavailable",
}

#: The table, checked against core's vocabulary while this module loads.
mapping: dict[type[BaseException], str] = checked_mapping(_TABLE, error_types())


def error_type(error: BaseException | type[BaseException]) -> str:
    """The fleet class for `error`, or `UnclassifiedError` naming what is missing.

    Takes an instance or the class itself: a breaker refusal is recorded before the
    exception that would describe it exists, and building one to throw away would be a
    lie about what happened.
    """
    cls = error if isinstance(error, type) else type(error)
    try:
        return mapping[cls]
    except KeyError:
        raise UnclassifiedError(
            f"{cls.__name__} has no entry in the error-class mapping; add one to "
            "muse.errortype.mapping — _OTHER is not a fallback"
        ) from None
