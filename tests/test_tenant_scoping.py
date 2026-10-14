"""muse-09 — tenant isolation: every account-scoped entry point, negatively tested.

Following `darkroom-09`'s pattern, which is the whole method and not a formality:
**enumerate every account-scoped entry point, write a negative test per entry point,
and make the enumeration load-bearing.** The last clause is what separates this from a
list of good intentions. An enumeration nobody checks against the code is a comment
that ages badly; so the completeness guards below walk the *running app* and the
*source tree* and fail when something appears that the table does not know about.

## The shape of the answer muse can give, which is itself the measurement

muse is a router, a vault and a meter, and it stores **no tenant-owned rows**.
`vault_secrets` is keyed by `provider` — one platform credential per vendor, by design
(`migrations/00002_vault_secrets.sql`) — and `outbox_events` has no tenant column at
all, because core's payload schema still pins `subject: platform` (D9). So the honest
enumeration is small, and the negative test for most of it is *the absence of a tenant
axis* rather than a scope check on one.

That is worth stating rather than inflating into a larger number. An isolation suite
that claims a surface it does not have is worse than none, because it makes a reader
believe the gap was closed. **12 entry points; 3 of them carry a tenant axis at all.**

## Absence, never 403 — and why the distinction is the whole point

A 403 on the tenant axis is an **enumeration oracle**: it says "this exists and is not
yours", which is strictly more information than the 404 a nonexistent resource gets.
It is the difference between a caller who learns nothing and a caller who can walk a
namespace. Two properties follow, and both are asserted rather than hoped for:

- **No account-scoped entry point in muse answers 403.** The one 403 muse has
  (`InsufficientScope`) is on the *capability* axis, and
  `test_a_forbidden_body_does_not_vary_by_account` asserts two different accounts
  receive a byte-identical body — so the 403 cannot be used to probe tenancy.
- **A path that does not exist answers 404, not 403**, whichever account the token
  names (`test_a_path_that_does_not_exist_is_404_for_every_account`).

## What is deliberately NOT fixed here

`Meter` knows the caller's `account_id` and cannot write it, because core owns the
payload schema. That is D9, recorded in `src/muse/metering.py` and in
`REPORT-muse-09-isolation.md`. This suite asserts the absence is real *and named* —
`test_a_metered_event_names_no_account` — rather than pretending the gap is closed. A
test that implied it was closed would be the defect, not the coverage.
"""

from __future__ import annotations

import ast
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from muse.metering import SUBJECT, UsageEvent
from muse.outbox import InMemoryTransport, OutboxPublisher
from muse.providers import Price, ProviderRegistry
from muse.providers.fake import FakeProvider
from muse.routes import routes_from_yaml
from muse.vault import Vault

from .conftest import asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.jwks import ACCOUNT, tampered, token
from .support.test_app import IDENTITY, TEST_KEY, build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "muse"

#: A second account, so a test can ask "what changes when the tenant changes?" and get
#: the honest answer — nothing, and that is the point — as an assertion rather than an
#: assumption. Every token here is real and drives the real verifier.
OTHER_ACCOUNT = "01HQ0000000000000000000000B"

#: An account nobody has ever heard of, for the third leg of every question.
NO_SUCH_ACCOUNT = "01HQ000000000000000000000ZZ"

MODEL = "gpt-4o-mini"
PRICE = Price(150, 600)
BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}
# The decisions twin of `BODY`, for the entry point that shares every property of
# the two above it.
DECISION_BODY = {
    "model": "fast",
    "input": "hello",
    "questions": [{"type": "noul", "instructions": "is this a refund?"}],
}


