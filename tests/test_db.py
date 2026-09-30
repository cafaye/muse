"""The database seam.

muse touches postgres in two places (the vault, the outbox) and needs three
operations from a driver. That surface is a `Protocol` rather than a concrete
pool so the suite runs without a server — AGENTS.md rule 3: no socket in tests.

`PsycopgDatabase` is still covered rather than excluded: it is exercised against a
duck-typed stand-in for `psycopg_pool.AsyncConnectionPool`, so the adapter's own
logic (parameter passing, row shape, cursor cleanup, transaction nesting) is
proven without opening a socket. Only `open()`, the one call that dials, is
excluded, and it says why in a `pragma` comment.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from muse.db import Database, PsycopgDatabase

from .support.fake_psycopg import FakePool

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

SELECT_ONE = "select 1 as ok"
INSERT_SECRET = "insert into vault_secrets (provider, ciphertext) values (%s, %s)"
DELETE_SECRET = "delete from vault_secrets where provider = %s"
READ_SECRET = "select ciphertext from vault_secrets where provider = %s"


async def test_execute_sends_the_sql_unchanged() -> None:
    pool = FakePool()
    await PsycopgDatabase(pool).execute(DELETE_SECRET, ("openai",))
    assert pool.statements == [DELETE_SECRET]


async def test_execute_passes_the_params_positionally() -> None:
    pool = FakePool()
    await PsycopgDatabase(pool).execute(SELECT_ONE, (1, "two", None))
    assert pool.params == [(1, "two", None)]


async def test_execute_defaults_to_no_params() -> None:
    pool = FakePool()
    await PsycopgDatabase(pool).execute(SELECT_ONE)
    assert pool.params == [()]


async def test_execute_closes_its_cursor() -> None:
    """A cursor is a server-side resource; one left open per statement is how a
    long-lived process exhausts its pool."""
    pool = FakePool()
    await PsycopgDatabase(pool).execute(SELECT_ONE)
    assert all(cursor.closed for cursor in pool.connections[0].cursors)


async def test_fetchone_returns_the_queued_row() -> None:
    pool = FakePool(rows=[{"ok": 1}])
    assert await PsycopgDatabase(pool).fetchone(SELECT_ONE) == {"ok": 1}


async def test_fetchone_returns_none_when_the_query_matched_nothing() -> None:
    pool = FakePool()
    assert await PsycopgDatabase(pool).fetchone(SELECT_ONE) is None


async def test_fetchone_asks_for_dict_rows() -> None:
    """Tuple rows would make every call site unpack by position, so a migration
    that reordered a select would silently change what a caller reads."""
    pool = FakePool(rows=[{"ok": 1}])
    await PsycopgDatabase(pool).fetchone(SELECT_ONE)
    assert pool.connections[0].cursors[0].row_factory is not None


async def test_fetchone_closes_its_cursor() -> None:
    pool = FakePool(rows=[{"ok": 1}])
    await PsycopgDatabase(pool).fetchone(SELECT_ONE)
    assert all(cursor.closed for cursor in pool.connections[0].cursors)


async def test_fetchone_sends_the_params_unchanged() -> None:
    pool = FakePool(rows=[{"ciphertext": b"\x00\x01"}])
    await PsycopgDatabase(pool).fetchone(READ_SECRET, ("openai",))
    assert pool.params == [("openai",)]


async def test_bytes_params_reach_the_driver_as_bytes() -> None:
    """AES-GCM output is `bytes`. A driver handed the repr would store the repr of
    a ciphertext, which decrypts to nothing and looks like a corrupt vault."""
    pool = FakePool()
    seal = b"\x00\x01\x02nonce-and-ciphertext"
    await PsycopgDatabase(pool).execute(INSERT_SECRET, ("openai", seal))
    assert pool.params == [("openai", seal)]


async def test_row_sequences_are_accepted_as_params() -> None:
    """`in (%s, %s, %s)` style params arrive as a list from a comprehension; the
    seam must not insist on a tuple."""
    pool = FakePool()
    providers: Sequence[str] = ["openai", "anthropic"]
    await PsycopgDatabase(pool).execute(DELETE_SECRET, providers)
    assert pool.params == [("openai", "anthropic")]


async def test_transaction_commits_on_clean_exit() -> None:
    pool = FakePool()
    async with PsycopgDatabase(pool).transaction():
        pass
    assert pool.connections[0].transactions[0].committed is True


async def test_transaction_rolls_back_and_reraises_on_error() -> None:
    """The outbox rule: if the work fails, the event never existed."""
    pool = FakePool()
    with pytest.raises(RuntimeError, match="boom"):
        async with PsycopgDatabase(pool).transaction():
            raise RuntimeError("boom")
    assert pool.connections[0].transactions[0].committed is False


async def test_the_transaction_closes_before_the_connection_goes_back() -> None:
    """Order matters on the failure path: a rollback that happens after the
    connection is back in the pool is a rollback on someone else's transaction."""
    pool = FakePool()
    with pytest.raises(RuntimeError):
        async with PsycopgDatabase(pool).transaction():
            raise RuntimeError("boom")
    assert pool.connections[0].transactions[0].committed is False
    assert len(pool.connections) == 1


