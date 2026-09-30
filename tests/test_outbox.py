"""The outbox publisher loop.

core owns the table, the loop's shape and the guarantee; this asserts that muse's
implementation keeps the guarantee. The properties, in the order they matter:

- **Ack, then mark.** `published_at` is set from the transport's acknowledgement, never
  before. A publish that was never acknowledged is an unpublished row, and the next
  pass republishes it under the same envelope `id` — which is what makes at-least-once
  delivery safe for a consumer.
- **The claim query is core's**, `for update skip locked` included, because that clause
  is what lets N replicas share one table. Asserted on the SQL text, since a rewritten
  claim query is the kind of change that is invisible in review and fatal under
  concurrency.
- **One row failing does not fail the batch.** The others are already on the bus, and
  rolling back their marks would republish them for nothing.
- **A failure increments `attempts` and grows the wait.** Exponential, capped. A broker
  that is down must not become a hot loop against a broker that is already down.

The transport is a stand-in that records what it was given, so no test needs a broker.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from muse.metering import Meter
from muse.outbox import (
    MAX_BACKOFF_SECONDS,
    STUCK_AFTER_SECONDS,
    InMemoryTransport,
    OutboxPublisher,
    OutboxRow,
    Transport,
    backoff_seconds,
    envelope_rows,
)
from muse.providers import Completion, Price

from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MOMENT = datetime(2026, 9, 30, 4, 19, 0, tzinfo=UTC)


def row(**overrides) -> OutboxRow:
    fields = {
        "id": "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
        "event_type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": MOMENT,
        "data": {"model": "gpt-4o-mini", "provider": "openai", "tokens_in": 12},
    }
    fields.update(overrides)
    return OutboxRow(**fields)


def database_with(*rows: OutboxRow) -> FakeDatabase:
    """A database whose outbox holds `rows`, as the claim query would return them."""
    database = FakeDatabase()
    for entry in rows:
        database.outbox.append(
            {
                "id": entry.id,
                "event_type": entry.event_type,
                "source": entry.source,
                "subject": entry.subject,
                "time": entry.time,
                "data": entry.data,
                "attempts": entry.attempts,
                "published_at": None,
            }
        )
    return database


def publisher(database: FakeDatabase, transport: Transport, **kwargs) -> OutboxPublisher:
    return OutboxPublisher(database, transport, **kwargs)


# --- the row ---------------------------------------------------------------


def test_a_row_renders_the_whole_envelope() -> None:
    """Every attribute core's schema requires, and the payload from `data` alone. The
    row's own columns carry the context, so the store and the contract cannot drift."""
    assert row().envelope() == {
        "specversion": "1.0",
        "id": "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
        "type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": "2026-09-30T04:19:00.000000Z",
        "data": {"model": "gpt-4o-mini", "provider": "openai", "tokens_in": 12},
    }


def test_the_time_is_normalised_to_utc() -> None:
    """A `timestamptz` rendered in the publisher's local zone would mean a different
    instant to a consumer in another one, and the row is a statement about when
    something happened."""
    from datetime import timedelta, timezone

    east = timezone(timedelta(hours=5))
    # 04:19 *in* +05:00 is 23:19 the previous day in UTC. Rendered in the publisher's own
    # zone the same value would be "04:19+05:00", which is a different instant to a
    # consumer in any other zone. Built in the eastern zone rather than converted into
    # it: `astimezone` moves the instant and keeps the wall clock reading, which is the
    # one case where a local rendering and a UTC one would agree.
    assert (
        row(time=datetime(2026, 9, 30, 4, 19, 0, tzinfo=east)).envelope()["time"]
        == "2026-09-29T23:19:00.000000Z"
    )


def test_a_naive_time_is_read_as_utc() -> None:
    """psycopg hands back an aware datetime for `timestamptz`, but a row read through
    any other path might not be, and a naive value must not be rendered as local time
    — that would silently shift every event by the publisher's offset."""
    assert row(time=datetime(2026, 9, 30, 4, 19, 0)).envelope()["time"].endswith("Z")


