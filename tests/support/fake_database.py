"""An in-memory `Database` for the suite.

The house rule is no socket in tests, so the store tests need a `Database` that
actually stores. This one does — dispatching on distinctive substrings of the SQL
rather than parsing it, and recording every statement it was asked to run.

Dispatching on substrings is a deliberate trade. A fake that parsed SQL would be a
second implementation of the query, and the two would drift. Matching on
`from vault_secrets` and `into outbox_events` means the fake cannot disagree with
the real SQL about what it means — only about how it is phrased. So the vault and
metering tests *also* assert the exact SQL and parameters, which is what catches a
change in the phrasing. Between the two, a real query change fails a test.

`depth` is the transaction nesting level at the time of the call, which is how the
same-transaction rule is asserted: two statements at the same depth inside one
`transaction()` block, and a rolled-back block leaving nothing behind.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

#: Substrings the tests' queries are matched on. Kept as data so a renamed table or a
#: rewritten query shows up as one clear failure rather than as a fake that quietly
#: returns nothing.
VAULT_TABLE = "vault_secrets"
OUTBOX_TABLE = "outbox_events"


class Statement:
    """One statement the fake was asked to run."""

    def __init__(self, sql: str, params: tuple[Any, ...], depth: int) -> None:
        self.sql = sql
        self.params = params
        self.depth = depth

    def __repr__(self) -> str:
        return f"Statement({self.sql!r}, {self.params!r}, depth={self.depth})"


class FakeDatabase:
    """A `Database` backed by dictionaries.

    Not a test double for postgres — it is a store that happens to be reached through
    the same seam production uses, so the code under test is the real vault code
    rather than a mock of it.
    """

    def __init__(self) -> None:
        self.vault: dict[str, dict[str, Any]] = {}
        self.outbox: list[dict[str, Any]] = []
        #: Every statement, in order, tagged with its transaction depth.
        self.statements: list[Statement] = []
        self.commits = 0
        self.rollbacks = 0
        self._depth = 0
        #: When set, the next statement raises it. Used to prove a write and its
        #: event roll back together.
        self.fail_next: BaseException | None = None

    # --- the Database protocol --------------------------------------------

    async def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        self._record(sql, params)
        self._maybe_fail()
        if VAULT_TABLE in sql:
            self._write_vault(sql, tuple(params))
        elif OUTBOX_TABLE in sql:
            self._write_outbox(sql, tuple(params))

    async def fetchone(self, sql: str, params: Sequence[Any] = ()) -> Mapping[str, Any] | None:
        self._record(sql, params)
        self._maybe_fail()
        if VAULT_TABLE in sql:
            return self._read_vault(sql, tuple(params))
        if OUTBOX_TABLE in sql:
            return self._read_outbox(sql, tuple(params))
        if "select 1" in sql:
            return {"ok": 1}
        return None

    @asynccontextmanager
    async def transaction(self) -> Iterator[FakeDatabase]:
        """A transaction. Nested ones join the outer one, as a real pool's would.

        The join matters: it is what makes "the write and its event are in the same
        transaction" observable as two statements at the same depth rather than as
        two separate blocks that happen to be adjacent in a test.
        """
        outermost = self._depth == 0
        self._depth += 1
        staged_vault = dict(self.vault)
        staged_outbox = list(self.outbox)
        try:
            yield self
        except BaseException:
            self.vault = staged_vault
            self.outbox = staged_outbox
            self.rollbacks += 1
            raise
        else:
            if outermost:
                self.commits += 1
        finally:
            self._depth -= 1

    # --- helpers for the tests --------------------------------------------

    def statements_matching(self, needle: str) -> list[Statement]:
        return [statement for statement in self.statements if needle in statement.sql]

    def sql(self) -> list[str]:
        return [statement.sql for statement in self.statements]

    def depths(self) -> list[int]:
        return [statement.depth for statement in self.statements]

    def _record(self, sql: str, params: Sequence[Any]) -> None:
        self.statements.append(Statement(sql, tuple(params), self._depth))

    def _maybe_fail(self) -> None:
        if self.fail_next is not None:
            error, self.fail_next = self.fail_next, None
            raise error

    # --- the two tables ----------------------------------------------------

    def _write_vault(self, sql: str, params: tuple[Any, ...]) -> None:
        if "insert into" in sql:
            provider, ciphertext, key_version = params[0], params[1], params[2]
            existing = self.vault.get(provider)
            self.vault[provider] = {
                "provider": provider,
                "ciphertext": ciphertext,
                "key_version": key_version,
                "created_at": existing["created_at"] if existing else "t0",
                "updated_at": "t1",
            }
        elif "delete from" in sql:
            self.vault.pop(params[0], None)

    def _read_vault(self, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
        if "order by provider" in sql:
            # `None` for an empty result, which is what `fetchone` returns from
            # postgres when a query matched no rows. A fake that returned an empty
            # list here would leave the caller's `None` branch untested and the
            # readiness body quietly wrong.
            rows = [{"provider": name} for name in sorted(self.vault)]
            return {"rows": rows} if rows else None
        if "select" in sql and "delete" not in sql:
            return self.vault.get(params[0]) if params else None
        return None

    def _write_outbox(self, sql: str, params: tuple[Any, ...]) -> None:
        if "insert into" in sql:
            self.outbox.append(dict(zip(_OUTBOX_COLUMNS, params, strict=False)))
        elif "set published_at" in sql:
            self._mark_outbox(params[0], lambda row: row.update(published_at="now()"))
        elif "set attempts" in sql:
            self._mark_outbox(
                params[0], lambda row: row.update(attempts=int(row.get("attempts", 0)) + 1)
            )

    def _mark_outbox(self, event_id: Any, mark: Callable[[dict[str, Any]], None]) -> None:
        """Apply the publisher's mark to the stored row, as postgres would.

        The publisher updates the same table the meter inserts into, so matching on the
        table name alone files a `published_at` mark as a second event — and the next
        claim hands the loop a row with no `event_type` in it. A row the id does not
        match is left alone, which is what an update of zero rows looks like.
        """
        for row in self.outbox:
            if row.get("id") == event_id:
                mark(row)

    def _read_outbox(self, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
        if "where published_at is null" in sql:
            return {
                "rows": [
                    _as_driver_row(row) for row in self.outbox if row.get("published_at") is None
                ]
            }
        return None


def _as_driver_row(row: Mapping[str, Any]) -> dict[str, Any]:
    """The row as psycopg hands it back: `jsonb` decoded, every other column verbatim.

    The store keeps `data` as the JSON text the insert wrote, because surviving the
    round trip through the database is the property the metering test asserts. A *reader*
    gets a dict, though — so a publisher that forgets that fails at runtime with
    `dict("...")` on a string, which is exactly the kind of thing a fake that agrees
    with the code instead of the driver hides.
    """
    payload = row.get("data")
    return {**row, "data": json.loads(payload) if isinstance(payload, str) else payload}


#: The parameter order the outbox insert is written in, so the fake can turn a
#: positional tuple back into a row the tests can read by name.
_OUTBOX_COLUMNS = (
    "id",
    "event_type",
    "source",
    "subject",
    "time",
    "data",
    "created_at",
)
