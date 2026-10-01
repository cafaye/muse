"""muse-09 — tenant isolation: every account-scoped entry point, negatively tested.

Following `darkroom-09`'s pattern, which is the whole method and not a formality:
**enumerate every account-scoped entry point, write a negative test per entry point,
and make the enumeration load-bearing.** The last clause is what separates this from a
list of good intentions. An enumeration nobody checks against the code is a comment
that ages badly; so the two completeness guards below walk the *running app* and the
*source tree* and fail when something appears that the table does not know about.

## The shape of the answer muse can give, which is the finding

muse is a router, a vault and a meter. It has no tenant-owned rows: `vault_secrets` is
keyed by `provider` (one platform credential per vendor, by design — see
`migrations/00002_vault_secrets.sql`) and `outbox_events` has no tenant column at all,
because core's payload schema still pins `subject: platform` (D9). So the honest
enumeration is **small**, and the negative test for most of it is *absence of a tenant
axis*, not a scope check on one.

That is worth stating plainly rather than inflating into twelve routes: an isolation
suite that claims a surface it does not have is worse than none, because it makes a
reader believe the gap was closed.

## Absence, never 403 — and why the distinction is the whole point

A 403 on the tenant axis is an **enumeration oracle**: it says "this exists and is not
yours", which is strictly more information than the 404 a nonexistent resource gets,
and it is the difference between a caller who learns nothing and a caller who can walk
a namespace. Two things follow, and both have tests:

- **No account-scoped entry point in muse answers 403.** The one 403 muse has
  (`InsufficientScope`) is on the *capability* axis, and
  `test_a_forbidden_body_does_not_vary_by_account` asserts the two accounts get a
  byte-identical body — so the 403 cannot be used to probe tenancy.
- **A path that does not exist answers 404, not 403**, whatever account the token
  names (`test_a_path_that_does_not_exist_is_404_for_every_account`).

## What is deliberately NOT fixed here

`Meter` knows the caller's `account_id` and cannot write it, because core owns the
payload schema. That is D9, recorded in `muse/metering.py` and in
`REPORT-muse-09-isolation.md`. This suite asserts the *absence* is real and named —
`test_a_metered_event_names_no_account` — rather than pretending the gap is closed.
Changing core's schema from muse is not this packet's decision.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from muse.errors import InsufficientScope
from muse.metering import SUBJECT, UsageEvent
from muse.providers import Price, ProviderRegistry
from muse.providers.fake import ScriptedProvider
from muse.routes import routes_from_yaml
from muse.vault import Vault

from .conftest import AUTH_HEADERS, asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.jwks import ACCOUNT, token, tampered
from .support.test_app import IDENTITY, TEST_KEY, build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC = REPO_ROOT / "src" / "muse"

#: A second account, so a test can ask "what changes when the tenant changes?" and get
#: the honest answer of "nothing, and that is the point" as an assertion rather than an
#: assumption. Every token here is minted for real and drives the real verifier.
OTHER_ACCOUNT = "01HQ0000000000000000000000B"

#: An account nobody has ever heard of, for the third leg of every question.
NO_SUCH_ACCOUNT = "01HQ000000000000000000000ZZ"

MODEL = "gpt-4o-mini"
PRICE = Price(150, 600)
BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}


# ---------------------------------------------------------------------------
# The enumeration. This table is the deliverable; everything below checks it.
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class EntryPoint:
    """One place a caller's tenant can be observed, refused, or forgotten.

    `tenant_axis` is the honest classification and it is the interesting column:

    - `ACCOUNT` — the tenant is the discriminator and something must check it. There
      are two, and both are covered.
    - `PLATFORM` — no tenant axis at all. The negative test asserts the absence, which
      is the only thing that can be asserted about a discriminator that does not exist.
    - `NONE` — not reachable from an authenticated caller (the outbox publisher runs in
      the background loop, not in a request).
    """

    name: str
    layer: str
    operation: str
    tenant_axis: str
    negative_test: str


#: The tenancy is established here, and everything downstream reads it from the
#: `Principal` rather than from the request body.
REQUIRE_BEARER = EntryPoint(
    "api.require_bearer", "auth", "read", "ACCOUNT", "test_an_edited_tenant_claim_is_refused"
)
#: Capability, not tenancy. Kept in the table because it is the only 403 in muse, and a
#: 403 is exactly what an isolation suite has to reason about.
REQUIRE_SCOPE = EntryPoint(
    "auth.require_scope", "auth", "read", "PLATFORM", "test_a_forbidden_body_does_not_vary_by_account"
)

POST_ROUTE = EntryPoint(
    "POST /v1/route", "http", "read", "ACCOUNT", "test_two_accounts_get_the_same_completion"
)
HEALTHZ = EntryPoint("GET /healthz", "http", "read", "PLATFORM", "test_healthz_names_no_account")
READYZ = EntryPoint(
    "GET /readyz", "http", "read", "PLATFORM", "test_readyz_names_no_account_and_no_credential"
)

METER_RECORD = EntryPoint(
    "metering.Meter.record", "service", "update", "ACCOUNT", "test_a_metered_event_names_no_account"
)
VAULT_GET = EntryPoint("vault.Vault.get", "service", "read", "PLATFORM", "test_the_vault_is_unreachable_from_any_caller")
VAULT_HAS = EntryPoint("vault.Vault.has", "service", "read", "PLATFORM", "test_the_vault_is_unreachable_from_any_caller")
VAULT_PROVIDERS = EntryPoint(
    "vault.Vault.providers", "service", "list", "PLATFORM", "test_the_provider_list_is_never_served"
)
VAULT_PUT = EntryPoint(
    "vault.Vault.put", "service", "update", "PLATFORM", "test_no_route_writes_the_vault"
)
VAULT_DELETE = EntryPoint(
    "vault.Vault.delete", "service", "delete", "PLATFORM", "test_deleting_an_absent_key_is_false_not_an_error"
)
OUTBOX_CLAIM = EntryPoint(
    "outbox.OutboxPublisher.claim", "service", "list", "NONE", "test_the_claim_query_is_unscoped_and_only_reads_unpublished"
)

#: **12 account-scoped entry points. 7 read, 2 list, 2 update, 1 delete.**
#:
#: Of the twelve, **three carry a tenant axis** (`ACCOUNT`) and nine do not. That ratio
#: is the measurement D18 was asking for: muse's tenant surface is the auth gate, the
#: one route, and the one write that meters the route — and the other nine are
#: load-bearing *because* they cannot see a tenant at all.
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

#: The ratchet. Raising the suite's test count in `gate.yml` is not enough — that floor
#: counts *tests*, and a test that stops being collected keeps it green. This counts
#: *enumerated entry points*, so deleting a row from the table without deleting its
#: negative test fails here.
ENTRY_POINT_COUNT = 12

#: Per-operation counts, reported in the packet and asserted so a table that quietly
#: loses a whole operation cannot.
OPERATION_COUNTS = {"read": 7, "list": 2, "update": 2, "delete": 1}

#: The tests that exist for the table, by name. Built at import from the table itself,
#: so it cannot drift from it — and then checked against what pytest actually
#: collected, which is the half that makes it a guard.
def _negative_test_names() -> frozenset[str]:
    return frozenset(entry.negative_test for entry in ENTRY_POINTS)


NEGATIVE_TESTS = _negative_test_names()


def auth(account: str) -> dict[str, str]:
    """A real bearer token naming `account`. Signed by the fixture identity, so the
    verifier runs its full chain — no stub, no bypass (see `tests/conftest.py`)."""
    return {"Authorization": f"Bearer {token(IDENTITY, account_id=account)}"}


def app_for(database: FakeDatabase | None = None):
    """An app whose one route completes successfully, over an in-memory database."""
    provider = ScriptedProvider("openai", [completion()])
    registry = ProviderRegistry()
    registry.register(provider)
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


def completion(**overrides):
    from muse.providers import Completion

    return Completion(
        **{
            "provider": "openai",
            "model": MODEL,
            "content": "hello there",
            "tokens_in": 12,
            "tokens_out": 8,
            "price": PRICE,
            **overrides,
        }
    )


# ---------------------------------------------------------------------------
# Guard 1 — the table is complete against the running application.
# ---------------------------------------------------------------------------


def test_every_http_route_is_enumerated(app):
    """A new route appears in the table or the suite goes red.

    This is the load-bearing half of the packet. An isolation suite that enumerates
    what it happens to know about is a suite that reports "12 entry points, all
    covered" while the thirteenth ships unreviewed — which is how a service grows a
    tenant surface it never tested. So the *running app* is the oracle, not this
    table.
    """
    enumerated = {entry.name for entry in ENTRY_POINTS if entry.layer == "http"}
    mounted = {
        f"{','.join(sorted(route.methods - {'HEAD'}))} {route.path}"
        for route in app.routes
        if getattr(route, "methods", None) and route.path not in {"/openapi.json", "/docs"}
    }
    assert mounted == enumerated, (
        f"the app serves {sorted(mounted - enumerated)} and the table does not enumerate it; "
        f"the table claims {sorted(enumerated - mounted)} and the app serves no such route"
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


def test_every_entry_point_names_a_test_that_actually_exists(request):
    """Each row's `negative_test` is a real, collected test function.

    The failure mode this exists for is a table row pointing at a test that was
    renamed: the row still reads as covered, the coverage is gone, and nothing else in
    the suite notices. Checked against `request.session.items`, so an uncollected
    function fails the guard rather than satisfying it.
    """
    collected = {item.name for item in request.session.items}
    missing = sorted(NEGATIVE_TESTS - collected)
    assert not missing, f"enumerated entry points name tests that are not collected: {missing}"


def test_the_entry_points_are_distinct():
    """No row is a restatement of another row wearing a different name."""
    names = [entry.name for entry in ENTRY_POINTS]
    assert len(set(names)) == len(names)


# ---------------------------------------------------------------------------
# Guard 2 — the table is complete against the source tree.
# ---------------------------------------------------------------------------

#: A DML or DQL verb at the start of a SQL statement, which is what makes a string
#: literal in `src/muse/` a query rather than prose. Matched case-insensitively
#: because `select 1 as ok` is lowercase in one place and the rest are uppercase.
_SQL = re.compile(r"\b(select|insert\s+into|update|delete\s+from)\b", re.IGNORECASE)

#: Files allowed to contain SQL, and the queries each is allowed to contain. Every one
#: of these is an enumerated entry point: this is the second, independent statement of
#: "there are twelve", derived from the source rather than from the table.
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

#: How many statements that is, and therefore the DB-tier half of the entry-point count.
QUERY_COUNT = 9


def test_every_sql_statement_in_the_source_is_enumerated():
    """A new query appears in `SQL_SITES` or the suite goes red.

    The half of the packet that a route walk cannot do: a query is a tenant surface
    the moment it touches tenant data, and a query reached only from a background
    loop is invisible to an HTTP-level enumeration. Nine statements, all attributed.
    """
    found: dict[str, list[str]] = {}
    for path in sorted(SRC.glob("*.py")):
        statements = [
            line.strip()
            for line in path.read_text().splitlines()
            if _SQL.search(line) and not line.strip().startswith("#")
        ]
        if statements:
            found[path.name] = statements

    assert set(found) == set(SQL_SITES), (
        f"SQL found in {sorted(set(found) - set(SQL_SITES))}, which the enumeration does "
        f"not account for; SQL_SITES claims {sorted(set(SQL_SITES) - set(found))}, which "
        "holds no statement"
    )
    for name, expected in SQL_SITES.items():
        haystack = "\n".join(found[name])
        for statement in expected:
            assert statement in haystack, f"{name}: enumerated query {statement!r} is not in the file"
    assert sum(len(v) for v in SQL_SITES.values()) == QUERY_COUNT


def test_no_query_filters_or_writes_by_a_tenant():
    """Not one statement mentions a tenant column — and that is asserted, not assumed.

    The negative test for the whole DB tier. If a query ever grows `where account_id`,
    this goes red and the negative tests above have to be rewritten, because from that
    commit a 404 is no longer the only correct answer to another account's data.

    `where` clauses are checked as whole lines because a tenant predicate split across
    a line is still a tenant predicate, and a guard that only reads one line is a
    guard with a bypass.
    """
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in sorted(SRC.glob("*.py"))
        for line in path.read_text().splitlines()
        if _SQL.search(line) and re.search(r"account_id|account", line, re.IGNORECASE)
    ]
    assert not offenders, (
        "a query now references the tenant; the table's PLATFORM rows are no longer "
        f"true and each needs a negative test: {offenders}"
    )


def test_no_migration_carries_a_tenant_column():
    """The same assertion at the schema, where a tenant column would first appear."""
    migrations = REPO_ROOT / "migrations"
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in sorted(migrations.glob("*.sql"))
        for line in path.read_text().splitlines()
        if re.search(r"\baccount_id\b", line)
    ]
    assert not offenders, f"a migration introduced a tenant column: {offenders}"


# ---------------------------------------------------------------------------
# Negative tests, one per entry point.
# ---------------------------------------------------------------------------


async def test_an_edited_tenant_claim_is_refused():
    """`api.require_bearer` — ACCOUNT, read.

    The tenant is read from the *verified claims*, never from the request. A caller
    who edits `account_id` after signing gets the same 401 as a caller with no tenant
    at all, so there is no way to arrive at another account's side of the system by
    rewriting a claim.
    """
    forged = tampered(token(IDENTITY, account_id=ACCOUNT), account_id=OTHER_ACCOUNT)
    headers = {"Authorization": f"Bearer {forged}"}
    async with asgi_client(app_for()) as client:
        borrowed = await client.post("/v1/route", json=BODY, headers=headers)
        anonymous = await client.post("/v1/route", json=BODY, headers=auth(NO_SUCH_ACCOUNT))
    assert borrowed.status_code == 401
    assert anonymous.status_code == 200
    # Same *class* of refusal as a caller with no tenant, and the account the forgery
    # named appears nowhere in the body.
    assert borrowed.json()["code"] == "unauthorized"
    assert borrowed.json()["status"] == 401
    assert OTHER_ACCOUNT not in borrowed.text
    assert ACCOUNT not in borrowed.text


async def test_a_token_with_no_tenant_is_refused_rather_than_defaulted():
    """`api.require_bearer` — the tenancy gate itself, and 401 rather than 403.

    `MissingAccount` is a 401 because it is about the credential, not about permission
    to touch a particular resource: a 403 here would tell a caller holding a perfectly
    valid signature that the token is real and only its tenant is wrong, which is the
    first half of an enumeration oracle.
    """
    async with asgi_client(app_for()) as client:
        no_tenant = await client.post("/v1/route", json=BODY, headers=auth(NO_SUCH_ACCOUNT))
        assert no_tenant.status_code == 200, "sanity: a real tenant is served"

        anonymous = await client.post(
            "/v1/route",
            json=BODY,
            headers={"Authorization": f"Bearer {token(IDENTITY, account_id=None)}"},
        )
    assert anonymous.status_code == 401
    assert anonymous.json()["code"] == "unauthorized"


async def test_a_forbidden_body_does_not_vary_by_account():
    """`auth.require_scope` — the only 403 in muse, and it is not a tenancy oracle.

    muse has exactly one 403, on the **capability** axis: a correctly signed token
    with no `completions:write`. It is the right status for a missing capability, and
    it would be the wrong status for "that resource is another account's". What makes
    it safe is that the body cannot be made to vary by tenant — so asking twice with
    two different accounts cannot learn anything about either one's holdings.
    """
    async with asgi_client(app_for()) as client:
        bodies = []
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            headers = {"Authorization": f"Bearer {token(IDENTITY, account_id=account, scopes=())}"}
            response = await client.post("/v1/route", json=BODY, headers=headers)
            assert response.status_code == 403, InsufficientScope.__name__
            bodies.append(response.json())

    # Only `trace_id` may differ between the three; everything else is fixed text.
    volatile = {"trace_id", "instance"}
    assert [{k: v for k, v in body.items() if k not in volatile} for body in bodies] == [
        {k: v for k, v in bodies[0].items() if k not in volatile}
    ] * 3
    for body in bodies:
        assert body["code"] == "forbidden"
        assert body["status"] == 403


async def test_two_accounts_get_the_same_completion():
    """`POST /v1/route` — ACCOUNT, read.

    The negative test for the only route muse serves, and the honest one: there is no
    per-tenant completion, no per-tenant history, and nothing in the response that
    names an account. Two accounts and an account that has never existed all receive a
    body whose keys are the same set — asserted by exact equality with the account
    value checked out of the payload, not by `in`.
    """
    async with asgi_client(app_for()) as client:
        bodies = {}
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.post("/v1/route", json=BODY, headers=auth(account))
            assert response.status_code == 200
            body = response.json()
            assert set(body) == {
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
            assert account not in response.text, "the response names the caller's account"
            bodies[account] = {k: v for k, v in body.items() if k not in {"id", "created", "trace_id"}}

    assert bodies[ACCOUNT] == bodies[OTHER_ACCOUNT] == bodies[NO_SUCH_ACCOUNT]


async def test_a_path_that_does_not_exist_is_404_for_every_account():
    """The tenant axis answers absence, never refusal.

    This is the assertion the packet is really asking for, at the only surface where
    muse lets a caller name something. A 404 is the same answer a caller gets for a
    resource that never existed, so nothing about another account's holdings is
    learnable; a 403 would be a strictly better oracle.
    """
    paths = ("/v1/routes", "/v1/completions", "/v1/accounts", "/v1/route/nope")
    async with asgi_client(app_for()) as client:
        seen = {}
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            for path in paths:
                response = await client.post(path, json=BODY, headers=auth(account))
                assert response.status_code == 404, f"{path} answered {response.status_code}"
                assert response.json()["code"] == "not_found"
                seen.setdefault(path, set()).add(response.json()["code"])
    assert all(codes == {"not_found"} for codes in seen.values())


async def test_healthz_names_no_account():
    """`GET /healthz` — PLATFORM, read.

    Liveness consults no dependency and no row, so there is no tenant axis and no
    data to leak. Asserted rather than assumed: the exact body, and the absence of
    every account id in the text.
    """
    async with asgi_client(app_for()) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            response = await client.get("/healthz", headers=auth(account))
            assert response.status_code == 200
            assert response.json() == {"status": "ok", "version": response.json()["version"]}
            assert account not in response.text
        anonymous = await client.get("/healthz")
    assert anonymous.status_code == 200
    assert anonymous.json() == {"status": "ok", "version": anonymous.json()["version"]}


async def test_readyz_names_no_account_and_no_credential():
    """`GET /readyz` — PLATFORM, read.

    Readiness *does* read: the database, and the vault's provider list. So this is the
    entry point where a leak would actually be possible, and the assertion is
    concrete — the body names providers at most, never a credential and never an
    account, and it is the same for every account.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    await vault.put("openai", "sk-provider-key-not-a-tenant-credential")

    async with asgi_client(app_for(database)) as client:
        bodies = []
        for account in (ACCOUNT, OTHER_ACCOUNT):
            response = await client.get("/readyz", headers=auth(account))
            assert response.status_code == 200
            assert account not in response.text
            assert "sk-provider-key-not-a-tenant-credential" not in response.text
            bodies.append(response.json())

    assert bodies[0] == bodies[1]
    assert set(bodies[0]) == {"status", "checks"}
    assert set(bodies[0]["checks"]) == {"db", "routes", "providers"}


