"""The outbox publisher's boot wiring: what the lifespan starts, under what condition,
and what is left running when the condition is not met.

The publisher's own behaviour is `tests/test_outbox.py`'s subject; this file holds the
question this packet was dispatched to answer — **does anything start the loop** — and
the properties that answer has to carry:

- **With `NATS_URL` set, a row reaches the bus.** Asserted against a stand-in broker
  *client*, not against a mocked-out function: the assertion is on the subject and the
  exact payload bytes handed to the JetStream publish call, which is the wire message.
  muse's suite opens no socket (AGENTS.md rule 3), so an in-process broker is out of
  reach; identity proves the same wiring against a real one in
  `cmd/identity/outbox_wiring_test.go`. The boundary is stated rather than glossed:
  what is *not* asserted here is a server storing the message.
- **With `NATS_URL` unset, no loop starts and no row is marked published.** The
  honest accumulation, and the shipped state.
- **Shutdown stops the loop and releases the connection**, because the lifespan's exit
  is this process's context, and a publisher that outlived the server would be a task
  writing to a pool that is about to close.
"""

from __future__ import annotations

import ast
import asyncio
import dataclasses
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest

from muse.main import Container, Settings, create_app, outbox_transport
from muse.metering import Meter
from muse.nats import NatsTransport
from muse.outbox import OutboxPublisher
from muse.providers import ProviderRegistry
from muse.providers.credentials import StaticCredentials
from muse.router import Router
from muse.routes import RetryPolicy, RouteTable
from muse.vault import Vault

from .support.fake_database import FakeDatabase
from .support.test_app import TEST_KEY, auth_for

pytestmark = [pytest.mark.anyio, pytest.mark.integration]

#: The row every test here drains, written the way `Meter` writes it.
MOMENT = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def outbox_row(**overrides: Any) -> dict[str, Any]:
    """One unpublished `outbox_events` row, as `from outbox_events` returns it."""
    fields: dict[str, Any] = {
        "id": "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
        "event_type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": MOMENT,
        "data": {
            "model": "gpt-4o-mini",
            "provider": "openai",
            "tokens_in": 12,
            "tokens_out": 8,
            "cost_micros": 7,
        },
        "attempts": 0,
        "published_at": None,
    }
    fields.update(overrides)
    return fields


def database_with(rows: list[dict[str, Any]] | None = None) -> FakeDatabase:
    database = FakeDatabase()
    for entry in rows or []:
        database.outbox.append(entry)
    return database


def envelope(**overrides: Any) -> dict[str, Any]:
    """The envelope the row above becomes, as the transport serialises it."""
    fields: dict[str, Any] = {
        "specversion": "1.0",
        "id": "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
        "type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": "2026-10-10T12:00:00.000000Z",
        "data": {
            "model": "gpt-4o-mini",
            "provider": "openai",
            "tokens_in": 12,
            "tokens_out": 8,
            "cost_micros": 7,
        },
    }
    fields.update(overrides)
    return fields


def container_for(database: FakeDatabase, settings: Settings, **overrides: Any) -> Container:
    """A container over `database`, with the settings under test.

    Readiness and the publisher need nothing from the routing half of the container —
    an empty registry and an empty table are the honest minimum — and `auth` is required
    rather than defaulted (AGENTS.md rule 20): a container that could be built without a
    verifier is a container whose `/v1/route` either skips verification or invents a
    permissive default.
    """
    registry = ProviderRegistry()
    table = RouteTable(version=1, defaults=RetryPolicy(), routes=())
    fields: dict[str, Any] = {
        "settings": settings,
        "database": database,
        "registry": registry,
        "routes": table,
        "router": Router(registry, table),
        "vault": Vault(database, TEST_KEY),
        "meter": Meter(database),
        "credentials": StaticCredentials({}),
        "auth": auth_for(),
    }
    fields.update(overrides)
    return Container(**fields)