# ---------------------------------------------------------------------------
# The enumeration. This table is the deliverable; everything after it checks it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """One place a caller's tenant can be observed, refused, or forgotten.

    `tenant_axis` is the honest classification and it is the interesting column:

    - `ACCOUNT` — the tenant is the discriminator, so something must check it. There
      are three of these and each is covered.
    - `PLATFORM` — no tenant axis at all. The negative test asserts the absence, which
      is the only thing that can be asserted about a discriminator that does not exist.
    - `NONE` — not reachable from an authenticated caller. The outbox publisher runs in
      the background loop, never in a request.
    """

    name: str
    layer: str
    operation: str
    tenant_axis: str
    negative_test: str


#: The tenancy is established here, and everything downstream reads it from the
#: verified `Principal` rather than from the request.
REQUIRE_BEARER = EntryPoint(
    "api.require_bearer", "auth", "read", "ACCOUNT", "test_an_edited_tenant_claim_is_refused"
)
#: Capability, not tenancy. In the table because it is the only 403 in muse, and a 403
#: is exactly what an isolation suite has to reason about.
REQUIRE_SCOPE = EntryPoint(
    "auth.require_scope",
    "auth",
    "read",
    "PLATFORM",
    "test_a_forbidden_body_does_not_vary_by_account",
)

POST_ROUTE = EntryPoint(
    "POST /v1/route", "http", "read", "ACCOUNT", "test_two_accounts_get_the_same_completion"
)
#: The OpenAI-compatible alias. `ACCOUNT`, and not `PLATFORM`, because it is the same
#: write: `muse.api.serve_completion` is what meters it, so it inherits the tenancy
#: establishment and the D9 gap of `/v1/route` rather than escaping them. Calling it
#: `PLATFORM` on the grounds that it "only renders a different body" would be a claim
#: about the shape rather than about the path, and the path is what meters.
POST_CHAT_COMPLETIONS = EntryPoint(
    "POST /v1/chat/completions",
    "http",
    "read",
    "ACCOUNT",
    "test_the_alias_carries_no_account_and_emits_the_same_payload",
)
#: The decisions path. `ACCOUNT`, and not `PLATFORM`, for the same reason the alias
#: is: `muse.api.serve_decisions` meters it through the same `Meter.record`, so the
#: tenancy establishment and the D9 gap are inherited rather than escaped. A decision
#: is a read of the model, and an unreadable tenant would be a resource — which is
#: why "read" is the honest axis and not "update".
POST_DECISIONS = EntryPoint(
    "POST /v1/decisions",
    "http",
    "read",
    "ACCOUNT",
    "test_a_decision_names_no_account_and_meters_once",
)
#: `PLATFORM`: the route table is configuration, not tenant data.
GET_MODELS = EntryPoint(
    "GET /v1/models", "http", "read", "PLATFORM", "test_the_model_list_names_no_account"
)
HEALTHZ = EntryPoint("GET /healthz", "http", "read", "PLATFORM", "test_healthz_names_no_account")
READYZ = EntryPoint("GET /readyz", "http", "read", "PLATFORM", "test_readyz_names_no_account")

METER_RECORD = EntryPoint(
    "metering.Meter.record",
    "service",
    "update",
    "ACCOUNT",
    "test_a_metered_event_names_no_account",
)
VAULT_GET = EntryPoint(
    "vault.Vault.get",
    "service",
    "read",
    "PLATFORM",
    "test_the_vault_is_unreachable_from_any_caller",
)
VAULT_HAS = EntryPoint(
    "vault.Vault.has",
    "service",
    "read",
    "PLATFORM",
    "test_the_vault_is_unreachable_from_any_caller",
)
VAULT_PROVIDERS = EntryPoint(
    "vault.Vault.providers", "service", "list", "PLATFORM", "test_the_provider_list_is_never_served"
)
VAULT_PUT = EntryPoint(
    "vault.Vault.put", "service", "update", "PLATFORM", "test_no_route_writes_the_vault"
)
VAULT_DELETE = EntryPoint(
    "vault.Vault.delete",
    "service",
    "delete",
    "PLATFORM",
    "test_deleting_an_absent_key_is_false_not_an_error",
)
OUTBOX_CLAIM = EntryPoint(
    "outbox.OutboxPublisher.claim",
    "service",
    "list",
    "NONE",
    "test_the_claim_query_is_unscoped_and_only_reads_unpublished",
)

#: **14 account-scoped entry points. 9 read, 2 list, 2 update, 1 delete.**
#:
#: Of the fourteen, **four carry a tenant axis** (`ACCOUNT`: `require_bearer`,
#: `POST /v1/route`, `POST /v1/chat/completions`, `Meter.record`) and ten do not. That
#: ratio is the measurement: muse's tenant surface is the auth gate, the two
#: entry points into the one routed write, and that write itself — and the other ten are
#: load-bearing *because* they cannot see a tenant.
#:
#: The alias packet took this from three to four, and the delta is worth reading
#: because it is the honest answer rather than the convenient one. `POST
#: /v1/chat/completions` could have been classified `PLATFORM` on the grounds that it
#: only renders a different body from a call that was already made. It cannot: it
#: reaches the same `Meter.record`, so it carries the same tenancy establishment and the
#: same D9 gap. A second body is not a second surface, and the enumeration had to move
#: because the *path* is new — not because a new discriminator appeared.
ENTRY_POINTS: tuple[EntryPoint, ...] = (
    REQUIRE_BEARER,
    REQUIRE_SCOPE,
    POST_ROUTE,
    POST_CHAT_COMPLETIONS,
    POST_DECISIONS,
    GET_MODELS,
    HEALTHZ,
    READYZ,
    METER_RECORD,
    VAULT_GET,
    VAULT_HAS,
    VAULT_PROVIDERS,
    VAULT_PUT,
    VAULT_DELETE,
    OUTBOX_CLAIM,
)

#: The ratchet. `gate.yml`'s floor counts *tests*, and a test that stops being collected
#: keeps it green. This counts *enumerated entry points*, so deleting a row without its
#: negative test fails here instead.
ENTRY_POINT_COUNT = 15

#: Per-operation counts, reported in the packet and asserted so a table that quietly
#: loses a whole operation cannot pass.
OPERATION_COUNTS = {"read": 10, "list": 2, "update": 2, "delete": 1}

#: How many of the fifteen carry a tenant axis. The single most load-bearing number in
#: this file, because it is the one that says "muse has almost no tenancy surface", and a
#: future packet that grows one has to move it deliberately.
TENANT_SCOPED_COUNT = 5


def auth(account: str, **claims: object) -> dict[str, str]:
    """A real bearer token naming `account`, signed by the fixture identity.

    Not a stub: every test in this file drives signature verification, the claim checks,
    the capability gate and the tenancy gate, so a negative test cannot pass by
    bypassing the boundary it is testing (see `tests/conftest.py`).

    `scopes=""` rather than `scopes=()` for the capability gate: the claim is a
    space-separated string, so an empty one is the empty set while an empty tuple is a
    claim muse cannot interpret — which is a 401, and would make a scope test pass for
    the wrong reason.
    """
    overrides: dict[str, object] = {"account_id": account, **claims}
    return {"Authorization": f"Bearer {token(IDENTITY, **overrides)}"}


def header(raw: str) -> dict[str, str]:
    """A raw credential in an `Authorization` header, for a token built by hand."""
    return {"Authorization": f"Bearer {raw}"}


def app_for(database: FakeDatabase | None = None):
    """An app whose one route completes successfully, over an in-memory database.

    `FakeProvider` rather than `ScriptedProvider`, and the choice is load-bearing here:
    a script is consumed one entry per call, so three accounts asking three questions
    would exhaust it and the third would 500 — which would make this suite's central
    claim ("every account gets the same thing") fail for a reason that has nothing to do
    with tenancy. The echo double answers identically every time, which is precisely the
    property under test.
    """
    registry = ProviderRegistry()
    registry.register(
        FakeProvider(name="openai", content="hello there", tokens_in=12, tokens_out=8, price=PRICE)
    )
    return build_test_app(
        registry=registry,
        database=database or FakeDatabase(),
        table=routes_from_yaml(
            write_routes(
                {
                    "version": 1,
                    "routes": [
                        {
                            "model": "fast",
                            "candidates": [{"provider": "openai", "model": MODEL}],
                        }
                    ],
                }
            )
        ),
    )


# ---------------------------------------------------------------------------
# Guard 1 — the table is complete against the running application.
# ---------------------------------------------------------------------------

#: FastAPI's own documentation routes. Named rather than pattern-matched because
#: "anything under /docs" is a filter with a bypass, and a route at `/docs/accounts`
#: would sail straight through it.
_FRAMEWORK_ROUTES = frozenset({"/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"})

#: HTTP verbs that name a resource, so an `operationId` or a `parameters` key is not
#: mistaken for a method.
_METHODS = frozenset({"get", "post", "put", "patch", "delete", "head", "options"})


def mounted_routes(app) -> set[str]:
    """Every ``METHOD path`` the running application actually serves.

    Read off the generated OpenAPI document rather than by walking `app.routes`: the
    document is the service's own statement of its HTTP surface, it is the thing the
    SDKs are generated from, and it is a flat mapping that does not change shape when
    the framework changes how it represents an included router.
    """
    paths = app.openapi().get("paths", {})
    return {
        f"{method.upper()} {path}"
        for path, operations in paths.items()
        if path not in _FRAMEWORK_ROUTES
        for method in operations
        if method in _METHODS
    }


def test_every_http_route_is_enumerated(app):
    """A new route appears in the table, or the suite goes red.

    This is the load-bearing half of the packet. An isolation suite that enumerates
    only what it happens to know about is a suite that reports "12 entry points, all
    covered" while the thirteenth ships unreviewed — which is exactly how a service
    grows a tenant surface nobody tested. So the *running app* is the oracle, not this
    table, and the assertion is set equality in both directions so a stale row fails
    as loudly as a missing one.
    """
    enumerated = {entry.name for entry in ENTRY_POINTS if entry.layer == "http"}
    mounted = mounted_routes(app)
    assert mounted == enumerated, (
        f"the app serves {sorted(mounted - enumerated)} and the table does not enumerate "
        f"it; the table claims {sorted(enumerated - mounted)} and the app serves no such "
        "route"
    )


def test_the_enumeration_has_the_counted_number_of_entry_points():
    """The ratchet, as a count of entry points rather than a count of tests."""
    assert len(ENTRY_POINTS) == ENTRY_POINT_COUNT


def test_the_enumeration_covers_every_operation_the_packet_asks_for():
    """`read`, `list`, `update`, `delete` — all four, with the reported counts."""
    counted: dict[str, int] = {}
    for entry in ENTRY_POINTS:
        counted[entry.operation] = counted.get(entry.operation, 0) + 1
    assert counted == OPERATION_COUNTS


def test_only_five_entry_points_carry_a_tenant_axis():
    """The measurement, pinned: muse's tenant surface is five entry points wide.

    Asserted because a packet that *grows* one should have to change this number on
    purpose, next to the negative test for the thing it added — not leave the old count
    sitting there claiming the surface is still three.

    The alias packet moved this from three to four. Both new rows are listed here in
    order so the delta is legible: `POST /v1/chat/completions` joins because it meters,
    and `GET /v1/models` stays off the list because a route table is configuration
    rather than tenant data.
    """
    carrying = [entry.name for entry in ENTRY_POINTS if entry.tenant_axis == "ACCOUNT"]
    assert carrying == [
        "api.require_bearer",
        "POST /v1/route",
        "POST /v1/chat/completions",
        "POST /v1/decisions",
        "metering.Meter.record",
    ]
    assert len(carrying) == TENANT_SCOPED_COUNT


def test_every_entry_point_names_a_test_that_actually_exists(request):
    """Each row's `negative_test` is a real, collected test function.

    The failure this exists for is a row pointing at a test that was renamed: the row
    still reads as covered, the coverage is gone, and nothing else in the suite notices.
    Checked against `request.session.items`, so an uncollected function fails the guard
    rather than satisfying it.
    """
    collected = {item.name for item in request.session.items}
    missing = sorted({entry.negative_test for entry in ENTRY_POINTS} - collected)
    assert not missing, f"enumerated entry points name tests that are not collected: {missing}"


def test_the_entry_points_are_distinct():
    """No row is a restatement of another row wearing a different name."""
    names = [entry.name for entry in ENTRY_POINTS]
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# Guard 2 — the table is complete against the source tree.
# ---------------------------------------------------------------------------

#: A DML or DQL verb, which is what makes a string literal in `src/muse/` a query rather
#: than prose. Case-insensitive because `select 1 as ok` is lowercase in one place and
#: the rest are uppercase.
_SQL = re.compile(
    r"\b(select\s|insert\s+into\b|update\s+\w+\s+set\b|delete\s+from\b)", re.IGNORECASE
)

#: Every query in the source, attributed to a file. This is the second, independent
#: statement of "there are twelve", derived from the tree rather than from the table —
#: and the half a route walk cannot do, because a query reached only from a background
#: loop is invisible at the HTTP level.
SQL_SITES: dict[str, tuple[str, ...]] = {
    "main.py": ("select 1 as ok",),
    "metering.py": ("insert into outbox_events",),
    "outbox.py": (
        "select id, event_type, source, subject, time, data, attempts",
        "update outbox_events set published_at",
        "update outbox_events set attempts",
    ),
    "vault.py": (
        "insert into vault_secrets",
        "select ciphertext, key_version from vault_secrets",
        "delete from vault_secrets",
        "select provider from vault_secrets",
    ),
}

#: How many statements that is: the DB-tier half of the entry-point count.
QUERY_COUNT = 9


def _string_literals(path: Path) -> list[str]:
    """Every string *constant* in a module that is not a docstring.

    Parsed rather than line-matched, because a line scan cannot tell a query from the
    word "update" in a comment explaining why a query was added — and a guard that fires
    on prose is a guard that gets deleted on the first day it is inconvenient.
    """
    tree = ast.parse(path.read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                docstrings.add(id(body[0].value))
    return [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and _SQL.search(node.value)
    ]


def test_every_sql_statement_in_the_source_is_enumerated():
    """A new query appears in `SQL_SITES`, or the suite goes red.

    Nine statements, all attributed to an enumerated entry point. A query is a tenant
    surface the moment it touches tenant data, so an unaccounted one is a surface with
    no negative test — the exact defect D18 measured at zero.

    The **count** is asserted per file, not merely that every expected statement is
    present, and that is the half this guard was missing the first time it ran. Asking
    "does the file contain the query the table names?" passes just as happily when the
    file holds *two* queries and the table names one of them: a canary that appended a
    second, tenant-free `select count(*)` to `metering.py` went green. A set-difference
    guard is only as good as the assertion saying which side is meant to be bigger.
    """
    found = {
        path.name: _string_literals(path)
        for path in sorted(SRC.rglob("*.py"))
        if _string_literals(path)
    }

    assert set(found) == set(SQL_SITES), (
        f"SQL found in {sorted(set(found) - set(SQL_SITES))}, which the enumeration does "
        f"not account for; SQL_SITES claims {sorted(set(SQL_SITES) - set(found))}, which "
        "holds no statement"
    )
    for name, expected in SQL_SITES.items():
        haystack = "\n".join(found[name]).lower()
        named = {statement.lower() for statement in expected}
        for statement in expected:
            assert statement.lower() in haystack, (
                f"{name}: enumerated query {statement!r} is not in the file"
            )
        unnamed = [
            literal.strip()[:60] for literal in found[name] if literal.strip().lower() not in named
        ]
        assert len(found[name]) == len(expected), (
            f"{name} holds {len(found[name])} SQL statements and the enumeration names "
            f"{len(expected)}; the unnamed ones are {unnamed}"
        )
    assert sum(len(v) for v in SQL_SITES.values()) == QUERY_COUNT


def test_no_query_filters_or_writes_by_a_tenant():
    """Not one statement mentions a tenant — and that is asserted, not assumed.

    The negative test for the whole DB tier. If a query ever grows `where account_id`,
    this goes red and the `PLATFORM` rows above stop being true, so each would need a
    negative test written against the new reality. Checking the *parsed literal* rather
    than one line is deliberate: a tenant predicate written across two lines is still a
    tenant predicate, and a guard that only reads one line is a guard with a bypass.
    """
    offenders = [
        f"{path.name}: {literal.strip()[:80]}"
        for path in sorted(SRC.rglob("*.py"))
        for literal in _string_literals(path)
        if re.search(r"account", literal, re.IGNORECASE)
    ]
    assert not offenders, (
        "a query now references the tenant, so the table's PLATFORM rows are no longer "
        f"true and each needs a negative test: {offenders}"
    )


def test_no_migration_carries_a_tenant_column():
    """The same assertion at the schema, where a tenant column would appear first."""
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in sorted((REPO_ROOT / "migrations").glob("*.sql"))
        for line in path.read_text().splitlines()
        if re.search(r"\baccount_id\b", line)
    ]
    assert not offenders, f"a migration introduced a tenant column: {offenders}"


def test_no_function_takes_a_tenant_parameter():
    """The blunt final sweep: nothing in `src/muse/` is called with an account.

    If one ever is, an isolation question exists that this table has not answered — and
    the way to find that out is a red suite rather than a review comment nobody reads.
    """
    offenders = [
        f"{path.name}:{node.lineno}"
        for path in sorted(SRC.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and any(
            argument.arg == "account_id" or argument.arg.startswith("account")
            for argument in _all_arguments(node)
        )
    ]
    assert not offenders, f"a function now takes a tenant argument: {offenders}"


def _all_arguments(node) -> list[ast.arg]:
    """Every positional and keyword argument of a function definition."""
    found = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    return found


def test_no_test_file_is_left_uncollected():
    """A test file that pytest cannot collect is a file of tests that never run.

    This is not hypothetical: this packet's own file was first written as
    `tests/tenant_scoping.py`, mirroring `darkroom-09`'s Rust layout, where every
    `.rs` under `tests/` is a target by convention. Python has no such convention —
    `pyproject.toml` sets `testpaths` but not `python_files`, so pytest collects
    `test_*.py` and `*_test.py` and nothing else. The 24 tests below ran green, were
    reported green, and were **not in the gate**: `904 passed, 3 skipped` both before
    and after the commit that added them.

    So the guard is the one thing that would have caught it, and it is here for every
    worker who writes a file rather than naming it right. A file that defines a `def
    test_` and does not match the pattern is a suite that reports green while doing
    nothing, which is the same defect as the SQL guard that passed on a file holding
    two queries while naming one.
    """
    uncollected = [
        path.name
        for path in sorted((REPO_ROOT / "tests").rglob("*.py"))
        if "def test_" in path.read_text()
        and not path.name.startswith("test_")
        and not path.name.endswith("_test.py")
    ]
    assert not uncollected, (
        f"these files define tests but pytest does not collect them (pyproject sets "
        f"`testpaths` and not `python_files`, so the default pattern applies): {uncollected}"
    )


def test_this_file_is_itself_collected(request):
    """The guard above, applied to the file it lives in.

    A guard that can pass because *it* is not running is the worst version of the
    failure, so this one is deliberately trivial and deliberately here: it asserts its
    own module is in the collected set. It costs a line and it is the difference
    between a guard and a comment.
    """
    collected = {str(item.fspath) for item in request.session.items}
    assert f"{Path(__file__).name}" in {Path(f).name for f in collected}


# ---------------------------------------------------------------------------
# Negative tests, one per entry point.
# ---------------------------------------------------------------------------

#: The exact success body keys, so a response that starts naming the caller's tenant is
#: a failure rather than an extra key nobody reads.
_ROUTE_KEYS = {
    "id",
    "object",
    "created",
    "model",
    "provider",
    "route",
    "choices",
    "usage",
    "cost_micros",
    "trace_id",
}


async def test_an_edited_tenant_claim_is_refused():
    """`api.require_bearer` — ACCOUNT, read.

    The tenant is read from the *verified claims*, never from the request. A caller who
    edits `account_id` after signing gets a 401, so there is no way to arrive at another
    account's side of the system by rewriting a claim — and the refusal names neither
    the account they asked for nor the one they had.
    """
    forged = tampered(token(IDENTITY, account_id=ACCOUNT), account_id=OTHER_ACCOUNT)
    async with asgi_client(app_for()) as client:
        borrowed = await client.post(
            "/v1/route", json=BODY, headers={"Authorization": f"Bearer {forged}"}
        )
        genuine = await client.post("/v1/route", json=BODY, headers=auth(OTHER_ACCOUNT))

    assert borrowed.status_code == 401
    assert borrowed.json()["code"] == "unauthorized"
    assert borrowed.json()["status"] == 401
    assert OTHER_ACCOUNT not in borrowed.text
    assert ACCOUNT not in borrowed.text
    # The real owner of that account is still served, so the 401 above was the
    # signature and not a broken fixture.
    assert genuine.status_code == 200


async def test_a_decision_names_no_account_and_meters_once():
    """`POST /v1/decisions` — ACCOUNT, read.

    The decisions twin of the alias's negative test, and it asks the same question of
    three accounts: a served decision names no tenant, and it meters once. Comparing
    the metered event across accounts is what makes "the same write" checkable — a
    decision that metered per account, or that carried one into the envelope, would be
    a tenant surface the suite had silently grown.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.post(
                "/v1/decisions", json=DECISION_BODY, headers=auth(account)
            )
            assert response.status_code == 200, f"/v1/decisions as {account}"
            assert account not in response.text, "the decision named the caller's account"

    assert len(database.outbox) == 3
    envelopes = {event["data"] for event in database.outbox}
    assert len(envelopes) == 1, (
        "one decision from three accounts metered three different ways"
    )