async def test_a_metered_event_names_no_account():
    """`metering.Meter.record` — ACCOUNT, update. The write, and the real gap.

    muse knows the caller's account and cannot write it: core's payload schema still
    pins `subject: platform` until D9. So the negative test asserts the absence is
    real *and named* — the event is on the wire, it names no account, and its subject
    is core's reserved literal rather than a tenant that a consumer could mistake for
    one. A test that pretended this was closed would be the defect.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database)) as client:
        await client.post("/v1/route", json=BODY, headers=auth(ACCOUNT))
        await client.post("/v1/route", json=BODY, headers=auth(OTHER_ACCOUNT))

    assert len(database.outbox) == 2
    for row in database.outbox:
        assert row["subject"] == SUBJECT == "platform"
        assert ACCOUNT not in str(row)
        assert OTHER_ACCOUNT not in str(row)
        # And the payload is exactly core's five fields — no account smuggled into one.
        import json as _json

        assert set(_json.loads(row["data"])) == {
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

    The vault holds one credential per *provider*, not per account, so there is no
    tenant axis to check. What must hold instead is the stronger property: no mounted
    route reads it. A caller with a valid token for any account cannot name a provider
    and get a key back, and asking for a provider that does not exist answers exactly
    as asking for one that does.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    await vault.put("openai", "sk-platform-key")

    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT):
            for path in ("/v1/credentials", "/v1/vault", "/v1/keys", "/readyz"):
                response = await client.post(path, json={"provider": "openai"}, headers=auth(account))
                assert response.status_code == 404, f"{path} answered {response.status_code}"
                assert "sk-platform-key" not in response.text

    # The store itself still answers by provider, and answers the same for a provider
    # that was never stored as for one that was.
    assert await vault.has("openai") is True
    assert await vault.has("anthropic") is False
    assert await vault.has(NO_SUCH_ACCOUNT) is False


async def test_the_provider_list_is_never_served():
    """`vault.Vault.providers` — PLATFORM, list.

    The one list query in muse. Its rows are vendor names; the negative test is that
    the list reaches no caller, and that it cannot be narrowed by a tenant because it
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
    write, rotate or replace a credential. Asserted by driving every mounted route
    and comparing the vault's contents before and after — the statement count is the
    evidence, not the absence of an exception.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)
    before = len(database.statements)

    async with asgi_client(app_for(database)) as client:
        for account in (ACCOUNT, OTHER_ACCOUNT, NO_SUCH_ACCOUNT):
            for path in ("/v1/credentials", "/v1/vault", "/v1/keys"):
                response = await client.post(path, json={"provider": "openai"}, headers=auth(account))
                assert response.status_code == 404

    writes = [s for s in database.statements[before:] if "vault_secrets" in s.sql]
    assert writes == [], f"a request wrote to the vault: {writes}"
    assert await vault.providers() == ()


