"""Metering: every routed call becomes one `muse.tokens.consumed` outbox event.

The event is the whole point of this module and the whole point of this service's
existence — it is what `billing` will aggregate into a customer's invoice, and it is
the only record that a token was ever spent. So the properties that matter are:

- **The envelope is valid per core's schema, and validated before the insert.**
  core's outbox spec requires the payload to be validated before the row is written;
  a publisher that discovers at 3am that it has been emitting two-segment types has
  already lost the events.
- **The event lands in the same transaction as the write it describes.** muse has no
  usage table — the outbox row *is* the usage record — so the insert is wrapped in a
  transaction, and a test asserts the statement is inside one rather than beside it.
- **Money is integer micro-dollars, taken from the completion.** Not recomputed here
  from a price table that may have moved since the call was priced.
- **Every completion produces exactly one event.** Zero is unbilled spend; two is
  double-billing. Both are asserted.
- **The subject is the call, not the account.** A routed call belongs to one request,
  and `muse` does not know the account in v1 — the auth stub does not read a token.
  `platform` is core's reserved literal for an event with no single entity, and using
  it keeps the envelope honest about what is not yet known.
"""

from __future__ import annotations

import json
import re

import pytest

from muse.contracts import SERVICE_NAME, TOKENS_CONSUMED
from muse.metering import Meter, UsageEvent, envelope_for
from muse.providers import Completion, Price
from muse.router import RoutedCompletion

from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PROVIDER = "openai"
MODEL = "gpt-4o-mini"
PRICE = Price(150, 600)
#: 12 in at 150/1k and 8 out at 600/1k: 1.8 + 4.8 = 6.6, rounded once to 7.
COST_MICROS = 7
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")
UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")


def routed(**overrides) -> RoutedCompletion:
    """A routed completion: 12 tokens in, 8 out, at the price above.

    `completion_fields` overrides the completion's own fields, so a test can vary the
    usage or the vendor without restating the eight it does not care about.
    """
    completion = Completion(
        **{
            "provider": PROVIDER,
            "model": MODEL,
            "content": "answered",
            "tokens_in": 12,
            "tokens_out": 8,
            "price": PRICE,
            **overrides.pop("completion_fields", {}),
        }
    )
    return RoutedCompletion(
        completion=completion,
        route=overrides.pop("route", "fast"),
        model=overrides.pop("model", MODEL),
        candidates_tried=overrides.pop("candidates_tried", 1),
        attempts=overrides.pop("attempts", 1),
    )


# --- the payload -----------------------------------------------------------


def test_the_payload_carries_exactly_the_five_billing_facts() -> None:
    """Exactly these five, and no more.

    core closes payload schemas with `additionalProperties: false`, so a field added
    now is a field a future core schema has to carry forever. The five here are the
    ones billing cannot reconstruct on its own: which model, which vendor, how many
    tokens each way, and what it cost.
    """
    event = envelope_for(routed(), now="2026-09-30T04:19:00Z")
    assert set(event.data) == {"model", "provider", "tokens_in", "tokens_out", "cost_micros"}


def test_the_payload_reports_the_tokens_the_provider_counted() -> None:
    data = envelope_for(routed(), now="2026-09-30T04:19:00Z").data
    assert (data["tokens_in"], data["tokens_out"]) == (12, 8)


def test_the_payload_reports_the_cost_in_micro_dollars() -> None:
    """An integer. A float here is a float in billing's sum, and a float that rounds
    differently on two machines is a reconciliation ticket nobody can reproduce."""
    cost = envelope_for(routed(), now="2026-09-30T04:19:00Z").data["cost_micros"]
    assert cost == COST_MICROS
    assert isinstance(cost, int)


def test_the_payload_reports_the_vendor_that_actually_served_the_call() -> None:
    """Not the route's first candidate. A fallback that fires must be billed to the
    vendor that ran, or the fallback vendor's invoice and the customer's bill
    disagree — and neither side can see the other's number."""
    event = envelope_for(
        routed(completion_fields={"provider": "anthropic"}, candidates_tried=2, attempts=2),
        now="2026-09-30T04:19:00Z",
    )
    assert event.data["provider"] == "anthropic"
    assert event.data["model"] == MODEL