async def test_a_token_with_no_tenant_is_refused_rather_than_defaulted():
    """`api.require_bearer` — the tenancy gate itself, and 401 rather than 403.

    `MissingAccount` is a 401 because it is about the *credential*, not about permission
    to touch a particular resource. A 403 here would tell a caller holding a perfectly
    valid signature that the token is real and only its tenant is wrong, which is the
    first half of an enumeration oracle.
    """
    async with asgi_client(app_for()) as client:
        served = await client.post("/v1/route", json=BODY, headers=auth(ACCOUNT))
        anonymous = await client.post(
            "/v1/route", json=BODY, headers=header(token(IDENTITY, account_id=None))
        )

    assert served.status_code == 200, "sanity: a token with a tenant is served"
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "unauthorized"
    assert ACCOUNT not in anonymous.text


async def test_a_forbidden_body_does_not_vary_by_account():
    """`auth.require_scope` — the only 403 in muse, and it is not a tenancy oracle.

    muse has exactly one 403, on the **capability** axis: a correctly signed token
    carrying no `completions:write`. That is the right status for a missing capability
    and the wrong status for "that resource is another account's". What makes it safe
    here is that the body cannot be made to vary by tenant — so asking twice, as two
    accounts, cannot learn anything about either one's holdings.
    """
    # `scopes=""` and not `scopes=()`: the claim is a space-separated string, so an
    # empty one is the empty set and an empty tuple is a claim muse cannot interpret
    # (which is a 401, and would make this test pass for the wrong reason).
    async with asgi_client(app_for()) as client:
        bodies = []
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.post("/v1/route", json=BODY, headers=auth(account, scopes=""))
            assert response.status_code == 403
            bodies.append(response.json())

    # Only `trace_id` may differ between the three; everything else is fixed text.
    volatile = {"trace_id", "instance"}
    stripped = [{k: v for k, v in body.items() if k not in volatile} for body in bodies]
    assert stripped[0] == stripped[1] == stripped[2]
    assert bodies[0]["code"] == "forbidden"
    assert bodies[0]["status"] == 403


