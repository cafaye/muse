"""The error *class* muse puts on a span, and where that vocabulary comes from.

`error.type` used to be `type(error).__name__`, which meant muse spelled the same
failure the way its own class hierarchy spelled it: `ProviderAuthError`. Every other
service will spell it their own way too, and a fleet-wide error view
(PLAN §7b: partition by `service.name`, filter on span status `error`, drill down by
`error.type`) then needs a mapping table instead of being a query. core-05 closed the
vocabulary to thirteen values for exactly that reason, and this file is the half of
that work which belongs to muse: drawing from the list rather than from our own class
names.

Three properties are asserted here, and each one fails in a different way:

- **The vocabulary is loaded, not retyped.** `error_types()` reads core's
  `traces.schema.json`. A Python list of thirteen strings would be a second source of
  truth, and core would change the enum while muse went on emitting a value the schema
  had stopped accepting — the drift this packet exists to prevent.
- **Every class muse raises is mapped.** Not the ones migrated today: every class in
  `muse.errors`, because the failure being caught is the exception class added in six
  months with no entry. The guard enumerates the module rather than a hand-written list,
  so a new class cannot hide by not being written down.
- **An unmapped class is refused, not defaulted.** `_OTHER` is reachable — it is one of
  the thirteen and `ProviderIndeterminate` is mapped onto it — but it is never a
  fallback. `dict.get(cls, "_OTHER")` would make forgetting a mapping silent, and the
  result is a dashboard full of `_OTHER` that nobody can act on.

The expected table is written out in full below rather than derived from the
implementation. A test asserting `error_type(X) == MAPPING[X]` would prove that a dict
looks like itself; this one is the independent statement of what each class means, and
it fails when the mapping is *wrong* rather than when it is absent.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from muse import errors as error_module
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
from muse.errortype import (
    SCHEMA_PATH,
    error_type,
    error_types,
    mapping,
)

pytestmark = pytest.mark.unit

#: What every class means, in one place, stated independently of the implementation.
#:
#: The reasoning behind the four that are not a lookup is carried forward from core-05
#: (`core/docs/observability.md`, "What is not migrated yet") rather than re-derived,
#: and each row below says which principle it follows. The vocabulary's own definitions
#: are in that document's class table.
EXPECTED: dict[type[BaseException], str] = {
    # --- ours: a bug in muse, or a deployment that cannot serve at all -----------
    MuseError: "internal_error",
    ConfigError: "internal_error",
    VaultKeyError: "internal_error",
    RouteConfigError: "internal_error",
    ContractError: "internal_error",
    InvalidEventType: "internal_error",
    InvalidSubject: "internal_error",
    InvalidServiceName: "internal_error",
    MeteringError: "internal_error",
    VaultDecryptError: "internal_error",
    UnclassifiedError: "internal_error",
    InvalidErrorType: "internal_error",
    # `VaultConfigError` is the one member of the config family that answers to a
    # caller rather than to a boot: "store this empty key" is a request that failed
    # validation, and core's `invalid_request` is exactly that — the fix is a
    # correction by whoever asked, not a page.
    VaultConfigError: "invalid_request",
    # --- the caller's request --------------------------------------------------
    RouteNotFound: "invalid_request",
    # --- the provider, by what it did -------------------------------------------
    ProviderError: "internal_error",
    ProviderAuthError: "provider_auth",
    ProviderInvalidRequest: "provider_rejected",
    ContentPolicyError: "provider_rejected",
    ProviderRateLimited: "rate_limited",
    ProviderTimeout: "timeout",
    ProviderUnavailable: "dependency_unavailable",
    ProviderIndeterminate: "_OTHER",
    CircuitOpen: "circuit_open",
    ResponseShapeError: "internal_error",
    # --- ours, but the rule we wrote and the caller tripped --------------------
    PriceUnavailable: "policy_denied",
    # --- the whole route --------------------------------------------------------
    CredentialUnavailable: "internal_error",
    AllCandidatesFailed: "dependency_unavailable",
    # --- the caller's credential (packet muse-06) -------------------------------
    # All four are `policy_denied` but one is not: core's definition is "a rule cafaye
    # itself owns refused the operation — authorization", which is exactly what a
    # refused credential is. `SigningKeysUnavailable` is the exception because its
    # responder is identity's operators rather than the caller's, and putting an outage
    # on the same dashboard as a fraud signal is how both get ignored.
    AuthError: "policy_denied",
    Unauthenticated: "policy_denied",
    InsufficientScope: "policy_denied",
    MissingAccount: "policy_denied",
    SigningKeysUnavailable: "dependency_unavailable",
}


def _declared_error_classes() -> dict[str, type[BaseException]]:
    """Every exception class `muse.errors` defines, by name.

    Read off the module rather than written out here, because a hand-written list is a
    list that goes stale the day a class is added — and a guard over a stale list is a
    guard that stopped guarding. The walk is over the module's own namespace, so
    `CandidateFailure` (a dataclass, not an exception) is excluded by the `issubclass`
    test rather than by an exception to it.
    """
    found = {
        name: value
        for name, value in vars(error_module).items()
        if isinstance(value, type)
        and issubclass(value, BaseException)
        and value.__module__ == error_module.__name__
    }
    assert found, "the walk found no error classes, so every assertion below is vacuous"
    return found


def test_every_error_class_muse_defines_is_mapped() -> None:
    """The guard that catches the exception class added in six months.

    Deliberately over the whole module rather than over the classes this packet
    migrated: a mapping that covers today's twenty-seven classes and tomorrow's twenty-
    eight is a mapping whose twenty-eighth entry is a 3am incident and a dashboard
    full of `_OTHER`.
    """
    unmapped = sorted(name for name, cls in _declared_error_classes().items() if cls not in mapping)
    assert unmapped == []


def test_the_expected_table_covers_every_declared_class() -> None:
    """The same completeness claim, made independently of `mapping`.

    Without this the table below could quietly stop describing the code — the exact way
    a "documented mapping" becomes a lie that nobody notices because the test asserts
    the document against itself.
    """
    assert set(EXPECTED) == set(_declared_error_classes().values())


def _instance(cls: type[BaseException]) -> BaseException:
    """A real instance of `cls`, built the way its own signature demands.

    Instantiating rather than passing the class is the point: two of these carry a
    model name and a failure chain, so a lookup that only ever saw the class object
    would be a lookup no production call site uses.
    """
    if cls is RouteNotFound:
        return cls("fast")
    if cls is AllCandidatesFailed:
        return cls("fast", ())
    return cls("boom")


def test_every_class_maps_to_the_value_this_file_says_it_maps_to() -> None:
    """The specific value per class, not "it is mapped".

    Requirement: an assertion that still fails when the mapping is *wrong*. Asserting
    membership would pass with `ProviderAuthError -> internal_error`, which is a wrong
    answer that looks like a right one.
    """
    for cls, expected in EXPECTED.items():
        assert error_type(_instance(cls)) == expected, f"{cls.__name__} is misclassified"
        assert error_type(cls) == expected, f"{cls.__name__} is misclassified"


def test_no_mapping_points_at_a_value_the_schema_does_not_have() -> None:
    """The table cannot invent a fourteenth class.

    core's `enum` is closed, and a value outside it is a span the collector's schema
    rejects — so a typo here is a 500-shaped telemetry hole, not a warning.
    """
    unknown = sorted(
        f"{cls.__name__} -> {value}" for cls, value in mapping.items() if value not in error_types()
    )
    assert unknown == []


def test_the_vocabulary_is_loaded_from_the_schema_rather_than_retyped() -> None:
    """`error_types()` is the schema's `enum`, read from the file that ships.

    Parsed here independently of `muse.errortype`, so this asserts the *result* of the
    load rather than that the load ran: a loader that filtered a value out would make
    the implementation's own check pass while this failed.
    """
    document = json.loads(Path(SCHEMA_PATH).read_text(encoding="utf-8"))
    declared = document["$defs"]["tracesAttributes"]["properties"]["error.type"]["enum"]
    assert sorted(error_types()) == sorted(declared)
    assert len(error_types()) == 13
    assert "error.message" not in document["$defs"]["tracesAttributes"]["properties"]


def test_the_schema_that_ships_is_the_one_the_mapping_claims_to_follow() -> None:
    """The vendored copy is a copy, and says where it came from.

    Not enforced by a magic string that could be edited along with the copy — that
    would be asserting a constant equals itself. What it does check is that the file is
    the one `muse.errortype` names, which is what stops a second copy appearing next to
    the first.
    """
    assert SCHEMA_PATH.name == "traces.schema.json"
    assert SCHEMA_PATH.parent.name == "telemetry"
    assert SCHEMA_PATH.is_file()
    assert SCHEMA_PATH.read_bytes() == Path(SCHEMA_PATH).read_bytes()


def test_an_unmapped_class_is_refused_rather_than_defaulted() -> None:
    """`_OTHER` is not a fallback.

    This is the shape `dict.get(cls, "_OTHER")` gets wrong: the mapping is forgotten
    once, the span is recorded as `_OTHER` forever, and nothing anywhere says so. Here
    the class that is not in the table raises, and the name it raises with is the class
    name — a symbol, never the exception's message.
    """

    class NeverRaised(ProviderUnavailable):  # a subclass, deliberately unmapped
        pass

    with pytest.raises(UnclassifiedError) as excinfo:
        error_type(NeverRaised("a detail that must not be quoted"))
    assert "NeverRaised" in str(excinfo.value)
    assert "a detail that must not be quoted" not in str(excinfo.value)


def test_the_mapping_is_by_exact_type_and_not_by_hierarchy() -> None:
    """A new subclass does not silently inherit its parent's class.

    The same discipline as `errors.is_retryable`, which tests `type(error) in ...`
    rather than walking the MRO precisely so that adding a subclass is a decision. A
    hierarchy walk here would let a class nobody classified be reported as one somebody
    else was, which is the same silence by another route.
    """
    assert mapping[ProviderUnavailable] == "dependency_unavailable"
    assert error_type(ProviderUnavailable("x")) == "dependency_unavailable"
    assert CircuitOpen not in ProviderUnavailable.__bases__
    with pytest.raises(UnclassifiedError):
        error_type(type("NewSubclass", (ProviderUnavailable,), {})("x"))


def test_a_value_outside_the_vocabulary_is_refused_when_the_table_is_built() -> None:
    """The check is callable, not only a module-level side effect.

    So it can be tested. `muse.errortype` runs the same function over its own table at
    import; a check that can only fail by breaking the import is a check nothing
    exercises.
    """
    from muse.errortype import checked_mapping

    with pytest.raises(InvalidErrorType) as excinfo:
        checked_mapping({ProviderAuthError: "provider_auth_typo"}, error_types())
    assert "provider_auth_typo" in str(excinfo.value)

    # ... and the table it just built is returned unchanged, so a caller can rely on it.
    assert checked_mapping({ProviderAuthError: "provider_auth"}, error_types()) == {
        ProviderAuthError: "provider_auth"
    }


def test_the_vendored_schema_is_byte_identical_to_core() -> None:
    """The drift guard. Set `MUSE_CORE_SCHEMAS=<core>/schemas` to run it.

    A vendored copy with no check is a copy that rots, and this lesson has now cost the
    fleet three times. The comparison is on bytes rather than on the parsed enum: a copy
    that differs only in a `description` is still a copy that has drifted, and the
    description is where a rule changes first.
    """
    root = os.environ.get("MUSE_CORE_SCHEMAS")
    if not root:
        pytest.skip("MUSE_CORE_SCHEMAS is not set; skipping parity with core")
    upstream = Path(root) / "telemetry" / "traces.schema.json"
    if not upstream.is_file():
        pytest.fail(
            f"MUSE_CORE_SCHEMAS is set to {root!r} but there is no telemetry/"
            "traces.schema.json under it; the copy in this repository is now "
            "unverifiable, which is worse than not having one"
        )
    assert upstream.read_bytes() == Path(SCHEMA_PATH).read_bytes()
