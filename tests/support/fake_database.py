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

from collections.abc import Iterator, Mapping, Sequence
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
            self._write_outbox(tuple(params))

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

    def _write_outbox(self, params: tuple[Any, ...]) -> None:
        self.outbox.append(dict(zip(_OUTBOX_COLUMNS, params, strict=False)))

    def _read_outbox(self, sql: str, params: tuple[Any, ...]) -> Mapping[str, Any] | None:
        if "where published_at is null" in sql:
            return {"rows": [row for row in self.outbox if row.get("published_at") is None]}
        return None


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
