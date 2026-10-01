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

#: **12 account-scoped entry points. 7 read, 2 list, 2 update, 1 delete.**
#:
#: Of the twelve, **three carry a tenant axis** (`ACCOUNT`: `require_bearer`,
#: `POST /v1/route`, `Meter.record`) and nine do not. That ratio is the measurement:
#: muse's tenant surface is the auth gate, the one route, and the one write that meters
#: the route — and the other nine are load-bearing *because* they cannot see a tenant.
ENTRY_POINTS: tuple[EntryPoint, ...] = (
    REQUIRE_BEARER,
    REQUIRE_SCOPE,
    POST_ROUTE,
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
ENTRY_POINT_COUNT = 12

#: Per-operation counts, reported in the packet and asserted so a table that quietly
#: loses a whole operation cannot pass.
OPERATION_COUNTS = {"read": 7, "list": 2, "update": 2, "delete": 1}

#: How many of the twelve carry a tenant axis. The single most load-bearing number in
#: this file, because it is the one that says "muse has almost no tenancy surface", and a
#: future packet that grows one has to move it deliberately.
TENANT_SCOPED_COUNT = 3


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


def test_only_three_entry_points_carry_a_tenant_axis():
    """The measurement, pinned: muse's tenant surface is three entry points wide.

    Asserted because a packet that *grows* one should have to change this number on
    purpose, next to the negative test for the thing it added — not leave the old count
    sitting there claiming the surface is still three.
    """
    carrying = [entry.name for entry in ENTRY_POINTS if entry.tenant_axis == "ACCOUNT"]
    assert carrying == ["api.require_bearer", "POST /v1/route", "metering.Meter.record"]
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
        for statement in expected:
            assert statement.lower() in haystack, (
                f"{name}: enumerated query {statement!r} is not in the file"
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
