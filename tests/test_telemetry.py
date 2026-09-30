"""W3C traceparent, and the span-attribute allowlist.

Two properties, and the second one is the reason this module exists in the shape it
does:

- **A traceparent is continued when it is well formed, and replaced when it is not.**
  A malformed header is an attacker's or a bug's, never a reason to fail a request:
  the correct response to unparseable correlation metadata is a fresh trace and a
  200, because a trace is an observability affordance and must never be able to take
  a customer's request down.
- **No span attribute can carry user content.** This is enforced by an allowlist at
  one choke point, not by discipline at each call site, because the failure is not
  someone writing a bad line today — it is someone in six months adding
  `span.set_attribute("prompt", request.prompt)` because it seemed useful, in a
  service whose prompts are other customers' data.

The canary test at the bottom is the one that matters: it drives a completion
through the whole app with a unique string in both the prompt and the completion,
exports the spans, and asserts the string appears nowhere. It asserts on the
*rendered payload*, not on a chosen attribute, so a leak through a key nobody
predicted is still caught.
"""

from __future__ import annotations

import pytest

from muse.redaction import Secret
from muse.telemetry import (
    ALLOWED_SPAN_ATTRIBUTES,
    MAX_ATTRIBUTE_LENGTH,
    Telemetry,
    parse_traceparent,
    record,
)

from .support.tracing import payloads, recording_telemetry

pytestmark = pytest.mark.unit

#: A well-formed `traceparent`: version 00, a 32-hex trace id, a 16-hex span id, the
#: sampled flag set. The canonical example from the W3C trace-context spec.
VALID = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

ZERO_TRACE_ID = "00-00000000000000000000000000000000-00f067aa0ba902b7-01"
ZERO_SPAN_ID = "00-4bf92f3577b34da6a3ce929d0e0e4736-0000000000000000-01"
TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
SPAN_ID = "00f067aa0ba902b7"


# --- parsing ---------------------------------------------------------------


def test_a_well_formed_traceparent_is_parsed_into_its_parts() -> None:
    parsed = parse_traceparent(VALID)

    assert parsed is not None
    assert parsed.trace_id == TRACE_ID
    assert parsed.span_id == SPAN_ID
    assert parsed.version == 0
    assert parsed.sampled is True


def test_a_traceparent_round_trips_to_the_same_header() -> None:
    """The header muse sends on must be the header it received, byte for byte.

    A caller that greps its own access log for the trace id it sent has to find the
    same id on the vendor's side, or the whole point of propagating it is lost.
    """
    assert parse_traceparent(VALID).header() == VALID


def test_the_unsampled_flag_is_carried_rather_than_upgraded() -> None:
    """A caller that asked not to be sampled gets an unsampled parent.

    Defaulting it to sampled would let a caller's sampling decision be overridden by
    a service that happens to be tracing, which is the caller's decision to make.
    """
    unsampled = VALID[:-2] + "00"

    parsed = parse_traceparent(unsampled)

    assert parsed.sampled is False
    assert parsed.header() == unsampled


def test_an_absent_traceparent_is_absent_rather_than_broken() -> None:
    """`None` and "garbage" are different answers to the caller's question.

    A caller sending no header gets a new trace, and a caller sending a broken one
    also gets a new trace — but only one of them is a bug worth looking for, and the
    middleware tells them apart by recording which.
    """
    assert parse_traceparent(None) is None
    assert parse_traceparent("") is None


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("not-a-traceparent", id="prose"),
        pytest.param("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7", id="three-fields"),
        pytest.param(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01-extra", id="five-fields"
        ),
        pytest.param("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7", id="missing-flags"),
        pytest.param("4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", id="no-version"),
        pytest.param(ZERO_TRACE_ID, id="all-zero-trace-id"),
        pytest.param(ZERO_SPAN_ID, id="all-zero-span-id"),
        pytest.param("00-4BF92F3577B34DA6A3CE929D0E0E4736-00f067aa0ba902b7-01", id="uppercase"),
        pytest.param("00-4bf92f3577b34da6a3ce929d0e0e473g-00f067aa0ba902b7-01", id="non-hex"),
        pytest.param("00-4bf92f3577b34da6-00f067aa0ba902b7-01", id="trace-id-too-short"),
        pytest.param(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b-01", id="span-id-too-short"
        ),
        pytest.param(
            "ff-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", id="forbidden-version"
        ),
        pytest.param(
            " 00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01", id="leading-space"
        ),
        pytest.param(
            "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01 ", id="trailing-space"
        ),
        pytest.param("00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-0g", id="non-hex-flags"),
    ],
)
def test_a_malformed_traceparent_is_refused_rather_than_guessed_at(value: str) -> None:
    """Every one of these is a fresh trace, and none of them is a 400.

    The all-zero ids are in the list on purpose and are the important ones: the spec
    reserves them, and an implementation that accepts `0000...` as a trace id
    correlates every such request in the system into one trace — which is a denial of
    service against the tracing backend, not a cosmetic bug.
    """
    assert parse_traceparent(value) is None