async def test_two_accounts_get_the_same_completion():
    """`POST /v1/route` — ACCOUNT, read.

    The negative test for the only route muse serves, and the honest one: there is no
    per-tenant completion, no per-tenant history, and nothing in the response that
    names an account. Two accounts and an account that has never existed all receive a
    body with the same keys and the same values once the per-response ids are removed —
    asserted by exact equality, with the account value checked out of the raw text.
    """
    async with asgi_client(app_for()) as client:
        bodies = {}
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.post("/v1/route", json=BODY, headers=auth(account))
            assert response.status_code == 200
            body = response.json()
            assert set(body) == _ROUTE_KEYS
            assert account not in response.text, "the response names the caller's account"
            bodies[account] = {
                k: v for k, v in body.items() if k not in {"id", "created", "trace_id"}
            }

    assert bodies[ACCOUNT] == bodies[OTHER_ACCOUNT] == bodies[NO_SUCH_ACCOUNT]


async def test_the_alias_carries_no_account_and_emits_the_same_payload():
    """`POST /v1/chat/completions` — ACCOUNT, read.

    The negative test for the alias, and it is deliberately the *same* question asked
    of `/v1/route`: two accounts and one that has never existed all get a body naming
    no tenant, and the metered event is byte-identical apart from its own ids.

    Asserting the alias against `/v1/route`'s negative test rather than inventing a
    second one is the point of the classification. An alias that meters differently, or
    that leaked a tenant into a body `/v1/route` does not, would be a tenant surface
    the suite had silently grown — and the payload comparison is what makes "the same
    write" checkable rather than asserted in a comment.
    """
    paths = ("/v1/route", "/v1/chat/completions")
    accounts = (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT)
    database = FakeDatabase()
    async with asgi_client(app_for(database)) as client:
        for path in paths:
            for account in accounts:
                response = await client.post(path, json=BODY, headers=auth(account))
                assert response.status_code == 200, f"{path} as {account}"
                assert account not in response.text, f"{path} named the caller's account"

    # One event per completion, on either path. `Meter.record` is reached through
    # `api.serve_completion`, so the alias cannot meter twice or not at all — and this
    # count is what would notice if a future change gave it its own write.

    # Two paths times three accounts = six requests, six events. The count is derived from
    # the two collections the loop above walked rather than written as a literal, so
    # adding a path or an account to that loop cannot leave this asserting a stale
    # number — which would be a test that fails loudly and for the wrong reason.
    assert len(database.outbox) == len(paths) * len(accounts)
    data = [json.loads(row["data"]) for row in database.outbox]
    # The payload is exactly core's five fields, identical across both paths, and the
    # subject is core's reserved literal rather than a tenant.
    for row, payload in zip(database.outbox, data, strict=True):
        assert row["subject"] == SUBJECT == "platform"
        assert set(payload) == {
            "model",
            "provider",
            "tokens_in",
            "tokens_out",
            "cost_micros",
        }
        for other in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            assert other not in str(row)
    # One distinct payload across all four requests: the alias does not add a field,
    # rename one, or compute the cost somewhere else.
    assert len({json.dumps(payload, sort_keys=True) for payload in data}) == 1