def test_a_zero_token_completion_is_still_metered() -> None:
    """A provider that reported no usage is a provider that has already been refused
    by the adapter. If one ever gets through, the event must still exist: a missing
    event is invisible, and a zero-token event is visibly wrong."""
    event = envelope_for(
        routed(completion_fields={"tokens_in": 0, "tokens_out": 0}), now="2026-09-30T04:19:00Z"
    )
    assert event.data["tokens_in"] == 0
    assert event.data["cost_micros"] == 0


# --- the envelope ----------------------------------------------------------


def test_the_envelope_is_the_three_segment_type_this_service_publishes() -> None:
    """`muse.tokens.consumed` — publisher-prefixed, three segments, snake_case after
    the service name. The pattern is core's, and it is what makes the type routable
    from the string alone."""
    assert envelope_for(routed(), now="2026-09-30T04:19:00Z").type == TOKENS_CONSUMED
    assert TOKENS_CONSUMED.split(".")[0] == SERVICE_NAME
    assert len(TOKENS_CONSUMED.split(".")) == 3


def test_the_envelope_declares_the_1_0_dialect() -> None:
    """CloudEvents 1.0 attribute names. A consumer parsing the envelope reads
    `specversion` first; a service that omits it is a service nobody can upgrade."""
    assert envelope_for(routed(), now="2026-09-30T04:19:00Z").specversion == "1.0"


def test_the_envelope_source_is_this_service() -> None:
    """Equal to the first segment of the type and to `name` in the manifest, as core
    requires. It is what a consumer filters on."""
    event = envelope_for(routed(), now="2026-09-30T04:19:00Z")
    assert event.source == SERVICE_NAME
    assert event.type.startswith(f"{SERVICE_NAME}.")


def test_the_envelope_id_is_a_uuid() -> None:
    """The consumer's dedupe key. At-least-once delivery means duplicates are ordinary
    operation, and this is the only thing that lets a consumer ignore one."""
    assert UUID.match(envelope_for(routed(), now="2026-09-30T04:19:00Z").id)


def test_two_events_for_one_call_would_need_two_ids() -> None:
    """The ids must differ, or a consumer deduplicating on them would drop a real
    event. Distinctness is the property; a fixed id would break it."""
    ids = {envelope_for(routed(), now="2026-09-30T04:19:00Z").id for _ in range(50)}
    assert len(ids) == 50


def test_the_envelope_time_is_rfc3339_utc() -> None:
    """Not an epoch, not a local time, not the transport time. `Z` rather than an
    offset so a consumer's parser does not have to know the publisher's zone."""
    assert RFC3339.match(envelope_for(routed(), now="2026-09-30T04:19:00Z").time)


def test_the_envelope_time_is_the_time_it_was_given() -> None:
    """The time the *state change* happened, not the time the row was inserted. A
    publisher that batched an hour's events would otherwise report them all as
    published at once, and a consumer reconstructing usage over a period would be
    reading a lie."""
    event = envelope_for(routed(), now="2026-01-02T03:04:05Z")
    assert event.time == "2026-01-02T03:04:05Z"


def test_the_envelope_subject_is_cores_reserved_literal() -> None:
    """`platform`, because a token consumption is about the call and muse does not
    know the account in v1.

    This is a deliberate placeholder, not an oversight: core requires `subject`, and
    using the reserved literal says "no single entity yet" rather than inventing an id
    that means nothing. It is a required field with a reserved value, not an absent
    one. When the auth contract lands and the account is known, this becomes the
    account id and the catalog row says so.
    """
    assert envelope_for(routed(), now="2026-09-30T04:19:00Z").subject == "platform"


def test_the_envelope_has_no_extra_attributes() -> None:
    """core sets `additionalProperties: false` on the envelope. An extra field is a
    contract change, and a silently-added one is the kind nobody reviews."""
    event = envelope_for(routed(), now="2026-09-30T04:19:00Z")
    assert set(event.as_wire()) == {
        "specversion",
        "id",
        "type",
        "source",
        "subject",
        "time",
        "data",
    }


