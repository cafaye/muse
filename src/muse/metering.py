"""Metering: every routed call becomes one `muse.tokens.consumed` outbox event.

The event is the reason this service exists — it is what `billing` will aggregate
into a customer's invoice, and the only record that a token was ever spent. Four
decisions are worth stating, and each has a test that would fail if it changed:

- **The envelope is validated before the insert.** core's outbox spec requires the
  payload to be validated before the row is written; a publisher that discovers at 3am
  it has been emitting a malformed type has already lost the events.
- **The insert is in a transaction.** core's rule is that a service never publishes an
  event outside a transaction that also wrote the state it describes. muse has no
  usage table — the outbox row *is* the usage record — so that transaction is the
  whole write, and the test asserts the statement happened at transaction depth 1
  rather than trusting that a context manager was entered.
- **A metering failure is not swallowed.** The completion has already happened and
  has already been paid for. Returning it to the caller anyway would hand them a 200
  and lose the spend, which is the exact failure this module exists to prevent. So the
  error propagates: the caller sees a 503, support sees an alert, and the tokens are a
  known loss rather than an invisible one.
- **The subject is core's reserved `platform` literal — and muse now knows better.**
  Since muse-06 the verified token carries an `account_id`, so the account this
  completion belongs to *is* known at the point the event is built. It is still not
  written, and that is deliberate rather than an oversight: core owns
  `schemas/events/muse/tokens/consumed.schema.json`, which closes `data` at exactly
  five fields and documents `subject: platform` as the state until D9. Putting the
  account on the envelope from a service would be a service publishing a contract core
  has not agreed to, and `billing` aggregates against the shape core published.

  So the gap is real and named: **every served completion is metered without the
  tenant it was spent by.** A consumer cannot attribute this spend to a customer from
  the event alone. The fix is a core change — D9, plus `account_id` in the payload
  schema — and it is recorded there and in this packet's report rather than driven
  through here. Changing the envelope is not this packet's decision to make.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from muse.contracts import SERVICE_NAME, TOKENS_CONSUMED, validate_event_type, validate_subject
from muse.db import Database
from muse.errors import MeteringError
from muse.router import RoutedCompletion

#: RFC3339, UTC, `Z`-suffixed. Not an epoch and not a local time: the column is
#: `timestamptz` and a value with an offset would be read back as a different instant
#: from the one the event claims.
_RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")

#: The subject for a token-consumption event while core's payload schema still says so.
#: core's reserved literal for an event with no single entity.
#:
#: muse *knows* the account since muse-06 — `require_bearer` refuses a token without
#: one — and still cannot put it here. See the module docstring: core owns the payload
#: schema, it closes `data` at five fields, and a service publishing an account on the
#: envelope would be publishing a contract core has not agreed to.
SUBJECT = "platform"

_INSERT = """
insert into outbox_events (id, event_type, source, subject, time, data)
values (%s, %s, %s, %s, %s, %s)
"""


@dataclass(frozen=True, slots=True)
class UsageEvent:
    """The `muse.tokens.consumed` envelope, ready to publish.

    Frozen: it is queued, retried and published later, and an envelope that changed
    between the commit and the publish would be a message that does not match the row
    it came from.
    """

    id: str
    type: str
    source: str
    subject: str
    time: str
    data: dict[str, object]
    #: core's envelope dialect. A field rather than a constant in `as_wire`, so a
    #: `UsageEvent` *is* the envelope and a test can assert the whole of it. Fixed by
    #: the schema, so it has a default and no caller sets it.
    specversion: str = "1.0"

    def as_wire(self) -> dict[str, object]:
        """The envelope as core's schema describes it.

        Every attribute, and nothing else: core sets `additionalProperties: false`, so
        an extra key is a contract change and a silently-added one is the kind nobody
        reviews.
        """
        return {
            "specversion": self.specversion,
            "id": self.id,
            "type": self.type,
            "source": self.source,
            "subject": self.subject,
            "time": self.time,
            "data": dict(self.data),
        }


def envelope_for(result: RoutedCompletion, *, now: str | None = None) -> UsageEvent:
    """Build the envelope for one routed completion.

    `now` is injectable so a test can assert an exact string; production leaves it
    alone and gets the wall clock. It is the time the *call* happened, not the time
    the row is inserted — a publisher that batched an hour of events would otherwise
    report them all as published at once, and a consumer reconstructing usage over a
    period would be reading a lie.
    """
    completion = result.completion
    moment = now if now is not None else _utc_now()
    if not _RFC3339.match(moment):
        raise MeteringError(f"{moment!r} is not an RFC3339 UTC timestamp")
    # The usage is checked before the cost, so the error names the field that is
    # actually wrong. Computing the cost first would raise from `usage_cost_micros`
    # with the same numbers and none of this module's context — and the difference
    # matters, because a negative count means a provider adapter is misreporting.
    if completion.tokens_in < 0 or completion.tokens_out < 0:
        raise MeteringError(
            f"refusing to meter negative usage: {completion.tokens_in} in, "
            f"{completion.tokens_out} out"
        )
    return UsageEvent(
        id=_new_id(),
        type=validate_event_type(TOKENS_CONSUMED),
        source=SERVICE_NAME,
        subject=validate_subject(SUBJECT),
        time=moment,
        data={
            "model": completion.model,
            "provider": completion.provider,
            "tokens_in": completion.tokens_in,
            "tokens_out": completion.tokens_out,
            "cost_micros": completion.cost_micros,
        },
    )


class Meter:
    """Writes one outbox row per routed completion.

    Holds only its database, and is constructed per call site, so there is no state to
    share between concurrent requests and no second thing for a test to stub.
    """

    def __init__(self, database: Database, *, now: Callable[[], str] | None = None) -> None:
        self._db = database
        self._now = now or _utc_now

    def now(self) -> str:
        """The timestamp this meter stamps events with.

        A method rather than a bare attribute so a caller can read the clock the meter
        is using — a test asserting `now` is injectable has no other way to check it
        took effect.
        """
        return self._now()

    async def record(self, result: RoutedCompletion) -> UsageEvent:
        """Meter one completion.

        In a transaction, and without catching anything. The caller of the router
        gets this error: the call was paid for, so losing its record silently is the
        one outcome that must not happen.
        """
        event = envelope_for(result, now=self._now())
        async with self._db.transaction() as tx:
            await tx.execute(
                _INSERT,
                (
                    event.id,
                    event.type,
                    event.source,
                    event.subject,
                    event.time,
                    json.dumps(event.as_wire()["data"]),
                ),
            )
        return event

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"


def _utc_now() -> str:
    """The wall clock, as RFC3339 UTC with a `Z`.

    `datetime.now(UTC)` rather than `utcnow()`: the naive call is deprecated, and the
    offset-aware one makes the `Z` below a fact rather than an assumption.
    """
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _new_id() -> str:
    """A UUID4 for the envelope.

    Generated *before* the insert, so the row and the message carry one identity: a
    republished row is recognisable as a duplicate by a consumer that already saw it.
    That is the whole mechanism behind at-least-once delivery being safe here.
    """
    import uuid

    return str(uuid.uuid4())