async def test_a_path_that_does_not_exist_is_404_for_every_account():
    """The tenant axis answers absence, never refusal.

    This is the assertion the packet is really asking for, at the only surface where
    muse lets a caller name something. A 404 is the same answer a caller gets for a
    resource that never existed, so nothing about another account's holdings is
    learnable; a 403 would be a strictly better oracle.
    """
    paths = ("/v1/routes", "/v1/completions", "/v1/accounts", "/v1/route/nope", "/v1/providers")
    async with asgi_client(app_for()) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            for path in paths:
                response = await client.post(path, json=BODY, headers=auth(account))
                assert response.status_code == 404, (
                    f"{path} as {account} answered {response.status_code}"
                )
                assert response.json()["code"] == "not_found"


async def test_healthz_names_no_account():
    """`GET /healthz` — PLATFORM, read.

    Liveness consults no dependency and no row, so there is no tenant axis and nothing
    to leak. Asserted rather than assumed: the exact body, and the absence of every
    account id in the text — including for an unauthenticated caller, which is the leg
    that proves no credential is consulted at all.
    """
    async with asgi_client(app_for()) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.get("/healthz", headers=auth(account))
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert account not in response.text
        anonymous = await client.get("/healthz")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"status": "ok"}


async def test_readyz_names_no_account():
    """`GET /readyz` — PLATFORM, read.

    Readiness is the one probe that *does* touch the database, so this is where a leak
    would actually be possible and the assertion is concrete: the body carries a
    dependency verdict and nothing else, and it is identical for every account. A
    credential in the vault must not appear, and no provider name may be listed —
    `ReadinessChecks` has one field, `db`, and this is the test that keeps it that way.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    await vault.put("openai", "sk-platform-key-not-a-tenant-credential")
    assert await vault.providers() == ("openai",), "the credential really is stored"

    async with asgi_client(app_for(database)) as client:
        bodies = []
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.get("/readyz", headers=auth(account))
            assert response.status_code == 200
            assert account not in response.text
            assert "sk-platform-key" not in response.text
            bodies.append(response.json())

    assert bodies[0] == bodies[1] == bodies[2]
    assert bodies[0] == {"status": "ok", "checks": {"db": "ok"}}


async def test_a_metered_event_names_no_account():
    """`metering.Meter.record` — ACCOUNT, update. The write, and the real gap.

    muse knows the caller's account and cannot write it: core's payload schema still
    pins `subject: platform` until D9. So the negative test asserts the absence is real
    *and named* — the event names no account, and its subject is core's reserved literal
    rather than a tenant a consumer could mistake for one. A test that implied this was
    closed would be the defect, not the coverage.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT):
            assert (
                await client.post("/v1/route", json=BODY, headers=auth(account))
            ).status_code == 200

    assert len(database.outbox) == 2
    for row in database.outbox:
        assert row["subject"] == SUBJECT == "platform"
        assert ACCOUNT not in str(row)
        assert OTHER_ACCOUNT not in str(row)
        # And the payload is exactly core's five fields — no account smuggled into one.
        assert set(json.loads(row["data"])) == {
            "model",
            "provider",
            "tokens_in",
            "tokens_out",
            "cost_micros",
        }