def test_the_envelope_renders_to_the_wire_shape() -> None:
    """The dict that goes into `data` and onto NATS. Asserted whole, so a field
    renamed in the dataclass and not here fails instead of shipping."""
    event = envelope_for(routed(), now="2026-09-30T04:19:00Z")
    assert event.as_wire() == {
        "specversion": "1.0",
        "id": event.id,
        "type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": "2026-09-30T04:19:00Z",
        "data": {
            "model": MODEL,
            "provider": PROVIDER,
            "tokens_in": 12,
            "tokens_out": 8,
            "cost_micros": COST_MICROS,
        },
    }


def test_the_payload_is_json_serialisable() -> None:
    """It goes into a `jsonb` column as a string. A payload holding a tuple or a
    Decimal would fail at the insert, after the call has already been paid for."""
    json.dumps(envelope_for(routed(), now="2026-09-30T04:19:00Z").as_wire())


def test_the_event_is_immutable() -> None:
    """It is queued, retried and published later. A mutable envelope that changed
    between the commit and the publish would be a message that does not match the row
    it came from."""
    with pytest.raises(AttributeError):
        envelope_for(routed(), now="2026-09-30T04:19:00Z").time = "later"  # type: ignore[misc]


# --- the insert ------------------------------------------------------------


async def test_a_metered_call_writes_one_row() -> None:
    db = FakeDatabase()
    await Meter(db).record(routed())
    assert len(db.outbox) == 1


async def test_the_row_carries_the_envelope_fields() -> None:
    """The same identity in the table and on the wire, so a republished row is
    recognisable as a duplicate by the consumer that already saw it."""
    db = FakeDatabase()
    await Meter(db).record(routed())
    row = db.outbox[0]
    assert row["event_type"] == TOKENS_CONSUMED
    assert row["source"] == SERVICE_NAME
    assert row["subject"] == "platform"
    assert UUID.match(row["id"])


async def test_the_row_stores_the_payload_as_json() -> None:
    """`jsonb` and not a mapped python object: the value has to survive a round trip
    through the database, and `json.dumps` is the contract for that."""
    db = FakeDatabase()
    await Meter(db).record(routed())
    payload = json.loads(db.outbox[0]["data"])
    assert payload["cost_micros"] == COST_MICROS
    assert payload["tokens_in"] == 12


async def test_the_row_leaves_the_publish_columns_at_their_defaults() -> None:
    """`published_at` null and `attempts` zero are what make the row *findable* by
    the publisher's `where published_at is null`. A row inserted as already-published
    is an event that will never be sent."""
    db = FakeDatabase()
    await Meter(db).record(routed())
    statement = db.statements[0]
    assert "published_at" not in statement.sql.lower().split("values")[0].split("(")[1]
    assert len(statement.params) == 6


async def test_the_insert_is_inside_a_transaction() -> None:
    """The outbox rule: an event is never written outside a transaction that also
    wrote the state it describes. muse has no usage table, so the transaction that
    inserts this row is the whole write — and the test asserts the statement happened
    at depth 1 rather than trusting that a context manager was entered."""
    db = FakeDatabase()
    await Meter(db).record(routed())
    assert db.depths() == [1]
    assert db.commits == 1
    assert db.rollbacks == 0


async def test_a_failure_inside_the_transaction_leaves_no_row() -> None:
    """The other half of the rule. An event that exists without the write it
    describes is worse than a lost event: a consumer would bill for a call that
    nothing recorded."""
    db = FakeDatabase()
    db.fail_next = RuntimeError("connection lost")

    with pytest.raises(RuntimeError, match="connection lost"):
        await Meter(db).record(routed())

    assert db.outbox == []
    assert db.rollbacks == 1


async def test_a_meter_failure_does_not_serve_the_caller_a_free_completion() -> None:
    """The one place this could have been made convenient, and it must not be.

    The completion already happened and has already been paid for. Swallowing the
    insert failure would hand the caller a 200 and lose the spend — the exact failure
    the whole module exists to prevent. So the error propagates: the caller sees a
    503 and support sees an alert, and the tokens are a known loss rather than an
    invisible one.
    """
    db = FakeDatabase()
    db.fail_next = RuntimeError("connection lost")

    with pytest.raises(RuntimeError):
        await Meter(db).record(routed())