def test_the_payload_is_copied_not_aliased() -> None:
    """The envelope goes to a transport that may hold it. Aliasing the row's dict would
    let a consumer's serialisation mutate the row."""
    source = row()
    envelope = source.envelope()
    envelope["data"]["tokens_in"] = 999
    assert source.data["tokens_in"] == 12


def test_a_batch_renders_in_order() -> None:
    """The publisher's ordering guarantee, stated in one place rather than at a call
    site. Two events about the same subject reach a consumer in `created_at` order."""
    rows = (row(id="a"), row(id="b"), row(id="c"))
    assert [item["id"] for item in envelope_rows(rows)] == ["a", "b", "c"]


# --- the claim query -------------------------------------------------------


def test_the_claim_query_uses_skip_locked() -> None:
    """core: `for update skip locked` is what lets N replicas run this loop against one
    table. Without it a row another publisher holds is *waited* on, so a slow batch in
    one replica stalls the rest — and the clause is invisible in a review of a rewritten
    query."""
    assert "for update skip locked" in _claim_sql()


def _claim_sql() -> str:
    from muse.outbox import _CLAIM

    return _CLAIM.lower()


def test_the_claim_query_orders_by_created_at_then_id() -> None:
    """`id` in the ordering as well as `created_at`, so the order is total: rows
    written in one transaction share a `created_at`, and without a tiebreak two replicas
    could publish them in opposite orders."""
    sql = _claim_sql()
    assert "order by created_at, id" in sql
    assert "where published_at is null" in sql


def test_the_claim_query_is_limited() -> None:
    """A batch, not a row. One round trip per event does not survive a service that
    emits thousands of them."""
    assert "limit %s" in _claim_sql()


# --- publishing ------------------------------------------------------------


async def test_a_batch_publishes_every_row() -> None:
    database = database_with(row(id="a"), row(id="b"))
    transport = InMemoryTransport()

    assert await publisher(database, transport).publish_batch() == 2
    assert [subject for subject, _ in transport.published] == [
        "muse.tokens.consumed",
        "muse.tokens.consumed",
    ]


async def test_the_subject_is_the_event_type() -> None:
    """core: the `event_type` column *is* the NATS subject. No mapping table, no prefix
    rewriting — which is what lets a consumer subscribe to a type from the string
    alone."""
    database = database_with(row())
    transport = InMemoryTransport()
    await publisher(database, transport).publish_batch()
    assert transport.published[0][0] == "muse.tokens.consumed"


async def test_a_published_row_is_marked_published() -> None:
    """`published_at` is the only definition of "published", and it is set from the
    transport's acknowledgement."""
    database = database_with(row())
    await publisher(database, InMemoryTransport()).publish_batch()
    assert any("published_at = now()" in statement.sql for statement in database.statements)


async def test_the_mark_is_by_id() -> None:
    """One row marked, not the batch. A batch-wide mark would publish a row whose own
    publish failed."""
    database = database_with(row(id="only-this-one"))
    await publisher(database, InMemoryTransport()).publish_batch()
    marks = [s for s in database.statements if "published_at = now()" in s.sql]
    assert marks[0].params == ("only-this-one",)


async def test_a_failing_transport_leaves_the_row_unpublished() -> None:
    """The guarantee, stated as a test: a publish that was never acknowledged is an
    unpublished row, and the next pass republishes it under the same id."""
    database = database_with(row())
    transport = InMemoryTransport(fail=True)

    assert await publisher(database, transport).publish_batch() == 0
    assert not any("published_at = now()" in s.sql for s in database.statements)


async def test_a_failing_row_increments_its_attempts() -> None:
    """`attempts` is the input to the backoff and the signal an alert is built on."""
    database = database_with(row())
    await publisher(database, InMemoryTransport(fail=True)).publish_batch()
    assert any("attempts = attempts + 1" in s.sql for s in database.statements)


