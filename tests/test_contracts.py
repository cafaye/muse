"""Contract constants and their parity with core.

`muse/contracts.py` carries a copy of the two regexes core owns in
`schemas/event-envelope.schema.json` ($defs.eventType, $defs.serviceName). A copy
is a drift risk, so this module asserts two things:

1. the copy behaves like core's pattern on a table of values (the contract the
   service actually relies on), and
2. when a core checkout is pointed at by `MUSE_CORE_SCHEMAS`, the copy is
   byte-identical to the schema's own pattern — the check that catches drift.

The parity test skips without the env var rather than failing: a bare checkout of
muse has no core repo to read, and a test that cannot run must say so rather than
pass silently. `tests/test_event_contract.py` is where the real envelope gets
validated against core's schema.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from muse.contracts import (
    EVENT_TYPE_PATTERN,
    SERVICE_NAME,
    SERVICE_NAME_PATTERN,
    TOKENS_CONSUMED,
    validate_event_type,
    validate_service_name,
    validate_subject,
)
from muse.errors import (
    RETRYABLE_PROVIDER_ERRORS,
    AllCandidatesFailed,
    CandidateFailure,
    ContentPolicyError,
    ContractError,
    CredentialUnavailable,
    InvalidEventType,
    InvalidSubject,
    PriceUnavailable,
    ProviderAuthError,
    ProviderError,
    ProviderInvalidRequest,
    ProviderRateLimited,
    ProviderTimeout,
    ProviderUnavailable,
    ResponseShapeError,
    RouteNotFound,
    is_retryable,
)

pytestmark = [pytest.mark.unit]

#: Values core's patterns must accept and must reject, as data so a schema change
#: shows up as a failing row rather than as a failing regex. Each list entry is
#: the same fact core's schema states, transcribed.
VALID_EVENT_TYPES = [
    "muse.tokens.consumed",
    "identity.user.created",
    "identity.api_key.revoked",
    "email-sender.email.queued",
]
INVALID_EVENT_TYPES = [
    "muse.tokens",  # two segments
    "muse.tokens.consumed.extra",  # four segments
    "tokens.consumed",  # unprefixed
    "muse.Tokens.consumed",  # upper case
    "muse.tokens.Consumed",  # upper case action
    "muse_tokens.consumed",  # underscore in the service segment
    "muse.tokens.consumed!",  # illegal character
    ".muse.tokens.consumed",  # leading separator
    "",
]
VALID_SERVICE_NAMES = ["muse", "identity", "email-sender", "caf"]
INVALID_SERVICE_NAMES = ["Muse", "muse_2", "-muse", "muse-", "m", "", "muse service"]
VALID_SUBJECTS = [
    "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
    "platform",  # core's reserved literal for an event with no single entity
    "acct:01J9Z8",
    "a/b",
    "usr_01J9Z8QK5M4N7P2R3T6V8W9X0A",  # core's own example subject
]
INVALID_SUBJECTS = ["", "has space", "-leading", "tab\there", "a" * 201]


@pytest.mark.parametrize("value", VALID_EVENT_TYPES)
def test_valid_event_types_are_accepted(value: str) -> None:
    assert validate_event_type(value) == value


@pytest.mark.parametrize("value", INVALID_EVENT_TYPES)
def test_invalid_event_types_are_rejected(value: str) -> None:
    with pytest.raises(InvalidEventType):
        validate_event_type(value)


def test_the_retry_list_is_exactly_the_three_transient_failures() -> None:
    """The list is the router's whole retry policy, so a new error class appearing
    in it is a behaviour change and must be a deliberate edit here."""
    assert (
        ProviderTimeout,
        ProviderRateLimited,
        ProviderUnavailable,
    ) == RETRYABLE_PROVIDER_ERRORS


@pytest.mark.parametrize(
    "error",
    [
        ProviderTimeout,
        ProviderRateLimited,
        ProviderUnavailable,
    ],
)
def test_transient_failures_are_retryable(error: type[ProviderError]) -> None:
    assert is_retryable(error("transient"))


@pytest.mark.parametrize(
    "error",
    [
        ProviderAuthError,
        ProviderInvalidRequest,
        ContentPolicyError,
        PriceUnavailable,
        CredentialUnavailable,
        ResponseShapeError,
    ],
)
def test_permanent_failures_are_not_retryable(error: type[ProviderError]) -> None:
    """Retrying a rejected credential or a malformed request spends latency to
    arrive at the same answer, so the router advances to the next candidate
    instead."""
    assert not is_retryable(error("permanent"))


def test_a_non_provider_error_is_not_retryable() -> None:
    """A bug in muse must not be retried as if the provider were flaky."""
    assert not is_retryable(ValueError("bug"))


def test_route_not_found_carries_the_model() -> None:
    error = RouteNotFound("gpt-4o-mini")
    assert error.model == "gpt-4o-mini"
    assert "gpt-4o-mini" in str(error)


def test_all_candidates_failed_lists_what_was_tried() -> None:
    """The message is what a support ticket gets; naming the candidates is the
    difference between 'it failed' and 'openai and anthropic both failed'."""
    failures = (
        CandidateFailure("openai", "gpt-4o-mini", ProviderTimeout, "timed out", 2),
        CandidateFailure("anthropic", "claude", ProviderAuthError, "bad key", 1),
    )
    error = AllCandidatesFailed("fast", failures)
    assert error.model == "fast"
    assert error.failures == failures
    assert "openai/gpt-4o-mini" in str(error)
    assert "anthropic/claude" in str(error)


def test_a_candidate_failure_is_immutable() -> None:
    """It is recorded on an exception that may outlive the request that made it."""
    failure = CandidateFailure("openai", "gpt-4o-mini", ProviderTimeout, "timed out", 1)
    with pytest.raises(AttributeError):
        failure.attempts = 2  # type: ignore[misc]


@pytest.mark.parametrize("value", VALID_SERVICE_NAMES)
def test_valid_service_names_are_accepted(value: str) -> None:
    assert validate_service_name(value) == value


@pytest.mark.parametrize("value", INVALID_SERVICE_NAMES)
def test_invalid_service_names_are_rejected(value: str) -> None:
    with pytest.raises(ContractError):
        validate_service_name(value)


@pytest.mark.parametrize("value", VALID_SUBJECTS)
def test_valid_subjects_are_accepted(value: str) -> None:
    assert validate_subject(value) == value


@pytest.mark.parametrize("value", INVALID_SUBJECTS)
def test_invalid_subjects_are_rejected(value: str) -> None:
    with pytest.raises(InvalidSubject):
        validate_subject(value)


def test_the_error_names_the_offending_value() -> None:
    with pytest.raises(InvalidEventType) as excinfo:
        validate_event_type("muse.tokens")
    assert "muse.tokens" in str(excinfo.value)


def test_this_services_published_type_is_valid() -> None:
    """`muse.tokens.consumed` is what `cafaye.yml` advertises; a typo in it would
    break routing for every consumer at once."""
    assert validate_event_type(TOKENS_CONSUMED) == TOKENS_CONSUMED


def test_the_published_type_starts_with_this_services_name() -> None:
    """core requires the first segment to equal the publishing service."""
    assert TOKENS_CONSUMED.split(".")[0] == SERVICE_NAME


def core_envelope_schema() -> Path | None:
    """Locate core's envelope schema, or `None` when no checkout is pointed at."""
    root = os.environ.get("MUSE_CORE_SCHEMAS")
    if not root:
        return None
    candidate = Path(root) / "event-envelope.schema.json"
    return candidate if candidate.is_file() else None


def test_patterns_are_byte_identical_to_core() -> None:
    """The drift guard. Set `MUSE_CORE_SCHEMAS=<core>/schemas` to run it."""
    schema = core_envelope_schema()
    if schema is None:
        pytest.skip("MUSE_CORE_SCHEMAS is not set; skipping parity with core")
    defs = json.loads(schema.read_text())["$defs"]
    assert defs["eventType"]["pattern"] == EVENT_TYPE_PATTERN
    assert defs["serviceName"]["pattern"] == SERVICE_NAME_PATTERN