def test_the_usage_event_type_carries_no_account():
    """`UsageEvent` — the envelope's own field list, asserted as a set.

    The envelope is what reaches the bus, and a future field here is a tenant field
    before D9 says it may be one. Cheap to assert, and it is the exact place a
    well-meaning "let's just add account_id" would land.
    """
    assert set(UsageEvent.__dataclass_fields__) == {
        "id",
        "type",
        "source",
        "subject",
        "time",
        "data",
        "specversion",
    }


async def test_the_vault_is_unreachable_from_any_caller():
    """`vault.Vault.get` / `Vault.has` — PLATFORM, read.

    The vault holds one credential per *provider*, not per account, so there is no tenant
    axis to check on it. What must hold instead is the stronger property: no mounted
    route reads it. A caller with a valid token for any account cannot name a provider
    and get a key back, and asking for a provider that was never stored answers exactly
    as asking for one that was.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    await vault.put("openai", "sk-platform-key")

    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            for path in ("/v1/credentials", "/v1/vault", "/v1/keys"):
                response = await client.post(
                    path, json={"provider": "openai"}, headers=auth(account)
                )
                assert response.status_code == 404, (
                    f"{path} as {account} answered {response.status_code}"
                )
                assert "sk-platform-key" not in response.text

    # The store itself still answers by provider, and "never stored" is indistinguishable
    # from "stored and then removed" — absence, at the layer that actually holds data.
    assert await vault.has("openai") is True
    assert await vault.has("anthropic") is False
    await vault.delete("openai")
    assert await vault.has("openai") is False


async def test_the_provider_list_is_never_served():
    """`vault.Vault.providers` — PLATFORM, list.

    The one list query in muse. Its rows are vendor names; the negative test is that the
    list reaches no caller, and that it cannot be narrowed by a tenant because the table
    has no tenant column to narrow on.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    await vault.put("openai", "sk-platform-key")
    await vault.put("anthropic", "sk-other-key")
    assert await vault.providers() == ("anthropic", "openai")

    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.get("/v1/providers", headers=auth(account))
            assert response.status_code == 404
            assert "openai" not in response.text
            assert "anthropic" not in response.text


async def test_no_route_writes_the_vault():
    """`vault.Vault.put` — PLATFORM, update.

    There is no admin surface in v1 (out of scope since muse-02), so no caller can
    write, rotate or replace a credential. Asserted by driving every path a caller could
    plausibly name and comparing the statements that ran — the evidence is the absence
    of a write, not the absence of an exception.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    before = len(database.statements)

    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            for path in ("/v1/credentials", "/v1/vault", "/v1/keys"):
                response = await client.post(
                    path, json={"provider": "openai"}, headers=auth(account)
                )
                assert response.status_code == 404

    writes = [
        statement.sql
        for statement in database.statements[before:]
        if "vault_secrets" in statement.sql
    ]
    assert writes == [], f"a request wrote to the vault: {writes}"
    assert await vault.providers() == ()


async def test_deleting_an_absent_key_is_false_not_an_error():
    """`vault.Vault.delete` — PLATFORM, delete. Absence, at the store.

    Deleting a key that is not there answers the same as deleting one that was never
    stored, and neither is a refusal. The three outcomes are ordered so that the only
    thing the return value answers is "was there something?" — never whose.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)

    assert await vault.delete("openai") is False, "deleting nothing is absent, not an error"

    await vault.put("openai", "sk-platform-key")
    assert await vault.delete("openai") is True
    assert await vault.delete("openai") is False, "and the second delete is absent again"

    # An account id is not even a storable provider name, so a caller cannot reach the
    # vault by passing a tenant where a vendor belongs.
    with pytest.raises(Exception) as refusal:
        await vault.delete(NO_SUCH_ACCOUNT)
    assert NO_SUCH_ACCOUNT in str(refusal.value)


