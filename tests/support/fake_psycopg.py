"""Duck-typed stand-ins for the psycopg objects `PsycopgDatabase` touches.

`PsycopgDatabase` is a thin adapter over a *connection pool*, and a pool is what
makes the seam honest: one checked-out connection per transaction, so two
concurrent completions metering their tokens never share a transaction. These
fakes implement the handful of calls the adapter makes and record what they were
asked to do, which is how the tests assert on SQL text, parameters, and commit
behaviour rather than on behaviour the fake invented.

The row queue lives on the pool: every cursor draws from it in order, so a test
scripts the answer to a query by putting one row in the queue and leaving the
rest to `None`.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any


class FakeCursor:
    """Stands in for `psycopg.AsyncCursor`. Records one statement."""

    def __init__(
        self,
        rows: list[Mapping[str, Any]],
        log: list[tuple[str, tuple[Any, ...]]],
        row_factory: Any = None,
    ) -> None:
        self._rows = rows
        self._log = log
        self.closed = False
        self.row_factory = row_factory
        self.sql: str | None = None
        self.params: tuple[Any, ...] = ()

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self.sql = sql
        self.params = tuple(params)
        self._log.append((sql, self.params))

    async def fetchone(self) -> Mapping[str, Any] | None:
        return self._rows.pop(0) if self._rows else None

    def close(self) -> None:
        self.closed = True

    async def __aenter__(self) -> FakeCursor:
        return self

    async def __aexit__(self, *exc: object) -> None:
        self.close()


class FakeTransaction:
    """Stands in for `psycopg.AsyncTransaction`. Remembers whether it committed."""

    def __init__(self) -> None:
        self.committed: bool | None = None

    async def __aenter__(self) -> FakeTransaction:
        return self

    async def __aexit__(self, exc_type: object, *_: object) -> None:
        self.committed = exc_type is None


class FakeConnection:
    """Stands in for one checked-out `psycopg.AsyncConnection`."""

    def __init__(self, rows: list[Mapping[str, Any]]) -> None:
        self._rows = rows
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self.transactions: list[FakeTransaction] = []
        self.cursors: list[FakeCursor] = []

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> FakeCursor:
        cursor = self.cursor()
        await cursor.execute(sql, params)
        return cursor

    def cursor(self, *, row_factory: Any = None, **_: Any) -> FakeCursor:
        cursor = FakeCursor(self._rows, self.executed, row_factory)
        self.cursors.append(cursor)
        return cursor

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[FakeTransaction]:
        handle = FakeTransaction()
        self.transactions.append(handle)
        async with handle:
            yield handle


class FakePool:
    """Stands in for `psycopg_pool.AsyncConnectionPool`."""

    def __init__(self, rows: Sequence[Mapping[str, Any]] = ()) -> None:
        self.rows: list[Mapping[str, Any]] = list(rows)
        self.connections: list[FakeConnection] = []

    @asynccontextmanager
    async def connection(self) -> AsyncIterator[FakeConnection]:
        handle = FakeConnection(self.rows)
        self.connections.append(handle)
        yield handle

    @property
    def statements(self) -> list[str]:
        """Every SQL string sent through any checked-out connection, in order."""
        return [sql for connection in self.connections for sql, _ in connection.executed]

    @property
    def params(self) -> list[tuple[Any, ...]]:
        return [params for connection in self.connections for _, params in connection.executed]