async def test_a_failure_does_not_stop_the_rest_of_the_batch() -> None:
    """The others are already on the bus; rolling back their marks would republish them
    for nothing. So one row failing is one row's problem."""
    database = database_with(row(id="bad"), row(id="good"))
    transport = InMemoryTransport()
    publisher_ = publisher(database, transport)
    publisher_._transport = _FailsFirst(transport, "bad")

    assert await publisher_.publish_batch() == 1
    assert [envelope["id"] for _, envelope in transport.published] == ["good"]


class _FailsFirst:
    """A transport that refuses one envelope id and passes the rest through."""

    def __init__(self, inner: InMemoryTransport, failing_id: str) -> None:
        self._inner = inner
        self._failing = failing_id

    async def publish(self, subject: str, envelope: dict) -> None:
        if envelope["id"] == self._failing:
            raise ConnectionError("broker unavailable")
        await self._inner.publish(subject, envelope)


async def test_the_same_id_is_republished_on_the_next_pass() -> None:
    """At-least-once is only safe because the id is stable. A regenerated id would make
    every retry a new event, and a consumer's dedupe table would grow without ever
    matching anything."""
    database = database_with(row())
    transport = InMemoryTransport(fail=True)
    failing = publisher(database, transport)
    await failing.publish_batch()

    # The broker comes back. The row is still unpublished, so it is claimed again.
    transport._fail = False
    assert await failing.publish_batch() == 1
    assert transport.published[0][1]["id"] == "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88"


async def test_an_empty_outbox_publishes_nothing() -> None:
    assert await publisher(FakeDatabase(), InMemoryTransport()).publish_batch() == 0


async def test_publishing_an_empty_batch_still_opens_a_transaction() -> None:
    """The claim is inside the transaction, so an empty pass is a transaction that
    commits having found nothing. The alternative — checking for work first — is a
    second query whose answer is stale by the time the transaction starts."""
    database = FakeDatabase()
    await publisher(database, InMemoryTransport()).publish_batch()
    assert database.commits == 1


async def test_a_batch_is_one_statement_per_row_plus_the_claim() -> None:
    """So a reviewer can count the round trips a batch costs, which is the number that
    decides whether the batch size is right."""
    database = database_with(row(id="a"), row(id="b"))
    await publisher(database, InMemoryTransport()).publish_batch()
    assert len(database.statements) == 3


# --- the backoff -----------------------------------------------------------


def test_the_backoff_starts_at_one_second() -> None:
    assert backoff_seconds(1) == 1.0