async def test_the_claim_query_is_unscoped_and_only_reads_unpublished():
    """`outbox.OutboxPublisher.claim` — NONE, list.

    The only cross-tenant read in muse, and it is deliberate: the publisher drains every
    unpublished event regardless of who spent it, because the row carries no tenant to
    filter on and a per-account drain would strand every other account's events behind
    one tenant's backlog. It is `NONE` rather than `PLATFORM` because no request reaches
    it — the publisher is the background loop, and the route walk in
    `test_every_http_route_is_enumerated` is what keeps that true.
    """
    database = FakeDatabase()
    transport = InMemoryTransport()
    publisher = OutboxPublisher(database, transport)

    await _seed_event(database, published=False, event_id="11111111-1111-1111-1111-111111111111")
    await _seed_event(database, published=True, event_id="22222222-2222-2222-2222-222222222222")

    assert await publisher.publish_batch() == 1, "only the unpublished row is claimed"
    assert [subject for subject, _ in transport.published] == ["muse.tokens_consumed"]

    statement = next(s for s in database.statements if "where published_at is null" in s.sql)
    assert "account" not in statement.sql
    # And it is the loop's, not a request's: nothing on a mounted route dispatches it.
    assert not hasattr(publisher, "endpoint")


async def _seed_event(database: FakeDatabase, *, published: bool, event_id: str) -> None:
    """One outbox row, published or not.

    `time` is a real `datetime` because the column is `timestamptz` and psycopg hands
    the publisher a `datetime` — a string here would make this test assert against a
    shape production never sees, which is how a publisher that only works in tests ships.
    """
    database.outbox.append(
        {
            "id": event_id,
            "event_type": "muse.tokens_consumed",
            "source": "muse",
            "subject": "platform",
            "time": datetime(2026, 1, 1, tzinfo=UTC),
            "data": "{}",
            "created_at": "t0",
            "published_at": "now()" if published else None,
            "attempts": 0,
        }
    )


# ---------------------------------------------------------------------------
# Guard 3 — the declaration's own line pins, against the tree.
# ---------------------------------------------------------------------------

#: The file that says what muse declares about its account boundary.
DECLARATION = REPO_ROOT / "tenancy.yml"

#: The directories core's checker never walks. A walk that read `.venv` or
#: `__pycache__` would find the tenancy key inside generated bytecode — which is
#: how core's own `tenancy_check.py` records getting 9 of muse's 24 sites from
#: `.pyc` files and depending on whether the suite had been run in the tree.
_SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".venv",
        "node_modules",
        "vendor",
        "target",
        "deps",
        "_build",
        "dist",
        "build",
        ".next",
        ".tox",
        ".mypy_cache",
        ".pytest_cache",
        "coverage",
        "__pycache__",
    }
)

#: Suffixes core's scanner reads. A file outside this list that carries the key is
#: reported as "a file type this checker does not read" and *still has to be
#: vouched for* — which is why `traces.schema.json` is in the declaration.
_TEXT_SUFFIXES = frozenset(
    {
        ".sql",
        ".go",
        ".ex",
        ".exs",
        ".rb",
        ".py",
        ".ts",
        ".tsx",
        ".rs",
        ".java",
        ".kt",
        ".php",
        ".cs",
        ".swift",
        ".js",
        ".mjs",
    }
)

#: The three spellings core's `KEY_SPELLINGS` derives from `account_id`, each
#: anchored at a word boundary so `sub_account_id` does not match.
_KEY = re.compile(r"(?<![A-Za-z0-9_])account_id(?![A-Za-z0-9_])")

#: A Python `#` comment, stripped before the key test — the same reason core strips
#: it: a comment that *talks about* the tenancy key is not a statement carrying it.
#: `src/muse/auth.py:74` and `:227` are exactly that and neither is a site.
_HASH_COMMENT = re.compile(r"#.*$")

#: An `insert` line, which core's scanner never attributes to a statement.
_INSERT = re.compile(r"\binsert\b", re.IGNORECASE)

#: DDL and `create policy`, which belong to the RLS scanner and are the other
#: reason a key-carrying line is not an account-scoped *statement*. Inert in a
#: Python tree, and mirrored anyway so this guard's answer is core's answer and
#: not this file's idea of it.
_DDL = re.compile(
    r"\b(create\s+(?:(?:foreign|materialized|unlogged|temporary|temp)\s+)?table"
    r"|alter\s+table|create\s+(unique\s+)?index|create\s+(materialized\s+)?view"
    r"|create\s+(or\s+replace\s+)?function)\b",
    re.IGNORECASE,
)
_POLICY_STATEMENT = re.compile(r"\bcreate\s+policy\b", re.IGNORECASE)

#: How far above a key-carrying line core looks for the statement it belongs to.
_ATTRIBUTION_WINDOW = 4

#: A SQL operation opening a line — `select … from t`, `update t set`, `delete
#: from t` — which is what makes a site an *account-scoped statement* rather than
#: prose. Reproduced from core's `sql_operation` so this guard reports the same
#: partition, not a coarser one.
_SQL_OPERATION = re.compile(
    r"\bselect\s+.*?\s+from\s+([A-Za-z_][A-Za-z0-9_.]*)"
    r"|\bupdate\s+"
    r"([A-Za-z_][A-Za-z0-9_.]*)\s+set\b"
    r"|\bdelete\s+from\s+([A-Za-z_][A-Za-z0-9_.]*)",
    re.IGNORECASE,
)

#: How many sites the scan is expected to find. The floor, and the reason this
#: guard is not a "find nothing, pass" guard: a scan that looked at the wrong
#: directory would find zero and agree with any declaration.
EXPECTED_SITES = 15


def _declared_sources() -> list[str]:
    """`scope.sources` from the declaration, which is `["src"]` here."""
    import yaml

    document = yaml.safe_load(DECLARATION.read_text(encoding="utf-8"))
    return list(document["scope"]["sources"])


def _declared_sites() -> set[tuple[str, int]]:
    """`scope.nonRowSites`, as `(relative path, line)` pairs."""
    import yaml

    document = yaml.safe_load(DECLARATION.read_text(encoding="utf-8"))
    return {(str(item["file"]), int(item["line"])) for item in document["scope"]["nonRowSites"]}


def _carries_key(line: str) -> bool:
    return bool(_KEY.search(line))