def bus_settings(**overrides: Any) -> Settings:
    """Settings for a deployment with a broker wired."""
    fields: dict[str, Any] = {"nats_url": "nats://broker.cafaye.internal:4222"}
    fields.update(overrides)
    return Settings(**fields)


class StandInJetStream:
    """A JetStream session that records the publishes it is handed.

    Only the call muse's transport makes, so the test cannot pass by accident: a
    transport that published the wrong subject, or the wrong bytes, fails here rather
    than somewhere downstream.
    """

    def __init__(self, broker: StandInBroker) -> None:
        self._broker = broker

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float | None = None,
        stream: str | None = None,
        headers: Any = None,
    ) -> dict[str, Any]:
        if self._broker._fail:
            raise TimeoutError("no stream covers this subject")
        self._broker.published_timeout = timeout
        self._broker.published.append((subject, payload))
        # A JetStream ack, as the broker returns one.
        return {"stream": "cafaye", "seq": 1, "domain": None, "duplicate": False}


class StandInBroker:
    """A broker client: one session, closed on request, refusing publishes if told to."""

    def __init__(self, *, fail: bool = False, refuse_close: bool = False) -> None:
        self.published: list[tuple[str, bytes]] = []
        self.published_timeout: float | None = None
        self.sessions = 0
        self.closed = 0
        self._fail = fail
        self._refuse_close = refuse_close

    async def jetstream(self) -> StandInJetStream:
        self.sessions += 1
        return StandInJetStream(self)

    async def close(self) -> None:
        if self._refuse_close:
            raise ConnectionError("the broker refused the close")
        self.closed += 1


def stand_in_connect(broker: StandInBroker) -> Callable[[str], Awaitable[StandInBroker]]:
    """`connect`, as the transport wants it, over a stand-in broker.

    The URL is asserted rather than accepted, because "the transport dialled the URL it
    was configured with" is the one thing about the real dial this file can check.
    """

    async def connect(url: str) -> StandInBroker:
        assert url, "the transport must dial the URL it was configured with"
        return broker

    return connect


async def drained(broker: StandInBroker, database: FakeDatabase, *, want: int = 1) -> None:
    """Wait until the loop has published `want` rows, or fail.

    A deadline rather than a fixed sleep: the loop's one-second idle interval is the
    publisher's business, and a test that slept through it would be slower than the
    code it is waiting for (AGENTS.md rule 14).
    """
    for _ in range(600):
        if len(broker.published) >= want:
            return
        await asyncio.sleep(0.005)
    pytest.fail("the outbox loop did not publish within the poll interval; it is not running")


async def retried(database: FakeDatabase, sql: str) -> None:
    """Wait until the loop has run `sql`, or fail."""
    for _ in range(600):
        if any(sql in statement.sql for statement in database.statements):
            return
        await asyncio.sleep(0.005)
    pytest.fail(f"the outbox loop never ran {sql!r}; it is not running")


# --- the condition ---------------------------------------------------------


def test_no_nats_url_means_no_transport() -> None:
    """The shipped state. Rows accumulate, and `outbox_transport` answers `None` rather
    than leaving a reader to infer why from the wiring."""
    container = container_for(database_with(), bus_settings(nats_url=None))

    assert outbox_transport(container) is None


def test_no_database_means_no_transport_even_with_a_broker() -> None:
    """`NATS_URL` without `MUSE_DATABASE_URL` is a misconfiguration, and a loop whose
    claimer cannot transact would raise on every pass rather than drain anything.

    Built through the real `_UnavailableDatabase` the boot path constructs, because a
    sentinel named in a comment and never in a test is a sentinel nobody has looked at.
    """
    from muse.main import _UnavailableDatabase

    container = container_for(database_with(), bus_settings())
    stranded = dataclasses.replace(container, database=_UnavailableDatabase())

    assert outbox_transport(stranded) is None


def test_a_broker_and_a_database_produce_a_transport() -> None:
    """The positive half, so the two above are a refusal and not a stub that always
    answers `None` — the canary discipline `test_trace_propagation.py` names."""
    container = container_for(database_with(), bus_settings())

    transport = outbox_transport(container, connect=stand_in_connect(StandInBroker()))

    assert isinstance(transport, NatsTransport)