async def test_statements_inside_a_transaction_write_through_it() -> None:
    pool = FakePool()
    async with PsycopgDatabase(pool).transaction() as tx:
        await tx.execute(INSERT_SECRET, ("openai", b"\x00"))
    assert pool.statements == [INSERT_SECRET]
    assert pool.params == [("openai", b"\x00")]


async def test_a_fetch_inside_a_transaction_works() -> None:
    pool = FakePool(rows=[{"ciphertext": b"\x00\x01"}])
    async with PsycopgDatabase(pool).transaction() as tx:
        assert await tx.fetchone(READ_SECRET, ("openai",)) == {"ciphertext": b"\x00\x01"}


async def test_a_transaction_holds_one_connection() -> None:
    pool = FakePool()
    async with PsycopgDatabase(pool).transaction() as tx:
        await tx.execute(SELECT_ONE)
        await tx.execute(SELECT_ONE)
    assert len(pool.connections) == 1


async def test_concurrent_transactions_get_separate_connections() -> None:
    """The property a pool exists for: two completions metering at the same time
    must not share a transaction, and must not serialise on one either."""
    pool = FakePool()
    database = PsycopgDatabase(pool)

    async def meter(n: int) -> None:
        async with database.transaction() as tx:
            await tx.execute(INSERT_SECRET, ("openai", n))

    await asyncio.gather(meter(1), meter(2))
    assert len(pool.connections) == 2
    assert all(len(connection.transactions) == 1 for connection in pool.connections)


async def test_the_handle_satisfies_the_protocol_stores_are_typed_against() -> None:
    database: Database = PsycopgDatabase(FakePool())
    await database.execute(SELECT_ONE)
    async with database.transaction() as tx:
        await tx.execute(SELECT_ONE)


async def test_a_statement_outside_a_transaction_is_allowed() -> None:
    """Reads on the request path do not need a transaction; a single statement is
    atomic by itself."""
    pool = FakePool(rows=[{"ok": 1}])
    assert await PsycopgDatabase(pool).fetchone(SELECT_ONE) is not None
    assert pool.connections[0].transactions == []


async def test_a_handle_used_outside_its_block_is_refused() -> None:
    """A handle that escaped its `async with` would run its statements on a
    connection already back in the pool — autocommitting them outside the
    transaction, which is the atomicity the outbox exists to guarantee."""
    pool = FakePool()
    handle = PsycopgDatabase(pool).transaction()
    async with handle:
        pass
    with pytest.raises(RuntimeError, match="outside its `async with` block"):
        await handle.execute(SELECT_ONE)
    assert pool.statements == []


async def test_a_handle_that_never_entered_is_refused() -> None:
    pool = FakePool()
    handle = PsycopgDatabase(pool).transaction()
    with pytest.raises(RuntimeError, match="outside its `async with` block"):
        await handle.fetchone(SELECT_ONE)


async def test_the_handle_repr_says_whether_it_is_bound() -> None:
    pool = FakePool()
    handle = PsycopgDatabase(pool).transaction()
    assert "bound=False" in repr(handle)
    async with handle as tx:
        assert "bound=True" in repr(tx)
