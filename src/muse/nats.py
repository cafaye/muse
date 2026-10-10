"""The JetStream transport: muse's `Transport`, over NATS.

`muse.outbox` describes what a transport is — one method, `publish`, returning when
the broker has taken the message — and deliberately ships no implementation. This is
that implementation, and it is the only thing about the broker the publisher knows:
a second bus, or a change to this one, touches this file and not the loop.

Four decisions, each with a test:

- **JetStream, and not core NATS.** The outbox contract sets `published_at` from the
  broker's *acknowledgement*, so a transport that returns before the broker has stored
  the message cannot honour it. `js.publish` is a request that blocks until the broker
  acknowledges, so a return without an exception **is** the ack the loop marks from.
- **The connection is made on first use, not at boot.** A broker that is down when
  the process starts is not a boot failure: the rows are the durable store, they
  accumulate while the broker is away, and they drain the moment it returns. Dialling
  at boot would couple muse's availability to the bus's, which is the same argument
  that keeps `/healthz` from consulting a dependency.
- **A publish is bounded.** `timeout` is passed to the client, which applies it to
  the broker's reply. Without a budget, a broker that neither acknowledges nor errors
  holds the loop — and its transaction — for as long as the process lives.
- **A failed publish drops the connection.** A timeout or a reset leaves a session
  that may be half-dead, so it is closed and the next pass dials again. Keeping it
  would make every later publish fail against a socket nobody is listening on.

The subject is the event type, and core's rule is explicit: no mapping table, no
prefix rewriting. A row with type `muse.tokens.consumed` is published on
`muse.tokens.consumed` and nothing else.

The stream is the stack's, not this module's. A publish to a subject no stream matches
is refused by the broker, and that refusal is correct: it leaves the row claimable
with `attempts` climbing, so the event leaves the table only once a stream exists that
covers its subject. **The stack must define a stream whose subject filter covers
`muse.>`, or every publish fails and no event ever leaves the table.** The failure is
loud — `attempts` grows, the warning names the refusal — which is the direction a
misconfiguration should fail in.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any, Protocol

import nats

logger = logging.getLogger("muse.nats")

#: How long one publish waits for the broker's acknowledgement. Ten seconds, and the
#: number identity uses for the same budget: long enough that a slow-but-healthy
#: broker never trips it (a healthy one acks in milliseconds), short enough that a
#: silent one costs the loop one bounded wait per event. The worst case for a full
#: batch against a fully silent broker is `batch_size * timeout`, loudly, with every
#: row left claimable.
DEFAULT_PUBLISH_TIMEOUT_SECONDS = 10.0


class JetStream(Protocol):
    """What this transport needs of a broker session — one method.

    Structural, so a test can hand the transport a stand-in rather than a broker, and
    so the only thing asserted about the client is the call muse makes on it.
    """

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float | None = None,
        stream: str | None = None,
        headers: Any = None,
    ) -> Any:
        """Publish `payload` on `subject`, returning once the broker acks."""
        ...


class BrokerConnection(Protocol):
    """A connection to the broker, and the two things done with one."""

    async def jetstream(self) -> JetStream:
        """The JetStream context this transport publishes through."""
        ...

    async def close(self) -> None:
        """Release the connection. Not an error to call twice."""
        ...


class NatsTransport:
    """Moves one envelope onto the bus, and returns when the broker has it.

    Not started by anything and owning no loop: `muse.outbox.OutboxPublisher` drives
    it. Holds the URL and the dial rather than an open connection, because the dial
    is lazy — see the module docstring — and a transport constructed at boot that
    held a socket would make a broker outage a boot outage.
    """

    def __init__(
        self,
        url: str,
        *,
        connect: Callable[[str], Awaitable[BrokerConnection]] | None = None,
        timeout: float = DEFAULT_PUBLISH_TIMEOUT_SECONDS,
    ) -> None:
        self._url = url
        # The injection point. Production gets `nats.connect`; a test gets a stand-in
        # that records what it was handed, which is how the suite asserts what would
        # go on the wire without opening a socket (AGENTS.md rule 3).
        self._connect = connect or nats.connect
        self._timeout = timeout
        self._connection: BrokerConnection | None = None
        self._jetstream: JetStream | None = None

    async def publish(self, subject: str, envelope: dict[str, Any]) -> None:
        """Publish `envelope` on `subject`, or raise to report a failure.

        Returns only after the broker's acknowledgement, which is the whole reason
        the publisher may mark the row from it. The payload is the envelope
        serialised once, here, so the bytes on the wire are the bytes this service
        built — a consumer that parses them gets exactly the envelope the outbox row
        described.
        """
        jetstream = await self._session()
        payload = json.dumps(envelope).encode("utf-8")
        try:
            await jetstream.publish(subject, payload, timeout=self._timeout)
        except Exception:
            # The connection may be half-dead, and every later publish through it
            # would fail against a socket nobody is listening on. Drop it and let the
            # next pass dial again; the row stays claimable either way.
            await self._discard()
            raise

    async def close(self) -> None:
        """Release the broker connection, if one was ever opened.

        Safe to call on a transport that never published, and safe to call twice. A
        close that raises is swallowed and logged: this runs during shutdown, there is
        nothing to be done about a refused close, and it must not replace the reason
        the process is going down.
        """
        await self._discard()

    async def _session(self) -> JetStream:
        """The live JetStream context, dialling on the first publish.

        One connection for the transport's life. The loop is a single task, so there
        is no concurrent publish to serialise here; and a lock would be a second
        thing to get right for a caller that does not exist.
        """
        if self._jetstream is None:
            connection = await self._connect(self._url)
            self._jetstream = await connection.jetstream()
            self._connection = connection
        return self._jetstream

    async def _discard(self) -> None:
        """Forget the connection and close it. Shared by the failure path and `close`
        so a refused close cannot mask the refusal that caused it."""
        connection, self._connection, self._jetstream = self._connection, None, None
        if connection is None:
            return
        try:
            await connection.close()
        except Exception:
            logger.warning("closing the broker connection raised; the session is dropped anyway")

    def __repr__(self) -> str:
        """Says whether a session is open. An operator reading a log line wants to know
        whether this transport is connected, and the URL is not a thing to print."""
        return f"{type(self).__name__}(connected={self._jetstream is not None})"
