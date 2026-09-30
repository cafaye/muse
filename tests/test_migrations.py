"""Migrations must match the contract core documents.

core owns the outbox table shape in `docs/event-outbox.md` — the column list is
the contract, and every service implements it in its own migration. Nothing
executes SQL in this suite (AGENTS.md rule 3: no socket in tests), so these
tests read the migration files and assert the shape: a column that core requires
is present with core's type and nullability, and the publisher's index exists.

A migration that drifts from core's documented table is invisible until a
consumer reads a null it was told could not be null, so it is worth a test.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

#: core docs/event-outbox.md, `create table if not exists outbox_events`.
#: (column, postgres type, not null?)
CORE_OUTBOX_COLUMNS = [
    ("id", "uuid", True),
    ("event_type", "text", True),
    ("source", "text", True),
    ("subject", "text", True),
    ("time", "timestamptz", True),
    ("data", "jsonb", True),
    ("created_at", "timestamptz", True),
    ("published_at", "timestamptz", False),
    ("attempts", "int", True),
]

#: The vault table is muse's own; the brief names it and nothing else depends on
#: its shape yet, so this is the minimum that keeps a key recoverable.
VAULT_COLUMNS = [
    ("provider", "text", True),
    ("ciphertext", "bytea", True),
    ("key_version", "int", True),
    ("created_at", "timestamptz", True),
    ("updated_at", "timestamptz", True),
]

COLUMN_RE = re.compile(
    r"^[ \t]+(?P<name>\w+)[ \t]+(?P<type>\w+(?:\s*\(\d+\))?)\s*"
    r"(?P<constraint>not null|primary key)?",
    re.IGNORECASE | re.MULTILINE,
)

#: Lines that are part of a CREATE TABLE body but are not columns.
NOT_A_COLUMN = {"constraint"}


def migration_text(name: str) -> str:
    path = MIGRATIONS / name
    assert path.is_file(), f"missing migration {name}"
    return path.read_text(encoding="utf-8")


def columns_of(create_table_sql: str) -> dict[str, tuple[str, bool]]:
    """Parse `name type [not null | primary key]` out of a CREATE TABLE body.

    `primary key` counts as not-null because postgres implies it, and core's
    documented table would otherwise look nullable.
    """
    body = create_table_sql[create_table_sql.index("(") :]
    found: dict[str, tuple[str, bool]] = {}
    for match in COLUMN_RE.finditer(body):
        if match["name"].lower() in NOT_A_COLUMN:
            continue
        found[match["name"]] = (match["type"].lower().replace(" ", ""), bool(match["constraint"]))
    return found


def assert_table(sql: str, table: str, expected: list[tuple[str, str, bool]]) -> None:
    lowered = sql.lower()
    head = f"create table if not exists {table}"
    assert head in lowered, f"{table} is not created by this migration"
    parsed = columns_of(lowered[lowered.index(head) :])
    for name, type_, not_null in expected:
        assert name in parsed, f"{table}.{name} is missing"
        assert parsed[name][0] == type_, f"{table}.{name} is {parsed[name][0]}, core says {type_}"
        assert parsed[name][1] is not_null, (
            f"{table}.{name} nullability disagrees with core's documented table"
        )


def test_every_migration_is_numbered_and_ordered() -> None:
    """Deploys apply files in name order; a gap or a duplicate is a surprise."""
    names = sorted(path.name for path in MIGRATIONS.glob("*.sql"))
    assert names == ["00001_outbox_events.sql", "00002_vault_secrets.sql"]


def test_outbox_table_matches_core() -> None:
    assert_table(migration_text("00001_outbox_events.sql"), "outbox_events", CORE_OUTBOX_COLUMNS)


def test_outbox_has_the_partial_index_the_publisher_query_needs() -> None:
    """core's publisher selects `where published_at is null order by created_at`;
    without the partial index that is a sequential scan of every event ever."""
    sql = migration_text("00001_outbox_events.sql").lower()
    assert "on outbox_events (created_at, id)" in sql
    assert "where published_at is null" in sql


def test_outbox_id_is_the_primary_key() -> None:
    """`id` is the consumer's dedupe key, so it has to be unique in the table."""
    sql = migration_text("00001_outbox_events.sql").lower()
    assert re.search(r"id\s+uuid\s+primary key", sql)


def test_vault_table_has_the_shape_the_store_uses() -> None:
    assert_table(migration_text("00002_vault_secrets.sql"), "vault_secrets", VAULT_COLUMNS)


def test_vault_is_keyed_by_provider() -> None:
    sql = migration_text("00002_vault_secrets.sql").lower()
    assert re.search(r"provider\s+text\s+primary key", sql)


def test_vault_never_stores_a_plaintext_column() -> None:
    """The column list is the whole security argument of this table: a plaintext
    column would be one careless insert away from a readable secret."""
    parsed = columns_of(migration_text("00002_vault_secrets.sql").lower())
    for name in parsed:
        assert "plaintext" not in name
        assert "key" not in name or name == "key_version"


def test_migrations_are_idempotent_to_apply() -> None:
    """`if not exists` on every create, so re-running a deploy's migration step is a
    no-op instead of a failed boot."""
    for path in MIGRATIONS.glob("*.sql"):
        sql = path.read_text(encoding="utf-8").lower()
        for statement in re.findall(r"create (?:table|index)\s+(?!if not exists)\w+", sql):
            pytest.fail(f"{path.name} has an unguarded `create {statement}`")


def test_every_table_is_created_guarded() -> None:
    """The other half of the same rule, stated separately so a failure says which
    guarantee broke."""
    for path in MIGRATIONS.glob("*.sql"):
        sql = path.read_text(encoding="utf-8").lower()
        assert sql.count("create table if not exists") == sql.count("create table ")
