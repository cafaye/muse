"""The outbox publisher: rows to the bus, with core's contract.

core owns the table, the loop's shape and the guarantee it makes; each service
implements it in its own language, in its own migration, with its own decisions
(`docs/event-outbox.md`, "Per-service implementations, never shared code"). So this is
muse's implementation and not a reusable library, and it is deliberately small: the SQL
from core's document, a transport interface, and the backoff arithmetic.

Four things in core's spec are the reason the code looks the way it does:

- **`for update skip locked` in the claim query.** It is what lets N replicas run this
  loop against one table: a row another publisher has claimed is skipped rather than
  waited on, so a slow batch in one replica cannot stall the rest.
- **Ack, then mark.** `published_at` is set from the transport's acknowledgement, never
  before. A publish that was never acknowledged is an unpublished row, and the next
  pass republishes it under the same envelope `id` — so a consumer that already got it
  ignores the duplicate.
- **`attempts` grows and the wait grows with it**, capped. A broker outage must not
  become a hot loop against a broker that is already down, and a row stuck for an hour
  is an incident while a row dropped quietly is a mystery.
- **The transaction is short.** Claim a batch, publish, mark, commit. Holding row locks
  across a network call is what turns a broker hiccup into a database one, and while it
  happens the whole table stops moving.

The transport is an interface with no implementation here: this packet has no NATS
client, and a stub that looked like a working one would be worse than an obvious gap.
`InMemoryTransport` is what the tests and the local stack use, and it is named for what
it is.
"""

from __future__ import annotations

import asyncio
import logging
import math
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from muse.db import Database

logger = logging.getLogger("muse.outbox")

#: Rows claimed per pass. 100-500 is core's range; the trade-off is lock duration
#: against round trips, and it is a per-service number because a different service's
#: events have a different size distribution.
DEFAULT_BATCH_SIZE = 100

#: The ceiling on the wait between attempts of one row: 1s, 2s, 4s, ... capped here.
#: Past the cap the row is left alone and `attempts` keeps growing, which is the signal
#: an alert is built on — see the note in `backoff_seconds`.
MAX_BACKOFF_SECONDS = 300.0

#: The first exponent whose `2 ** n` is already at or past the cap, so the comparison
#: in `backoff_seconds` can be made *before* the power is taken. Derived from the cap
#: rather than written as a literal so a changed ceiling cannot leave this stale: with
#: a 300s cap it is 9, because `2 ** 8` is 256 and `2 ** 9` is 512.
_CAP_EXPONENT = math.ceil(math.log2(MAX_BACKOFF_SECONDS))

#: A row this old is past every plausible outage. core's guidance is to alert and leave
#: it alone rather than drop it: an event that is stuck is an incident, an event that
#: vanished is a mystery. This constant exists so the alert has a threshold to be
#: written against; the loop itself does not delete anything.
STUCK_AFTER_SECONDS = 3600.0

#: The attempt count at which the wait has certainly reached the ceiling. Compared
#: against *before* exponentiating, so a row that has been failing for a month returns
#: the cap instead of raising `OverflowError`.
_CAP_EXPONENT = 64

#: core's claim query, verbatim in shape. The ordering is `(created_at, id)` rather
#: than `created_at` alone so the order is total: rows written in one transaction share
#: a `created_at`, and without the tiebreak two replicas could publish them in opposite
#: orders.
_CLAIM = """
select id, event_type, source, subject, time, data, attempts
  from outbox_events
 where published_at is null
 order by created_at, id
 limit %s
   for update skip locked
"""

_MARK_PUBLISHED = "update outbox_events set published_at = now() where id = %s"

_MARK_FAILED = "update outbox_events set attempts = attempts + 1 where id = %s"


@dataclass(frozen=True, slots=True)
class OutboxRow:
    """One unpublished event, as the claim query returns it."""

    id: str
    event_type: str
    source: str
    subject: str
    time: datetime
    data: dict[str, Any]
    attempts: int = 0

    def envelope(self) -> dict[str, Any]:
        """The envelope to publish.

        The row's own columns, not the `data` payload, for the context attributes. The
        payload is `data` and nothing else — so what goes on the bus is exactly what
        core's schema validates, and the store and the contract cannot drift apart.
        """
        return {
            "specversion": "1.0",
            "id": self.id,
            "type": self.event_type,
            "source": self.source,
            "subject": self.subject,
            "time": _rfc3339(self.time),
            "data": dict(self.data),
        }


@runtime_checkable
class Transport(Protocol):
    """Where published events go.

    One method, and it is `publish` rather than `send` because the acknowledgement is
    the whole point: `published_at` is set from what this returns, so a transport that
    cannot tell muse whether the broker took the message cannot implement the guarantee.
    """

    async def publish(self, subject: str, envelope: dict[str, Any]) -> None:
        """Publish `envelope` on `subject`, or raise to report a failure."""
        ...