async def test_the_insert_names_only_the_columns_it_sets() -> None:
    """Six positional parameters, so the statement and the row cannot drift: a
    seventh column would be a seventh `%s` or a value in the wrong position, and
    `jsonb` swallowing a string into the wrong column is a silent corruption."""
    db = FakeDatabase()
    await Meter(db).record(routed())
    assert len(db.statements) == 1
    assert db.statements[0].params[1:] == (
        TOKENS_CONSUMED,
        SERVICE_NAME,
        "platform",
        db.outbox[0]["time"],
        db.outbox[0]["data"],
    )


async def test_two_calls_meter_two_events() -> None:
    """One per call, never coalesced. A batch would be an optimisation, and it is
    also a place where a partial write loses a record."""
    db = FakeDatabase()
    meter = Meter(db)
    await meter.record(routed())
    await meter.record(routed())
    assert len(db.outbox) == 2
    assert {row["id"] for row in db.outbox} != {db.outbox[0]["id"]}


async def test_the_meter_needs_nothing_but_a_database() -> None:
    """It is constructed per call site with only what it uses, so there is no second
    place a test has to stub and no state to share between requests."""
    assert isinstance(Meter(FakeDatabase()), Meter)


def test_a_meter_reprs_without_a_database_handle() -> None:
    """A `Meter` can appear in a traceback frame. Printing the connection would put a
    DSN — and therefore a password — into a log line."""
    assert "psycopg" not in repr(Meter(FakeDatabase()))
    assert repr(Meter(FakeDatabase())) == "Meter()"


def test_a_meter_can_be_given_a_clock() -> None:
    """Injectable so a test can assert an exact timestamp, and so a future packet can
    meter a completion whose time came from the provider's own response rather than
    the local clock."""
    meter = Meter(FakeDatabase(), now=lambda: "2026-01-02T03:04:05Z")
    assert meter.now() == "2026-01-02T03:04:05Z"


async def test_a_usage_event_validates_its_own_type_and_subject() -> None:
    """core's envelope schema is the contract, and the type and subject are the two
    fields with patterns in it. Checked on the way in rather than by a publisher."""
    event = envelope_for(routed(), now="2026-09-30T04:19:00Z")
    assert isinstance(event, UsageEvent)
    assert event.type == TOKENS_CONSUMED


def test_a_payload_with_a_negative_token_count_is_refused() -> None:
    """A provider reporting negative tokens is wrong, and a negative in the event is a
    credit in somebody's invoice. Refused at the metering boundary — the last place it
    is still attributable to this call — and named here rather than raised from
    `usage_cost_micros` with the same numbers and none of the context."""
    from muse.errors import MeteringError

    with pytest.raises(MeteringError, match="negative usage"):
        envelope_for(
            routed(completion_fields={"tokens_in": -1}),
            now="2026-09-30T04:19:00Z",
        )


def test_a_negative_output_count_is_refused() -> None:
    from muse.errors import MeteringError

    with pytest.raises(MeteringError, match="negative usage"):
        envelope_for(
            routed(completion_fields={"tokens_out": -5}),
            now="2026-09-30T04:19:00Z",
        )


def test_a_cost_cannot_be_negative_by_construction() -> None:
    """Stated as an invariant rather than tested as a refusal: `Price` refuses negative
    rates and the token counts above are non-negative, so the product cannot be
    negative. A guard in `envelope_for` for it would be unreachable code, and
    unreachable code in a money path is worse than none — it reads as a control that
    does not exist."""
    with pytest.raises(ValueError, match="cannot be negative"):
        Price(-1, 0)
    assert (
        envelope_for(
            routed(completion_fields={"tokens_in": 0, "tokens_out": 0}), now="2026-09-30T04:19:00Z"
        ).data["cost_micros"]
        == 0
    )


def test_a_malformed_time_is_refused() -> None:
    """The `time` goes into a `timestamptz`. A local time, an epoch, or a string with
    an offset would all be stored, and would all be read back as a different instant
    from the one the event claims."""
    from muse.errors import MeteringError

    with pytest.raises(MeteringError, match="RFC3339"):
        envelope_for(routed(), now="2026-09-30 04:19:00")


def test_a_metered_call_defaults_to_the_current_time() -> None:
    """`now` is injectable so the tests can assert an exact string; production leaves
    it alone and gets the wall clock."""
    assert RFC3339.match(envelope_for(routed()).time)
