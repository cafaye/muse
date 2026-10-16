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


def sql_only(name: str) -> str:
    """The executable lines of a migration, comments dropped and lower-cased.

    A migration's comments are its argument to a reader, and they name the SQL
    they argue about — `00003_app_role_grants.sql` says "no TRUNCATE" and "no
    policy" in prose while granting neither. An assertion about what a migration
    GRANTS has to read the statements, or it reds on the sentence explaining why
    the statement is absent. Every executable line is kept, including a trailing
    comment on one, so a privilege cannot hide behind a `--`.
    """
    return "\n".join(
        line
        for line in migration_text(name).lower().splitlines()
        if not line.lstrip().startswith("--")
    )


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
    assert names == [
        "00001_outbox_events.sql",
        "00002_vault_secrets.sql",
        "00003_app_role_grants.sql",
    ]


def test_the_app_role_migration_grants_the_verbs_the_code_issues() -> None:
    """`muse_app` gets DML on the two tables and nothing more.

    The role is provisioned by kit's cluster (`10-cluster.sh` creates `<svc>` and
    `<svc>_app` for every name in `KIT_POSTGRES_DATABASES`); the GRANT is the
    service's business, which is why it is a migration here. Read the other way,
    this is the test that fails if a future edit widens the application's
    privilege to `all` — TRUNCATE, REFERENCES and TRIGGER are the verbs `all`
    would add, and none of them is a verb muse's statements use.
    """
    sql = sql_only("00003_app_role_grants.sql")
    assert "grant select, insert, update on table outbox_events to muse_app;" in sql
    assert "grant select, insert, update, delete on table vault_secrets to muse_app;" in sql
    assert "truncate" not in sql, (
        "TRUNCATE is not a verb any statement in src/ issues, and `grant all` would "
        "add it along with REFERENCES and TRIGGER — the login role holds DML and "
        "nothing else"
    )


def test_the_app_role_migration_carries_no_policies() -> None:
    """The honest zero, asserted where the next reader would look for policies.

    muse holds no account-scoped rows: neither table has an `account_id` column,
    and `tests/test_tenant_scoping.py` holds the same negative for `src/`. So the
    grant migration must not grow a policy, and `cafaye.protect_table` is not
    called here — it raises `undefined_column` on a table with no `account_id`,
    and its hint says a service holding no customer rows needs none of this. This
    test exists so that "finish the job by adding RLS" is a red test rather than a
    well-meant edit.
    """
    sql = sql_only("00003_app_role_grants.sql")
    assert "policy" not in sql, (
        "this migration grants privileges to the login role and writes no policy: "
        "muse holds no account-scoped rows, so there is no predicate for one to "
        "carry — and protect_table cannot be called on a table with no account_id"
    )
    assert "row level security" not in sql


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