# --- the loop, through the app --------------------------------------------


async def test_the_lifespan_starts_the_loop_and_a_row_reaches_the_bus() -> None:
    """The packet's first Done-means, through the real wiring.

    A row is in the table, `NATS_URL` is set, the lifespan runs — and the stand-in
    broker holds the message. What is asserted is the *wire*: the subject muse published
    to and the payload as bytes. A test asserting `publish_batch` was called would pass
    against a loop that never touched the transport.
    """
    database = database_with([outbox_row()])
    broker = StandInBroker()
    app = create_app(container=container_for(database, bus_settings()))

    import muse.main as main

    main.nats_connect = stand_in_connect(broker)
    try:
        async with app.router.lifespan_context(app):
            assert isinstance(app.state.outbox_publisher, OutboxPublisher)
            await drained(broker, database)
    finally:
        main.nats_connect = _original_connect(main)

    subject, payload = broker.published[0]

    # The subject is the event type — core's rule, no mapping table, no prefix
    # rewriting — and it is inside the `muse.>` filter the stack's stream must cover.
    assert subject == "muse.tokens.consumed"
    assert subject.startswith("muse.")

    # The payload is the envelope, byte for byte, so a consumer parsing it gets exactly
    # what the outbox row described.
    assert json.loads(payload) == {
        "specversion": "1.0",
        "id": "0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",
        "type": "muse.tokens.consumed",
        "source": "muse",
        "subject": "platform",
        "time": "2026-10-10T12:00:00.000000Z",
        "data": {
            "model": "gpt-4o-mini",
            "provider": "openai",
            "tokens_in": 12,
            "tokens_out": 8,
            "cost_micros": 7,
        },
    }

    # `published_at` is the contract's own definition of published, set from the
    # broker's ack — the observable half of "it reached the bus", and the reason the
    # next pass does not repeat it.
    marks = [s for s in database.statements if "published_at = now()" in s.sql]
    assert marks, "the row was published but never marked, so the next pass repeats it"
    assert marks[0].params == ("0198f1c2-7a41-7c3b-9d55-2f0b6a1e4c88",)


async def test_the_lifespan_does_not_start_a_loop_without_a_broker() -> None:
    """The packet's second Done-means. Without `NATS_URL` nothing starts and — the half
    that matters — no row is claimed and none is marked, because a row marked by a loop
    that published nowhere is the failure identity's `NoopPublisher` comment warns
    about."""
    database = database_with([outbox_row()])
    app = create_app(container=container_for(database, bus_settings(nats_url=None)))

    async with app.router.lifespan_context(app):
        # A full poll interval's worth, so a loop that *had* started would have had the
        # chance to claim the row.
        await asyncio.sleep(0.05)

    assert getattr(app.state, "outbox_publisher", None) is None
    assert not any("published_at is null" in s.sql for s in database.statements)
    assert not any("published_at = now()" in s.sql for s in database.statements)


async def test_shutdown_stops_the_loop_and_closes_the_connection() -> None:
    """The loop is tied to the process's context, so a shutdown cancels it and releases
    what it opened. A publisher that outlived the server would be a task writing to a
    pool that is about to close, and a connection nobody closes keeps the event loop's
    exit pending."""
    broker = StandInBroker()
    database = database_with([outbox_row()])
    app = create_app(container=container_for(database, bus_settings()))

    import muse.main as main

    main.nats_connect = stand_in_connect(broker)
    try:
        async with app.router.lifespan_context(app):
            # Drain first, so the transport has a session to release: the half of the
            # shutdown being asserted is that what the loop opened is closed, not that a
            # transport that never connected closes twice.
            await drained(broker, database)
            assert app.state.outbox_publisher is not None
    finally:
        main.nats_connect = _original_connect(main)

    assert broker.closed == 1
    assert broker.published, "the row never drained, so this test would pass on any close"
    assert getattr(app.state, "outbox_publisher", None) is None