def test_the_backoff_doubles() -> None:
    assert [backoff_seconds(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]


def test_the_backoff_is_capped() -> None:
    """Without a cap, a broker that is down for a day produces waits that grow past any
    deployment's patience and the loop stops retrying at all.

    10,000 attempts is the case that matters: `2 ** 9999` is not a float at all, so a
    naive exponential raises `OverflowError` and the loop dies on exactly the day the
    cap exists for.
    """
    assert backoff_seconds(100) == MAX_BACKOFF_SECONDS
    assert backoff_seconds(10_000) == MAX_BACKOFF_SECONDS


def test_a_row_that_has_never_failed_waits_nothing() -> None:
    assert backoff_seconds(0) == 0.0


def test_the_stuck_threshold_is_an_hour() -> None:
    """A constant rather than a magic number inside the loop, so the alert someone
    eventually writes has a documented threshold to be written against."""
    assert STUCK_AFTER_SECONDS == 3600.0


# --- the loop --------------------------------------------------------------


async def test_the_loop_stops_when_told_to() -> None:
    """A publisher that only runs because a web worker happens to be up is a publisher
    that stops when traffic does, and an outbox nobody publishes grows until it is the
    largest table in the database.

    The transport is what stops the loop, not a second coroutine racing it. A
    coroutine approach depends on the loop yielding — and `publish` as written never
    does, so the test would spin forever rather than fail. A test that hangs instead of
    failing is a test that eventually gets deleted.
    """
    import asyncio

    stop = asyncio.Event()

    class StopsAfterOne:
        def __init__(self) -> None:
            self.published: list[str] = []

        async def publish(self, subject: str, envelope: dict) -> None:
            self.published.append(envelope["id"])
            stop.set()

    transport = StopsAfterOne()
    await publisher(database_with(row()), transport).run(stop=stop)
    assert len(transport.published) == 1


async def test_the_loop_sleeps_when_there_is_nothing_to_publish() -> None:
    """Otherwise an idle service spins on a query that returns nothing."""
    import asyncio

    stop = asyncio.Event()
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        stop.set()

    loop = publisher(FakeDatabase(), InMemoryTransport(), sleep=sleep)
    await loop.run(stop=stop)

    assert slept == [1.0]


async def test_the_loop_does_not_sleep_after_a_full_batch() -> None:
    """The next pass has work to do either way, so sleeping there would add a second of
    latency to the common case for no benefit.

    Asserted on the *order* of publishes and sleeps, and with two rows at a batch size
    of one, because a count cannot tell the two apart. With a single row a loop that
    slept after *every* pass would also sleep exactly once — just before the row went
    out, instead of after the pass that found nothing. Two rows make the two
    implementations produce different traces.

    The sleep is what stops the loop, so the test terminates on its own. A version that
    raced a task against a timeout would hang on any scheduler hiccup, and a test that
    hangs instead of failing is a test that eventually gets deleted.
    """
    import asyncio

    stop = asyncio.Event()
    trace: list[str] = []

    async def sleep(seconds: float) -> None:
        trace.append(f"sleep {seconds}")
        stop.set()

    await publisher(
        database_with(row(id="a"), row(id="b")),
        _Tracing(trace),
        batch_size=1,
        sleep=sleep,
    ).run(stop=stop)

    assert trace == ["publish", "publish", "sleep 1.0"]


class _Tracing:
    """A transport that records each publish into a shared trace.

    Shared rather than a list of its own, because the assertion is about the order of
    the publisher's *callbacks* — a publish, then a sleep, then another publish is a
    different answer from two publishes and then a sleep, and neither is visible in a
    per-object count.
    """

    def __init__(self, trace: list[str]) -> None:
        self._trace = trace

    async def publish(self, subject: str, envelope: dict) -> None:
        self._trace.append("publish")


# --- the end-to-end path ---------------------------------------------------


async def test_a_metered_completion_reaches_the_bus_intact() -> None:
    """The whole chain: a routed completion, metered into the outbox, published with
    its envelope unchanged. This is the test that would catch a payload field being
    renamed in one module and not the other."""
    from muse.router import RoutedCompletion

    database = FakeDatabase()
    completion = Completion(
        provider="openai",
        model="gpt-4o-mini",
        content="answered",
        tokens_in=12,
        tokens_out=8,
        price=Price(150, 600),
    )
    result = RoutedCompletion(
        completion=completion, route="fast", model="gpt-4o-mini", candidates_tried=1, attempts=1
    )
    await Meter(database, now=lambda: "2026-09-30T04:19:00.000000Z").record(result)

    # The fake stores what the insert wrote; give it a datetime so the publisher can
    # render it the way postgres would hand one back.
    for entry in database.outbox:
        entry["time"] = MOMENT
        entry["attempts"] = 0
    transport = InMemoryTransport()
    await OutboxPublisher(database, transport).publish_batch()

    subject, envelope = transport.published[0]
    assert subject == "muse.tokens.consumed"
    assert envelope["source"] == "muse"
    assert envelope["data"] == {
        "model": "gpt-4o-mini",
        "provider": "openai",
        "tokens_in": 12,
        "tokens_out": 8,
        "cost_micros": 7,
    }
    assert envelope["id"] == database.outbox[0]["id"]


def test_a_publisher_reprs_its_batch_size() -> None:
    """The batch size is the one number an operator tunes, and a `repr` that omits it
    is a `repr` that costs a debugger a step."""
    assert "batch_size=100" in repr(publisher(FakeDatabase(), InMemoryTransport()))


def test_a_default_publisher_uses_core_s_batch_range() -> None:
    """100-500 is core's stated range. Below it a busy service round-trips per event;
    above it the transaction holds row locks for too long."""
    from muse.outbox import DEFAULT_BATCH_SIZE

    assert 100 <= DEFAULT_BATCH_SIZE <= 500
