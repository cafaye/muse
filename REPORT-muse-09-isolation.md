# REPORT — muse-09: tenant isolation, negative tests first

**Packet:** `muse-09` (D18, following `darkroom-09`)
**Branch:** `worker/muse-09-isolation`
**Worktree:** `/Users/kaka/Code/any/moon/cafaye/muse-worker-muse-09-isolation`
**Pushed:** no. Merge is the manager's.

**D18 measured muse at 0 cross-tenant negative tests.** This is the enumeration, the
negative test per entry point, and the guards that keep the enumeration from going
stale. **`src/` is unchanged** — no 403 was found on the tenant axis, so nothing needed
fixing. See *Findings* for what was found and deliberately not changed.

---

## 1. The enumeration: 12 account-scoped entry points

**Per-operation counts: read 7, list 2, update 2, delete 1.**

| # | Entry point | Layer | Op | Tenant axis | Negative test |
| --- | --- | --- | --- | --- | --- |
| 1 | `api.require_bearer` | auth | read | **ACCOUNT** | `test_an_edited_tenant_claim_is_refused` |
| 2 | `auth.require_scope` | auth | read | PLATFORM | `test_a_forbidden_body_does_not_vary_by_account` |
| 3 | `POST /v1/route` | http | read | **ACCOUNT** | `test_two_accounts_get_the_same_completion` |
| 4 | `GET /healthz` | http | read | PLATFORM | `test_healthz_names_no_account` |
| 5 | `GET /readyz` | http | read | PLATFORM | `test_readyz_names_no_account` |
| 6 | `metering.Meter.record` | service | update | **ACCOUNT** | `test_a_metered_event_names_no_account` |
| 7 | `vault.Vault.get` | service | read | PLATFORM | `test_the_vault_is_unreachable_from_any_caller` |
| 8 | `vault.Vault.has` | service | read | PLATFORM | `test_the_vault_is_unreachable_from_any_caller` |
| 9 | `vault.Vault.providers` | service | list | PLATFORM | `test_the_provider_list_is_never_served` |
| 10 | `vault.Vault.put` | service | update | PLATFORM | `test_no_route_writes_the_vault` |
| 11 | `vault.Vault.delete` | service | delete | PLATFORM | `test_deleting_an_absent_key_is_false_not_an_error` |
| 12 | `outbox.OutboxPublisher.claim` | service | list | NONE | `test_the_claim_query_is_unscoped_and_only_reads_unpublished` |

**Only three carry a tenant axis: `require_bearer`, `POST /v1/route`, `Meter.record`.**
That ratio is the measurement, and it is the honest one rather than an inflated count.
muse is a router, a vault and a meter, and it stores **no tenant-owned rows**:

- `vault_secrets` is keyed by `provider` — one credential per *vendor*, by design
  (`migrations/00002_vault_secrets.sql`: "One row per provider, one API key per row").
- `outbox_events` has **no tenant column**, because core's payload schema still pins
  `subject: platform` until D9 (`src/muse/metering.py`'s module docstring).

So nine of the twelve are classified `PLATFORM` — no tenant axis at all — and their
negative test asserts *the absence*. That is the only thing assertable about a
discriminator that does not exist, and pretending otherwise would be the defect.

The classification is load-bearing, not documentation:
`test_only_three_entry_points_carry_a_tenant_axis` pins it, so a packet that grows a
fourth tenant surface has to change that number deliberately, next to the negative test
for whatever it added.

### How the enumeration was derived

Not by reading `api.py` and counting routes. Two independent oracles:

- **HTTP**: `app.openapi()["paths"]` from the running app. The generated document is the
  service's own statement of its surface and the thing the SDKs are generated from. (Not
  `app.routes`: this FastAPI version represents an included router as an opaque
  `_IncludedRouter` with no `.routes`, so a route walk silently finds none of `POST
  /v1/route`. That is a guard that passes by finding nothing, found the hard way.)
- **DB**: `ast`-parsed string literals in `src/muse/**/*.py`, so a query is
  distinguished from the word "update" in a comment explaining why a query was added.
  **Nine statements**, attributed: `main.py` 1, `metering.py` 1, `outbox.py` 3,
  `vault.py` 4.

---

## 2. Absence, never 403

A 403 on the tenant axis is an enumeration oracle: it says *this exists and is not
yours*, which is strictly more information than the 404 a nonexistent resource gets.

- **No account-scoped entry point answers 403.** muse's single 403 is
  `InsufficientScope`, on the **capability** axis. It is the right status for a missing
  capability and the wrong status for "that resource is another account's". What makes
  it safe is that its body cannot vary by tenant —
  `test_a_forbidden_body_does_not_vary_by_account` drives three accounts and asserts the
  bodies are equal once `trace_id` and `instance` are removed.