async def test_a_refused_publish_leaves_the_row_claimable() -> None:
    """The failure direction, and the reason a missing stream is loud rather than
    silent: the broker refuses, the row is not marked, `attempts` climbs, and the event
    leaves the table only once a stream covers its subject. identity's
    `TestNewAppWithNATSURLButNoStreamLeavesTheRowUnpublished` is the same assertion
    against a real broker."""
    database = database_with([outbox_row()])
    broker = StandInBroker(fail=True)
    app = create_app(container=container_for(database, bus_settings()))

    import muse.main as main

    main.nats_connect = stand_in_connect(broker)
    try:
        async with app.router.lifespan_context(app):
            await retried(database, "attempts = attempts + 1")
    finally:
        main.nats_connect = _original_connect(main)

    assert broker.published == []
    assert not any("published_at = now()" in s.sql for s in database.statements)
    assert any("attempts = attempts + 1" in s.sql for s in database.statements)


def _original_connect(main: Any) -> Any:
    """The real dial, so the stand-in does not outlive the test that installed it.

    A test that replaced `main.nats_connect` and left it there would be a test that
    handed every later test in the process a fake broker.
    """
    import nats

    return nats.connect


# --- the transport ---------------------------------------------------------


async def test_the_transport_publishes_the_row_it_was_handed() -> None:
    """The transport's own seam: one publish, one subject, the envelope as bytes. This is
    the furthest a suite with no socket can see, and it is asserted on the arguments
    rather than on a call having happened."""
    broker = StandInBroker()
    transport = NatsTransport("nats://broker.test:4222", connect=stand_in_connect(broker))

    await transport.publish("muse.tokens.consumed", envelope())
    await transport.close()

    assert broker.published == [("muse.tokens.consumed", json.dumps(envelope()).encode("utf-8"))]
    assert broker.closed == 1


async def test_the_transport_passes_a_timeout_to_the_broker() -> None:
    """A publish with no budget is a publish that can hold the loop — and its
    transaction — for the life of the process, so the budget is asserted at the client
    call rather than assumed to be the client's default."""
    broker = StandInBroker()
    transport = NatsTransport(
        "nats://broker.test:4222", connect=stand_in_connect(broker), timeout=2.5
    )

    await transport.publish("muse.tokens.consumed", envelope())

    assert broker.published_timeout == 2.5


async def test_a_failed_publish_drops_the_connection_and_reraises() -> None:
    """The failure path has to *raise*: the loop marks a row published only from a
    return, so a transport that swallowed a failure would mark rows the broker never
    saw. And it drops the session, so the next pass dials instead of reusing a socket
    nobody is listening on."""
    broker = StandInBroker(fail=True)
    transport = NatsTransport("nats://broker.test:4222", connect=stand_in_connect(broker))

    with pytest.raises(TimeoutError):
        await transport.publish("muse.tokens.consumed", envelope())

    assert broker.closed == 1
    assert "connected=False" in repr(transport)


async def test_a_close_that_raises_does_not_mask_the_shutdown() -> None:
    """Swallowed and logged: this runs while the process is going down, and there is
    nothing to be done about a refused close."""
    broker = StandInBroker(refuse_close=True)
    transport = NatsTransport("nats://broker.test:4222", connect=stand_in_connect(broker))

    await transport.publish("muse.tokens.consumed", envelope())
    await transport.close()  # must not raise

    assert "connected=False" in repr(transport)


async def test_a_transport_that_never_published_closes_without_a_broker() -> None:
    """`close` on a transport with no session is a no-op, so the lifespan's finally block
    does not have to know whether a publish ever happened."""
    transport = NatsTransport("nats://broker.test:4222", connect=stand_in_connect(StandInBroker()))

    await transport.close()

    assert "connected=False" in repr(transport)