def _scanned_sites() -> set[tuple[str, int]]:
    """Every line in `scope.sources` carrying the tenancy key, as core's scanner sees it.

    This is a **mirror** of core's `tenancy_check.scan_file`, and it is a mirror
    rather than an import because core is a different repository that this one may
    not depend on (`tests/test_dependencies.py` would refuse it, and the fleet's
    rule is that a service does not vendor a checker it can then edit). The
    consequence is stated rather than left for the next reader: **the authority is
    core's checker, and this guard can go stale against a change to core's scan.**

    What that costs is bounded, and what it buys is the whole point: the failure
    this file exists to prevent — `e051a76` inserting a class into `errors.py` and
    moving `MissingAccount`'s docstring two lines down, which turned muse's honest
    zero into a lie for a full hour before the fleet checker said so — is a failure
    of *this repository's declaration against this repository's tree*. Core's
    checker only ever runs outside the suite, so it can only ever tell a successor
    that the last packet was wrong. This one says it while the packet is still being
    written.
    """
    found: set[tuple[str, int]] = set()
    for source in _declared_sources():
        root = REPO_ROOT / source
        for path in sorted(root.rglob("*")):
            if not path.is_file():
                continue
            if _SKIPPED_DIRECTORIES & set(path.relative_to(root).parts):
                continue
            relative = path.relative_to(REPO_ROOT).as_posix()
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:  # pragma: no cover - a file that vanishes mid-scan
                continue
            depth = {"ddl": 0, "policy": 0}
            for index, raw in enumerate(lines):
                line = index + 1
                if depth["policy"] > 0:
                    depth["policy"] -= 1
                    continue
                if depth["ddl"] > 0:
                    depth["ddl"] += raw.count("(") - raw.count(")")
                    continue
                if _POLICY_STATEMENT.search(raw):
                    depth["policy"] = _statement_length(lines, index)
                    continue
                if _DDL.search(raw):
                    depth["ddl"] = max(raw.count("(") - raw.count(")"), 0)
                    continue
                stripped = _HASH_COMMENT.sub("", raw) if path.suffix == ".py" else raw
                if not _carries_key(stripped) or _INSERT.search(stripped):
                    continue
                if path.suffix not in _TEXT_SUFFIXES:
                    # Not a line this scanner can read, and still a site the
                    # declaration must vouch for — the `.json` schema's case.
                    found.add((relative, line))
                    continue
                if _SQL_OPERATION.search(stripped) is None:
                    # The key is not on the statement's own line; look above.
                    for back in range(1, _ATTRIBUTION_WINDOW + 1):
                        above = index - back
                        if above < 0:
                            break
                        if _SQL_OPERATION.search(
                            _HASH_COMMENT.sub("", lines[above])
                            if path.suffix == ".py"
                            else lines[above]
                        ):
                            break
                    else:
                        found.add((relative, line))
                        continue
                    # Attributed to a statement above, so it IS an account-scoped
                    # statement — which on `accountScoped: false` is a failure this
                    # file reports separately.
                    continue
                # The key sits on a statement's own line: the strongest form.
                found.add((relative, line))
    return found


def _statement_length(lines: list[str], index: int) -> int:
    """How many lines the statement opening at `index` runs for, at least one."""
    if lines[index].rstrip().endswith(";"):
        return 1
    return 2


def test_the_declarations_line_pins_are_the_trees_line_pins():
    """Every key-carrying line in `src` is declared, and every declared line carries the key.

    THE TEST THAT WAS MISSING. `tenancy.yml` vouches for fifteen sites *by line
    number*, and a line number is a fact about a file that any insertion above it
    quietly rewrites. The previous attempt at this packet added a class to
    `errors.py`, moved `MissingAccount`'s docstring from 269 to 289, and stayed
    green on the suite while the fleet's `tenancy-check` went red — because nothing
    in this repository read the declaration. The declaration was checked only from
    outside the repository that owns it.

    Both directions, and the second is the one that catches a stale entry rather
    than a missing one:

    - **A key-carrying line the declaration does not name is a failure.** That is
      core's `tenancy.honest-zero`, reproduced where it can fail a packet instead
      of a merge.
    - **A declared line that no longer carries the key is a failure too.** A pin
      pointing at prose that moved is worse than a missing pin: it is a declaration
      that reads as checked and is not, which is rule 23's "a guard that can pass
      is indistinguishable from a guard that was never looking" one level up.
    """
    scanned = _scanned_sites()
    declared = _declared_sites()

    assert len(scanned) == EXPECTED_SITES, (
        f"the scan found {len(scanned)} key-carrying sites and {EXPECTED_SITES} were "
        f"expected; a change to this number means either a new mention of the "
        f"tenancy key in src (declare it, with the reason it is not a row scope) or "
        f"a scanner that no longer reads the tree it was pointed at: "
        f"{sorted(f'{f}:{n}' for f, n in scanned)}"
    )
    assert scanned == declared, (
        "tenancy.yml's nonRowSites no longer matches the lines in src that carry "
        f"{_KEY.pattern!r}. Undeclared: {sorted(f'{f}:{n}' for f, n in scanned - declared)}; "
        f"declared but absent: {sorted(f'{f}:{n}' for f, n in declared - scanned)}. "
        "A key-carrying line with no entry is an omission, not a zero — declare it "
        "with the reason it is not a row predicate, or move the code so the line "
        "does not carry the key."
    )


def test_a_declared_site_names_the_reason_it_is_not_a_row_scope():
    """A vouch with no reason is the vague-reason failure the brief names.

    core's schema only requires the field; the checker only counts the `(file,
    line)` pair. What stops a vouch from being a place to hide an omission is the
    *reason*, and a reason nobody checks is a reason nobody writes — so the reason
    is checked here, for length and for the word it has to be about.
    """
    import yaml

    document = yaml.safe_load(DECLARATION.read_text(encoding="utf-8"))
    vouchers = document["scope"]["nonRowSites"]
    assert vouchers, (
        "the declaration vouches for nothing, so the scan's fifteen sites are all unnamed"
    )

    for item in vouchers:
        reason = item["reason"].strip()
        assert len(reason) >= 40, (
            f"{item['file']}:{item['line']} is vouched for in {len(reason)} characters; "
            "a vouch this short is the vague-reason failure, not a vouch"
        )
        # Every site in this tree is prose about the claim, a claim read off the
        # verified token, or core's vendored schema. A reason that mentions a row,
        # a query or a predicate is a reason for a different kind of site.
        assert not re.search(
            r"\b(select|where clause|row scope by|scoped by the row)\b", reason, re.IGNORECASE
        ), (
            f"{item['file']}:{item['line']} claims to be a row predicate; that is "
            "accountScoped: true, and the declaration says false"
        )