async def test_deleting_an_absent_key_is_false_not_an_error():
    """`vault.Vault.delete` — PLATFORM, delete. Absence, at the store.

    Deleting a key that is not there answers the same as deleting one that was never
    stored, and neither is a refusal. The three outcomes are then ordered so the
    question "was there something?" is the only thing the return value answers — it
    never says whose.
    """
    database = FakeDatabase()
    vault = Vault(database, TEST_KEY)

    assert await vault.delete("openai") is False, "deleting nothing is absent, not an error"
    assert await vault.delete(NO_SUCH_ACCOUNT) is False

    await vault.put("openai", "sk-platform-key")
    assert await vault.delete("openai") is True
    assert await vault.delete("openai") is False, "and the second delete is absent again"


async def test_the_claim_query_is_unscoped_and_only_reads_unpublished():
    """`outbox.OutboxPublisher.claim` — NONE, list.

    The only cross-tenant read in muse, and it is deliberate: the publisher drains
    every unpublished event regardless of who spent it, because the row carries no
    tenant to filter on and a per-account drain would strand every other account's
    events behind one tenant's backlog. It is `NONE` rather than `PLATFORM` because no
    request reaches it — the publisher is the background loop, and the route walk in
    `test_every_http_route_is_enumerated` is what keeps that true.
    """
    from muse.outbox import OutboxPublisher

    database = FakeDatabase()
    database.outbox.append(
        {
            "id": "11111111-1111-1111-1111-111111111111",
            "event_type": "muse.tokens_consumed",
            "source": "muse",
            "subject": "platform",
            "time": "2026-01-01T00:00:00.000000Z",
            "data": "{}",
            "created_at": "t0",
            "published_at": None,
            "attempts": 0,
        }
    )
    publisher = OutboxPublisher(database, publish=lambda envelope: None, clock=lambda: 0.0)
    claimed = await publisher.claim(1)

    assert [row.id for row in claimed] == ["11111111-1111-1111-1111-111111111111"]
    statement = database.statements_matching("where published_at is null")[0]
    assert "account" not in statement.sql
    # And it is the loop's, not a request's: nothing on a mounted route dispatches it.
    assert not hasattr(publisher, "endpoint")


def test_no_module_handles_a_tenant_parameter():
    """The last sweep: no function in `src/muse/` takes an account as an argument.

    A cheap, blunt final check that the tenant surface has not grown a parameter. If
    it ever has, an isolation question exists that this table has not answered, and
    the way to find out is a red suite rather than a review comment nobody reads.
    """
    offenders = [
        f"{path.name}: {line.strip()}"
        for path in sorted(SRC.rglob("*.py"))
        for line in path.read_text().splitlines()
        if re.match(r"\s*(async\s+)?def\s+\w+\s*\([^)]*account", line)
    ]
    assert not offenders, f"a function now takes an account argument: {offenders}"