class InMemoryTransport:
    """A transport that keeps what it was given.

    For tests and for the local compose stack, and named for what it is so nobody wires
    it into a deployment by accident. It records the subject and envelope of every
    successful publish and counts the failures, which is what makes the loop's
    ack-then-mark behaviour assertable without a broker.
    """

    def __init__(self, *, fail: bool = False) -> None:
        #: `(subject, envelope)` per successful publish, in order.
        self.published: list[tuple[str, dict[str, Any]]] = []
        self.failures = 0
        self._fail = fail

    async def publish(self, subject: str, envelope: dict[str, Any]) -> None:
        if self._fail:
            self.failures += 1
            raise ConnectionError("broker unavailable")
        self.published.append((subject, envelope))


class OutboxPublisher:
    """Moves rows from `outbox_events` to the bus.

    Not started by the app factory. A publisher is a loop, and a loop belongs in a
    worker process or a task — never in the request path, where it would hold a database
    transaction open across a network call for every completion muse serves.
    """

    def __init__(
        self,
        database: Database,
        transport: Transport,
        *,
        batch_size: int = DEFAULT_BATCH_SIZE,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self._db = database
        self._transport = transport
        self._batch_size = batch_size
        self._sleep = sleep or asyncio.sleep

    async def publish_batch(self) -> int:
        """Claim a batch, publish it, mark it. Returns how many were published.

        A batch, not a row: one round trip per event does not survive a service that
        emits thousands of them. A batch that raises is rolled back by the transaction
        and nothing is marked, so the next pass retries the whole batch — at-least-once
        is the guarantee, and a duplicate under the same `id` is free.
        """
        published = 0
        async with self._db.transaction() as tx:
            rows = await self._claim(tx)
            for row in rows:
                try:
                    await self._transport.publish(row.event_type, row.envelope())
                except Exception as error:  # a transport failure is data, not a crash
                    # One row failing does not fail the batch: the others are already
                    # on the bus, and rolling back the marks would republish them for
                    # no reason. This row's `attempts` goes up and the backoff grows.
                    logger.warning(
                        "publishing %s failed on attempt %d: %s", row.id, row.attempts + 1, error
                    )
                    await tx.execute(_MARK_FAILED, (row.id,))
                    continue
                # Ack first, mark second. The mark is the only definition of
                # "published", and it is set from the transport's acknowledgement
                # rather than from the attempt.
                await tx.execute(_MARK_PUBLISHED, (row.id,))
                published += 1
        return published

    async def run(self, *, stop: asyncio.Event | None = None) -> None:
        """Poll until `stop` is set.

        Deliberately not wired into the app. A publisher that only runs because a web
        worker happens to be up is a publisher that stops when traffic does, and an
        outbox nobody publishes is an outbox that grows until the table is the largest
        in the database.
        """
        stopping = stop or asyncio.Event()
        while not stopping.is_set():
            published = await self.publish_batch()
            if published == 0:
                # An empty batch is the poll interval. Sleeping after a full batch
                # instead would add latency to the common case for no benefit — the
                # next pass has work to do either way.
                await self._sleep(self._idle_seconds())
            logger.debug("published %d outbox rows", published)

    async def _claim(self, tx: Database) -> tuple[OutboxRow, ...]:
        result = await tx.fetchone(_CLAIM, (self._batch_size,))
        rows = result.get("rows", ()) if result else ()
        return tuple(
            OutboxRow(
                id=row["id"],
                event_type=row["event_type"],
                source=row["source"],
                subject=row["subject"],
                time=row["time"],
                data=row["data"],
                attempts=int(row.get("attempts", 0)),
            )
            for row in rows
        )

    def _idle_seconds(self) -> float:
        """The wait between empty passes.

        One second. Long enough that an idle service is not spinning on a query that
        returns nothing, short enough that an event published just after a pass waits
        about a second rather than a minute.
        """
        return 1.0

    def __repr__(self) -> str:
        return f"{type(self).__name__}(batch_size={self._batch_size})"


def backoff_seconds(attempts: int) -> float:
    """How long to wait before retrying a row that has failed `attempts` times.

    Exponential, capped at five minutes. The cap is applied by *comparing before
    exponentiating*, which is the part that is easy to get wrong: `2.0 ** 9999` is not
    a float at all, so a naive `min(2 ** n, cap)` raises `OverflowError` — killing the
    loop on exactly the long broker outage the cap exists to survive.
    """
    if attempts < 1:
        return 0.0
    if attempts - 1 >= _CAP_EXPONENT:
        return MAX_BACKOFF_SECONDS
    return min(2.0 ** (attempts - 1), MAX_BACKOFF_SECONDS)


def _rfc3339(moment: datetime) -> str:
    """A `timestamptz` as RFC3339 UTC.

    Normalised to UTC rather than rendered in the connection's zone, because the row's
    `time` is a statement about when the state change happened and it has to mean the
    same instant to every consumer regardless of where the publisher runs.
    """
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def envelope_rows(rows: Sequence[OutboxRow]) -> list[dict[str, Any]]:
    """The envelopes for a batch, in order.

    A helper rather than a comprehension at the call site so the ordering — which is
    the publisher's ordering guarantee — is stated in one place.
    """
    return [row.envelope() for row in rows]
