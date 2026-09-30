"""The database seam.

muse needs three things from postgres: run a statement, read one row, and open a
transaction. That is the whole `Database` protocol, and it is a protocol rather
than a pool type so the suite runs without a server (AGENTS.md rule 3: no socket
in tests).

`PsycopgDatabase` is the production implementation. It takes a *pool*, not a
connection, and that choice is the load-bearing one: the outbox rule is that an
event is inserted in the same transaction as the write it describes, which means
`transaction()` has to hand out an isolated handle. With a single shared
connection, two completions metering their tokens would either serialise on one
transaction or — worse — quietly share one, and the atomicity guarantee would be
fiction.

Stores are typed against `Database` and receive a transaction handle from their
caller rather than opening one themselves. The transaction boundary is a property
of the operation ("this write and this event commit together"), so it belongs at
the call site, not inside a repository.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from typing import Any, Protocol, runtime_checkable

from psycopg.rows import dict_row

#: Positional query parameters, in the order the placeholders appear.
Params = Sequence[Any]


@runtime_checkable
class Database(Protocol):
    """What muse needs from a database.

    Statements are written with `%s` placeholders and parameters passed
    separately. Nothing in this service builds SQL by interpolation, because a
    model name or a provider name reaches these functions and both are worth
    quoting.
    """

    async def execute(self, sql: str, params: Params = ()) -> None:
        """Run one statement and discard any rows."""
        ...

    async def fetchone(self, sql: str, params: Params = ()) -> Mapping[str, Any] | None:
        """Run one statement and return its first row as a mapping, or `None`."""
        ...

    def transaction(self) -> AbstractAsyncContextManager[Database]:
        """Open a transaction, yielding a handle bound to it.

        Used as `async with database.transaction() as tx:`. The handle commits on
        a clean exit and rolls back on any exception, so the caller writes the
        domain row and its event into the same `tx` and cannot commit one without
        the other.
        """
        ...


class PsycopgDatabase:
    """`Database` over a `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, pool: Any) -> None:
        # Typed `Any` on purpose: psycopg's generics are not worth importing the
        # pool package for, and the pool is duck-typed at test time anyway.
        self._pool = pool

    @classmethod
    async def open(  # pragma: no cover - the one call here that dials
        cls, dsn: str, *, min_size: int = 1, max_size: int = 8
    ) -> PsycopgDatabase:
        """Build a pool and open it.

        The one call in this module that opens a socket, and so the one function
        the suite cannot reach: AGENTS.md rule 3 is no socket in tests. Excluded on
        the `def` so the whole body goes, rather than three pragmas that a later
        edit can drift out of sync with.

        Everything it returns is exercised through `PsycopgDatabase(pool)` with a
        stand-in pool, so what is uncovered is the constructor and nothing else.
        """
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(dsn, min_size=min_size, max_size=max_size, open=False)
        await pool.open()
        return cls(pool)

    async def execute(self, sql: str, params: Params = ()) -> None:
        # An explicit cursor rather than `connection.execute(...)`, which returns
        # one the caller owns: a cursor left open per statement is how a long-lived
        # process exhausts its pool.
        async with self._pool.connection() as connection, connection.cursor() as cursor:
            await cursor.execute(sql, tuple(params))

    async def fetchone(self, sql: str, params: Params = ()) -> Mapping[str, Any] | None:
        async with (
            self._pool.connection() as connection,
            connection.cursor(row_factory=dict_row) as cursor,
        ):
            await cursor.execute(sql, tuple(params))
            return await cursor.fetchone()

    def transaction(self) -> _PoolTransaction:
        return _PoolTransaction(self._pool)


class _PoolTransaction:
    """A checked-out connection plus an open transaction, as a `Database`.

    Entering checks a connection out of the pool and begins a transaction on it;
    exiting ends the transaction and then returns the connection, in that order —
    so a rollback happens while the connection is still held rather than after it
    has gone back to the pool. `AsyncExitStack` is what guarantees that order and
    what forwards the exception to both scopes correctly; hand-rolling it means
    two `__aexit__` calls with a hand-counted argument list.
    """

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._stack = AsyncExitStack()
        self._connection: Any = None

    async def __aenter__(self) -> _PoolTransaction:
        self._connection = await self._stack.enter_async_context(self._pool.connection())
        await self._stack.enter_async_context(self._connection.transaction())
        return self

    async def _require_bound(self) -> Any:
        """The check that turns a use-before-`async with` into a clear message.

        Not defensive programming: a transaction handle that escapes its `async
        with` block would execute its statements on a connection that has already
        gone back to the pool, autocommitting them outside the transaction. That is
        precisely the atomicity guarantee the outbox depends on, so the failure is
        worth naming rather than tracing.
        """
        if self._connection is None:
            raise RuntimeError(
                "transaction handle used outside its `async with` block; the "
                "connection has been returned to the pool"
            )
        return self._connection

    async def __aexit__(self, *exc: object) -> bool | None:
        try:
            return await self._stack.__aexit__(*exc)
        finally:
            # The connection is back in the pool now, so anything still holding
            # this handle must be refused rather than silently autocommitting on a
            # connection someone else is about to use.
            self._connection = None

    async def execute(self, sql: str, params: Params = ()) -> None:
        connection = await self._require_bound()
        async with connection.cursor() as cursor:
            await cursor.execute(sql, tuple(params))

    async def fetchone(self, sql: str, params: Params = ()) -> Mapping[str, Any] | None:
        connection = await self._require_bound()
        async with connection.cursor(row_factory=dict_row) as cursor:
            await cursor.execute(sql, tuple(params))
            return await cursor.fetchone()

    def __repr__(self) -> str:
        """Says whether the handle is bound. An unbound handle whose `execute` is
        called is a `NoneType` error three frames from the mistake, and the repr is
        what a debugger or a log line shows."""
        return f"{type(self).__name__}(bound={self._connection is not None})"