- **A path that does not exist answers 404 for every account.** Five paths × three
  accounts, including one that differs only in shape (`/v1/route/nope`).
- **`MissingAccount` is a 401.** A 403 there would tell a caller holding a valid
  signature that the token is real and only its tenant is wrong — the first half of an
  enumeration oracle.

---

## 3. Coverage per operation

| Op | Count | Entry points | What the negative test establishes |
| --- | --- | --- | --- |
| read | 7 | 1, 2, 3, 4, 5, 7, 8 | no response or store read names another account; an edited `account_id` is a 401 |
| list | 2 | 9, 12 | the provider list reaches no caller; the outbox claim drains unpublished rows only and is not on any route |
| update | 2 | 6, 10 | the metered payload is core's five fields and names no account; driving every plausible path writes **zero** vault statements |
| delete | 1 | 11 | absent is `False`, and `False` again after a delete — never a refusal |

---

## 4. DB tier: what ran, what skipped, and on which variables

**No database is required.** `tests/support/fake_database.py` is an in-memory store and
`bin/prime` opens no socket (AGENTS.md rule 3). This is stated rather than discovered.

| Tier | Variable | Result |
| --- | --- | --- |
| self-contained | *(none)* | **930 passed** |
| core-parity | `MUSE_CORE_SCHEMAS` | **933 passed, 0 skipped** |

Gate run with `MUSE_CORE_SCHEMAS=../core/schemas bin/prime`:

```
933 passed in 31.75s          coverage 100.00% (branch)
```

Without it: `930 passed, 3 skipped in 43.21s`, coverage 100.00%.

The **3 skips** are the pre-existing core-parity tests and nothing else:
`test_contracts.py::test_patterns_are_byte_identical_to_core`, the telemetry-vocabulary
drift guard in `test_error_vocabulary.py`, the manifest-drift guard in `test_openapi.py`.

`gate-check --prove`:

- `MUSE_CORE_SCHEMAS=../core/schemas ../core/harness/bin/gate-check --prove .` →
  **OK, 0 failures, 2 warnings** (the two expected `gate.requirement-unproven`).
- `../core/harness/bin/gate-check --prove .` (no core checkout) →
  **FAIL `gate.proof-missing`**: *"proof 'core-parity' never appeared"*. The skipped tier
  is still a red, not a green.

---

## 5. Findings

### No 403 on the tenant axis — nothing in `src/` needed changing

Searched for the property rather than assuming it. Every account-scoped path answers 401,
404, or 200-with-no-tenant-data. `src/` is **byte-identical** to `7fe725f` apart from
nothing at all: this packet adds tests, `CHANGELOG.md`, `gate.yml`, `AGENTS.md` and this
report.

### FINDING 1 — every served completion is metered without the tenant it was spent by

**Severity: real, named, and owned by core (D9). Not fixed here, and deliberately so.**

`Meter` receives the route's result and knows nothing about the caller's `account_id`.
The envelope carries `subject: "platform"` — core's reserved literal for "no single
entity" — and a `data` payload closed at five fields. So **every row in
`outbox_events` is unattributable to a customer**: `billing` cannot aggregate an invoice
from these events, because the account is not in them.

The temptation is to add `account_id` to `data` or set `subject` to the account. Both
would be muse publishing a contract core has not agreed to, and `billing` aggregates
against the shape core published. The fix is core's.

What this packet does instead: **asserts the absence is real and named**
(`test_a_metered_event_names_no_account` — payload is exactly core's five fields, subject
is the literal, neither account appears anywhere in the row) and **pins the schema**
(`test_no_migration_carries_a_tenant_column`, `test_no_query_filters_or_writes_by_a_tenant`).
When D9 lands and core permits it, these three tests are the ones that must be
deliberately inverted, and they will be the first thing to go red.

### FINDING 2 — a guard that passed because it found nothing, twice

Both found by **breaking them on purpose**, not by reading them (AGENTS.md rule 23).

**(a) The SQL guard could not see an extra query.** It checked that every enumerated
query was present and that every file containing SQL was accounted for — but never that
a file held no *more* than the enumeration named. A canary appending a second,
tenant-free `select count(*) from outbox_events` to `metering.py` **went green**. Fixed
by asserting the per-file count, with the unnamed statements in the failure message.

**(b) 24 of the 26 tests were never collected.** The file was first written as
`tests/tenant_scoping.py`, mirroring `darkroom-09`'s Rust layout where every `.rs` under
`tests/` is a target by convention. Python has no such convention: `pyproject.toml` sets
`testpaths` and **not** `python_files`, so pytest's default `test_*.py` / `*_test.py`
applies. The whole file was skipped and the gate read **`904 passed, 3 skipped` before
and after** the commit that added it — a green gate over a suite that did not run, which
is the precise identity defect `gate.yml` exists to prevent, arrived at from the other
direction.

Fixed by renaming to `tests/test_tenant_scoping.py`, and by two new guards:

- `test_no_test_file_is_left_uncollected` walks `tests/` and fails on any module defining
  `def test_` that does not match the collection pattern. Canary: a stray
  `tests/tenant_canary.py` goes red, confirmed.
- `test_this_file_is_itself_collected` asserts this module is in the collected set,
  because a guard that can pass because *it* is not running is the worst version of the
  failure.

This is also why the `gate.yml` floor moved. A `core-parity` floor left at 907 would have
cleared the commit that added 26 tests and ran none of them — **the count never moved, so
nothing could notice.** See §6.

---

## 6. Gate and the ratchet

`gate.yml` `proof[].core-parity.minimum`: **907 → 933**, raised in the same commit as the
tests, as the ratchet clause requires.

The clause exists because a floor is only "exact" relative to the suite's current size.
Here it caught a real defect rather than guarding a hypothetical one: muse-08 added 9
compose tests and a floor left at 898 would have cleared a run with one of the three
drift guards deleted. muse-09's version of that: a floor left at 907 clears a commit that
made the suite 26 tests bigger and ran none of them, because the number did not change.
**A reviewer adding tests must raise this floor in the same commit**, and after FINDING
2(b) the failure it guards is one that has already happened once.

Unchanged: `proof[].suite.minimum: 890` (still below 930, so a new test does not need it
raised first).

---

## 7. Canary evidence — 11 breakages, 11 caught

Each applied to the tree, the suite run, the result recorded, the tree reverted.

| # | Breakage | Guard that fired |
| --- | --- | --- |
| 1 | a new route mounted on the app | `test_every_http_route_is_enumerated` |
| 2 | a new tenant-free SQL statement in `metering.py` | `test_every_sql_statement_in_the_source_is_enumerated` — **was green**, fixed |
| 3 | `_SELECT` grows `and account_id = %s` | `test_no_query_filters_or_writes_by_a_tenant` |
| 4 | `Vault.get` takes `account_id` | `test_no_function_takes_a_tenant_parameter` |
| 5 | migration 00001 gains `account_id` | `test_no_migration_carries_a_tenant_column` |
| 6 | a row deleted from `ENTRY_POINTS` | the count ratchet **and** the operation counts |
| 7 | a negative test renamed | `test_every_entry_point_names_a_test_that_actually_exists` |
| 8 | `require_scope`'s message names the account | `test_a_forbidden_body_does_not_vary_by_account` |
| 9 | `/readyz` grows an `account_id` field | `test_readyz_names_no_account` |
| 10 | a missing path mapped to `forbidden` | `test_a_path_that_does_not_exist_is_404_for_every_account` |
| 11 | a stray `tests/tenant_canary.py` | `test_no_test_file_is_left_uncollected` |

**On breakage 8, a note on method.** The first attempt replaced an assertion with a no-op
(`assert stripped[0] is not None`) and the suite stayed green — as a no-op must, and as a
useless canary. The honest version changes `muse.auth.require_scope` to name the account
in its message, and *that* goes red. A canary that only weakens the assertion proves the
assertion is not being read, which is not the same claim.

**Guard direction is asserted, not assumed.** `test_every_http_route_is_enumerated` uses
set equality, so a stale row fails as loudly as a missing one — a guard that only catches
additions is a guard that tolerates deletions, which is how a table drifts into claiming
routes that were removed two packets ago.

---

## 8. Compliance

- **No sleeps, no raised retries, no loosened assertions.** Every clock in play is
  injected; the tests assert on responses and statement counts, never on elapsed time.
  The canary script above is a separate shell script in `/tmp`, not committed.
- **Nothing logged.** No token, key or JWT is printed by any test, and no prompt or
  completion content is asserted on. `Vault.put("openai", "sk-platform-key")` values are
  placeholder literals checked for *absence* in a response body, never printed — and the
  credential read path is `Secret`, so a leak would have to defeat `Secret.__repr__` too.
- **No socket.** ASGI transport, an in-memory `Database`, an in-memory transport for the
  publisher.
- **`pytest.mark.anyio` + `pytest.mark.unit`** on the module, `--strict-markers` clean.
- **100% branch coverage held** across all 20 source modules; `--cov-fail-under=100`
  unchanged. No `# pragma: no cover` added.
- **Suite count: 904 → 930** self-contained; **933** with the core tier.

## 9. Files

| File | Change |
| --- | --- |
| `tests/test_tenant_scoping.py` | **new**, 26 tests: 12 negative + 14 guards/canaries |
| `gate.yml` | `core-parity` floor 907 → 933, with the ratchet note |
| `CHANGELOG.md` | entry at the top of `[Unreleased]` |
| `AGENTS.md` | rule 24 for tenant scoping |
| `src/`, `migrations/` | **unchanged** |

**Not pushed.** Merge to `master` is the manager's.