async def test_one_transport_reuses_its_connection() -> None:
    """One connection for the transport's life, and not one per publish: a publish per
    round trip is a broker handshake on every event, which is the cost the whole
    outbox exists to avoid.

    The broker counts what it was asked for, so a transport that dialled twice would
    show two sessions.
    """
    broker = StandInBroker()
    transport = NatsTransport("nats://broker.test:4222", connect=stand_in_connect(broker))

    await transport.publish("muse.tokens.consumed", envelope())
    await transport.publish("muse.tokens.consumed", envelope())

    assert len(broker.published) == 2
    assert broker.sessions == 1  # one connection for the transport's life
    assert broker.closed == 0  # nothing closed it yet, and nothing needed to
    assert "connected=True" in repr(transport)


# --- the shape of the wiring -----------------------------------------------


def _names_in(tree: ast.AST) -> set[str]:
    """Every identifier a module refers to, however it refers to it.

    `ast.Name`, `ast.Attribute` **and `ast.alias`** — and the alias half is the one
    that matters, because the first draft of this guard collected the first two only
    and was broken on purpose to check it: a bare `from muse.outbox import
    InMemoryTransport` in `main.py` sailed straight through, since an import binds an
    `alias` node and not a `Name` one. A guard that cannot see the spelling it is
    guarding against is not a guard (rule 23's discipline, and the same one that found
    24 uncollected tests in muse-09).
    """
    names = {node.id for node in ast.walk(tree) if isinstance(node, ast.Name)}
    names |= {node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)}
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            names.add(node.name)
            if node.asname:
                names.add(node.asname)
    return names


def test_no_transport_that_publishes_nowhere_is_wired() -> None:
    """identity's fifth design point, and muse keeps it more strongly than identity
    does: there is no no-op publisher here at all.

    The tree is the oracle for a claim about the tree — the same technique as
    `tests/test_alias_delegation.py`'s AST walk and `test_tenant_scoping.py`'s SQL
    walk. `InMemoryTransport` is a test double, so its name appearing anywhere in
    `src/muse/main.py` would mean the shipped wiring had found a way to drain the
    outbox without a broker.

    Parsed rather than grepped, because a comment explaining why something is *not*
    wired is exactly where the name would otherwise appear — and collected through
    `_names_in`, which was wrong once and was fixed by breaking it.
    """
    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parent.parent / "src" / "muse" / "main.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))

    assert "InMemoryTransport" not in _names_in(tree)
    # `NatsTransport` is built in one place, by `outbox_transport`, and that is the
    # function the lifespan calls. A second construction site would be a second
    # condition nobody documented.
    constructions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "NatsTransport"
    ]
    assert len(constructions) == 1


def test_the_publisher_is_constructed_only_by_the_lifespan() -> None:
    """A loop in the request path would hold a database transaction open across a
    network call for every completion muse serves, which is the arrangement the outbox
    exists to avoid. So exactly one module constructs it, and it is the composition
    root — `api.py`, the request path, never mentions the publisher at all."""
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parent.parent / "src" / "muse"
    builders = []
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        if "OutboxPublisher" in _names_in(tree):
            builders.append(path.name)

    assert builders == ["main.py"], (
        f"the outbox publisher is constructed in {builders}; it belongs in the "
        "composition root's lifespan and nowhere else"
    )


async def test_the_loop_serves_requests_while_it_drains() -> None:
    """The reason this is a lifespan task and not something in the request path: the
    publisher runs with no request waiting on it, and the transport it holds is the
    lifespan's, closed when the app stops."""
    database = database_with()
    broker = StandInBroker()
    app = create_app(container=container_for(database, bus_settings()))

    import muse.main as main

    main.nats_connect = stand_in_connect(broker)
    try:
        async with app.router.lifespan_context(app):
            publisher = app.state.outbox_publisher
            assert isinstance(publisher, OutboxPublisher)
            assert publisher is not None
            # The transport on state is closed by the shutdown below, so this is a
            # check that the app owns what it started.
            assert repr(publisher) == "OutboxPublisher(batch_size=100)"
    finally:
        main.nats_connect = _original_connect(main)