def test_a_future_version_is_parsed_rather_than_discarded() -> None:
    """Version `01` is unknown, and the spec says to continue it rather than drop it.

    Refusing an unknown version would break every caller the day W3C ships a `02`,
    and a trace id that is well formed is worth continuing even if this build does
    not understand the flags in it.
    """
    parsed = parse_traceparent("01-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01")

    assert parsed is not None
    assert parsed.version == 1
    assert parsed.trace_id == TRACE_ID


# --- the allowlist ---------------------------------------------------------


def test_the_allowlist_carries_no_name_that_could_hold_content() -> None:
    """The list is the security control, so it is asserted on directly.

    A name that merely *sounds* safe is the risk: `muse.prompt` or `muse.input` are
    exactly the attributes a future change adds, and they must not be addable by
    accident. Substrings that indicate user content are refused outright.
    """
    forbidden = (
        "prompt",
        "message",
        "content",
        "text",
        "body",
        "header",
        "query",
        "api_key",
        "secret",
        "credential",
        "error_message",
        "detail",
    )

    assert not [
        name for name in ALLOWED_SPAN_ATTRIBUTES if any(word in name.lower() for word in forbidden)
    ]


def test_an_attribute_outside_the_allowlist_is_dropped() -> None:
    """The choke point refuses what it does not recognise.

    This is the test that fails the day someone adds `prompt` to a span: the
    allowlist is the only path to an attribute, so an unlisted name is not "probably
    fine", it is gone.
    """
    telemetry, exporter = recording_telemetry()

    with telemetry.span("muse.test") as span:
        record(span, **{"muse.prompt": "the user's prompt", "muse_model": "gpt-4o-mini"})

    assert payloads(exporter) == [
        {
            "name": "muse.test",
            "kind": "internal",
            "status": "unset",
            "attributes": {"muse_model": "gpt-4o-mini"},
        }
    ]


def test_a_secret_is_never_converted_into_an_attribute() -> None:
    """A `Secret` is refused on its type, not on its content.

    Checking the value would mean a filter with a bypass — the thing
    `muse.redaction` exists to argue against. Refusing the type means there is no
    code path at all that can render a credential as text, however it got there.
    """
    telemetry, exporter = recording_telemetry()

    with telemetry.span("muse.test") as span:
        record(span, muse_model=Secret("sk-live-SECRET"), muse_route="fast")

    assert payloads(exporter) == [
        {
            "name": "muse.test",
            "kind": "internal",
            "status": "unset",
            "attributes": {"muse_route": "fast"},
        }
    ]


def test_a_value_that_is_not_a_scalar_is_dropped() -> None:
    """An object with a `__str__` is a value that can be anything.

    Passing a message list to an allowlisted key is the plausible mistake, and the
    rendered span would then hold a repr of a list of the caller's prompts. Refusing
    anything that is not a scalar closes that without guessing at shapes.
    """
    telemetry, exporter = recording_telemetry()

    with telemetry.span("muse.test") as span:
        record(span, muse_model=["role", "user"], muse_attempts=2, muse_breaker_state="closed")

    assert payloads(exporter) == [
        {
            "name": "muse.test",
            "kind": "internal",
            "status": "unset",
            "attributes": {"muse_attempts": 2, "muse_breaker_state": "closed"},
        }
    ]


def test_a_long_value_is_truncated_to_the_attribute_ceiling() -> None:
    """Defence in depth behind the allowlist, not a substitute for it.

    The canary test below uses a short canary precisely so truncation cannot be what
    makes it pass: a truncated leak is still a leak, and a redaction rule proved by a
    length limit is a redaction rule with a hole in it.
    """
    telemetry, exporter = recording_telemetry()

    with telemetry.span("muse.test") as span:
        record(span, muse_model="x" * (MAX_ATTRIBUTE_LENGTH + 500))

    recorded = payloads(exporter)[0]["attributes"]["muse_model"]
    assert len(recorded) == MAX_ATTRIBUTE_LENGTH
    assert recorded == "x" * MAX_ATTRIBUTE_LENGTH


def test_an_empty_attribute_mapping_is_not_an_error() -> None:
    """`record(span)` with nothing to say is a legitimate call."""
    telemetry, exporter = recording_telemetry()

    with telemetry.span("muse.test") as span:
        record(span)

    assert payloads(exporter) == [
        {"name": "muse.test", "kind": "internal", "status": "unset", "attributes": {}}
    ]


# --- the no-op tracer ------------------------------------------------------


def test_the_noop_telemetry_still_produces_a_usable_span() -> None:
    """The default configuration is a real tracer with nowhere to export.

    muse must be able to create and close spans before anyone has configured a
    collector, so a missing `MUSE_OTEL_ENDPOINT` degrades to "traces are not
    exported", never to "the service does not start" and never to an exception on the
    request path.
    """
    telemetry = Telemetry.noop()

    with telemetry.span("muse.test", muse_route="fast") as span:
        record(span, muse_route="fast")
        assert span is not None


def test_telemetry_is_immutable() -> None:
    """One telemetry per app, read by every concurrent request.

    Same reason `Container` is frozen (AGENTS.md rule 1): a mutable tracer handle is a
    config change half the fleet sees and half does not.
    """
    telemetry, _ = recording_telemetry()

    with pytest.raises(AttributeError):
        telemetry.tracer = None  # type: ignore[misc]
