# REPORT-muse-tenancy-01 — muse declares the honest zero

**Packet:** W36 — muse: declare the honest zero
**Branch:** `worker/muse-w36-tenancy`
**Date:** 2026-10-07
**Verdict:** `tenancy.yml` written — the honest zero, `accountScoped: false`,
with every key-carrying site vouched for in `scope.nonRowSites`. The fleet
checker exits **0** with the 2 expected warnings. The gate is green.

**History.** Attempt 1 shipped the honest zero without `nonRowSites` and the
then-current checker failed it (`tenancy.honest-zero`); attempt 1's report
argued the failure was a false red and recommended a core fix. The premise
then changed: **core W41 (`core@4c95ba`) landed** — the checker no longer
walks `__pycache__` bytecode and gained the field this packet needs,
`scope.nonRowSites`. This attempt vouches for all 15 sites the scanner
reads, each with the real reason it is not a row scope, and the checker is
green. The recommended core fix is the fix that landed.

---

## 1. The measurement: muse holds no account-scoped rows

The claim `accountScoped: false` rests on a measurement, not an assumption.
Four commands establish it.

### 1.1 `account_id` in the source tree

```
$ grep -rn "account_id" src/
src/muse/auth.py:6:- `account_id` — present, and **required**: muse bills per tenant, so a token with no
src/muse/auth.py:74:#: core's required claims (`openapi-conventions.md:134`), and `account_id` alongside
src/muse/auth.py:78:REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "jti", "account_id")
src/muse/auth.py:118:    account_id: str
src/muse/auth.py:160:        account_id = self._account_of(claims)
src/muse/auth.py:163:            account_id=account_id,
src/muse/auth.py:227:        # `account_id` is deliberately absent from this loop. `_account_of` owns its
src/muse/auth.py:232:            if name != "account_id" and name not in claims:
src/muse/auth.py:289:        core requires `account_id` for authenticated service traffic and every query is
src/muse/auth.py:295:        A non-string `account_id` is refused rather than ignored. `gateway` ignores it,
src/muse/auth.py:300:        value = claims.get("account_id")
src/muse/auth.py:303:                "token carries no account_id; muse meters per tenant, so a request whose "
src/muse/metering.py:21:  Since muse-06 the verified token carries an `account_id`, so the account this
src/muse/metering.py:31:  the event alone. The fix is a core change — D9, plus `account_id` in the payload
src/muse/schemas/telemetry/traces.schema.json:203:        "account_id": { "type": "string", "maxLength": 64, "description": "The account, same reasoning as tenant_id." }
src/muse/errors.py:269:    """A verified token carries no usable `account_id`. 401.
src/muse/errors.py:271:    core requires `account_id` on authenticated service traffic, and muse bills and
src/muse/errortype.py:233:    # a cafaye-owned rule (core's `account_id` requirement) refused the operation.
```

18 lines. **Not one is a SQL statement.** They are:

* **Claim verification** (`auth.py`): `REQUIRED_CLAIMS`, the `Principal`
  dataclass field, `_account_of`, the claim-presence loop. The account is read
  out of a *verified* token and carried on the caller; it is never a
  predicate on a row.
* **Docstrings** describing that verification (`auth.py`, `errors.py`,
  `metering.py`).
* **core's own telemetry schema**, vendored byte-identically
  (`traces.schema.json`), where `account_id` is a span-attribute *description*
  — and rule 16 forbids putting it on a span anyway.

Three of the 18 (`auth.py:74`, `auth.py:227`, `errortype.py:233`) are `#`
comments, which the scanner strips before matching — which is why the
checker's site list is 15, not 18.

### 1.2 `account_id` in the migrations

```
$ grep -rn "account_id" migrations/
(exit 1 — no matches)
```

Zero. Neither `outbox_events` nor `vault_secrets` has an `account_id` column.

### 1.3 Row-level-security DDL in the migrations

```
$ grep -rni "create policy\|row level security\|force row" migrations/
(exit 1 — no matches)
```

Zero. No policy, no `enable`, no `force`. So `rls.databaseEnforced: false`
with an empty `tables` is the truth, and it is checked: a policy appearing in
`migrations/` is `tenancy.rls-undeclared`, a failure.

### 1.4 The SQL that does exist carries no account

The only SQL string literals in `src/` are the vault and outbox statements —
`insert into outbox_events`, `insert into vault_secrets … on conflict`,
`select … from vault_secrets where provider = %s`, `delete from vault_secrets
where provider = %s`, `select … from outbox_events … for update skip locked`,
`update outbox_events set … where id = %s`. Every one is keyed by `provider`
or the envelope `id`. None carries `account_id`, `tenant`, or any account
predicate.

### 1.5 The negative is asserted by the suite, not assumed

`tests/test_tenant_scoping.py` holds the zero on four axes, each with a test
named after the failure it prevents:

| Axis | Test |
| --- | --- |
| No query filters or writes by a tenant | `test_no_query_filters_or_writes_by_a_tenant` |
| No migration carries a tenant column | `test_no_migration_carries_a_tenant_column` |
| No function takes a tenant parameter | `test_no_function_takes_a_tenant_parameter` |
| The 14-entry-point enumeration, 4 with a tenant axis | `test_only_four_entry_points_carry_a_tenant_axis` |
| The metered payload names no account (the D9 gap, asserted) | `test_a_metered_event_names_no_account` |

**The measurement is complete: no account-scoped row, statement, migration, or
parameter exists in this tree.** The honest zero is true.

---

## 2. Why `account_id` is a token claim and not a row scope

`account_id` appears in muse in exactly two roles, and neither is a row
discriminator:

1. **Authorization** (`src/muse/auth.py`). `require_bearer` verifies the token
   against identity's JWKS and *requires* an `account_id` claim: a token with
   no usable account is refused (`MissingAccount`, 401) rather than defaulted
   to `sub`, because muse bills per tenant and an unattributable spend is a
   request nobody can charge for. The claim is checked for presence, type and
   emptiness in `_account_of`, and an edited claim fails the signature.
2. **Metering** (`src/muse/metering.py`). The verified account travels on the
   metered event's payload — the `muse.tokens.consumed` envelope. The D9 gap
   is asserted, not papered over: `Meter` knows the caller's `account_id` and
   cannot write it, so the payload is exactly core's five fields with
   `subject: platform`.

In both roles the account is a property of the **caller**, established once at
the edge and carried on the request context. No table muse reads or writes is
scoped by it: `vault_secrets` is keyed by `provider` (one platform credential
per vendor, by design) and `outbox_events` has no tenant column at all, because
core's payload schema still pins `subject: platform` until D9. That is the
whole of rule 24, and it is why the declaration is the honest zero rather than
a boundary the service does not have.

---

## 3. The checker's output

```
$ /Users/kaka/Code/any/wt/core/harness/bin/tenancy-check .
WARN tenancy.enumeration-partial: this checker reads SQL statements and account-key predicates, and it could not classify 15 account-scoped site(s): src/muse/auth.py:118 at no statement within 4 lines; src/muse/auth.py:160 at no statement within 4 lines; src/muse/auth.py:163 at no statement within 4 lines; src/muse/auth.py:232 at no statement within 4 lines; src/muse/auth.py:289 at no statement within 4 lines; src/muse/auth.py:295 at no statement within 4 lines; src/muse/auth.py:300 at no statement within 4 lines; src/muse/auth.py:303 at no statement within 4 lines; src/muse/auth.py:6 at no statement within 4 lines; src/muse/auth.py:78 at no statement within 4 lines; src/muse/errors.py:269 at no statement within 4 lines; src/muse/errors.py:271 at no statement within 4 lines; src/muse/metering.py:21 at no statement within 4 lines; src/muse/metering.py:31 at no statement within 4 lines; src/muse/schemas/telemetry/traces.schema.json:203 at a file type this checker does not read. This declaration is NOT proven closed, and this is a warning rather than a failure because no language agnostic text scanner can settle it — read the named files and confirm each one is declared
    fix: read the named files by hand and confirm each one is declared; the checker's scanner reads SQL and account-key predicates only
WARN tenancy.scope-key-unused: no account-scoped statement this scanner can read carries 'account_id'. That is not proof the service has no scoping — it is usually a key spelled differently from the column it is really scoping by, and on a service written in a language this scanner cannot classify it is what an honest 'I cannot see this' looks like
    fix: check scope.key against the column or parameter this service really scopes by; a key nothing matches is either spelled differently or lives in a language this scanner cannot classify

OK /Users/kaka/Code/any/wt/muse-w36-tenancy: 0 failure(s), 2 warning(s) — warnings do not move the exit code
EXIT: 0
```

### 3.1 The two warnings — the expected verdict, named and not fixed

**`tenancy.enumeration-partial`** names all 15 `account_id` lines the scanner
saw and could not tie to a SQL statement. This is the expected verdict for a
Python service and it is correct: the scanner reads SQL and account-key
predicates, and a Python token claim is neither. The packet says to name it,
not fix it. It is named here, in the `tenancy.yml` header, and in
`scope.nonRowSites`. The 15 lines are enumerated in §1.1 and every one is a
token-claim mention (claim verification, docstrings, or core's vendored schema
description) — none is an account-scoped statement, which is what the
scanner's own message says ("could not classify … no statement within 4
lines").

**`tenancy.scope-key-unused`** says no account-scoped *statement* the scanner
can read carries `account_id`. That is exactly true and exactly the honest
"I found nothing" the field exists to produce. It is the difference between an
honest zero and an unexamined one.

### 3.2 The failure that was, and how it closed

Attempt 1's checker run reported `FAIL tenancy.honest-zero` over the same 15
sites: the pre-W41 checker counted *every* key-carrying line in the declared
sources as evidence against a zero, with no way to vouch for a site that was
not a row scope. For a SQL service that is right; for a Python service whose
token claim is named `account_id` it made the honest zero undeclarable.
Attempt 1's report recommended the core fix: let a declaration name the
key-carrying sites that are not row scopes.

**Core W41 (`core@4c95ba`) is that fix.** The checker gained
`scope.nonRowSites` — a list of `{file, line, reason}` vouching for each
site — and `check_honest_zero` now fails only for scanned sites the
declaration does *not* name. The field is not an escape hatch: it excuses
only sites the scanner could not attribute to a statement, and a key the
scanner read in a `select` is a row scope by definition no matter what the
list says. This declaration vouches for all 15 sites, each with the reason
open at that line (claim verification, docstring, or core's vendored schema
description), and the check passes.

---

## 4. The gate

```
$ bin/prime
==> syncing (locked)
Resolved 88 packages in 25ms
Checked 86 packages in 27ms
==> lint
All checks passed!
72 files already formatted
==> tests
============================= test session starts ==============================
platform darwin -- Python 3.14.7, pytest-9.1.1, pluggy-1.6.0
rootdir: /Users/kaka/Code/any/wt/muse-w36-tenancy
configfile: pyproject.toml
testpaths: tests
plugins: cov-7.1.0, anyio-4.15.1
collected 1037 items
...
=============================== tests coverage ================================
TOTAL                                1851      0    382      0   100%
Required test coverage of 100% reached. Total coverage: 100.00%
======================= 1033 passed, 4 skipped in 26.82s =======================
```

Green: ruff clean, 1033 passed / 4 skipped (the four core-parity tests, which
skip without `MUSE_CORE_SCHEMAS` pointing at a core checkout), 100% branch
coverage.

`bin/prime` is `uv sync --locked && ruff && pytest` with a 100% branch-coverage
floor. It does not run `tenancy-check` (that is core's harness, run outside the
gate). No existing test was weakened or skipped; no test was added — the
change is the declaration file and this report only.

---

## 5. What was shipped

| File | Change |
| --- | --- |
| `tenancy.yml` | New. The honest zero: `accountScoped: false`, `scope.key: account_id`, `scope.sources: [src]`, `scope.nonRowSites` vouching for all 15 key-carrying sites with per-site reasons, empty `entryPoints`, `rls.databaseEnforced: false`, `rls.sources: [migrations]`, empty `tables`. Header names the 15 scanner sites and why none is a row scope. |
| `REPORT-muse-tenancy-01.md` | New. This report. |

No source, test, migration, or gate file was changed. The declaration is the
measured truth, vouched for site by site; the checker's verdict is exit 0
with the two expected warnings.

## 6. Follow-ups (not this packet)

1. **core:** the `check_honest_zero` fix this report recommended in attempt 1
   **landed** as core W41 (`core@4c95ba`) — `scope.nonRowSites` plus the
   `__pycache__` walk removal. Nothing further.
2. **muse:** add `tenancy-check` to the gate (`gate.yml` + `bin/prime`), per
   `docs/tenancy.md` §"Adopting it in a service" step 5. Until then it is a
   second thing to remember, which is how it would be forgotten.
3. **muse:** optionally guard `tenancy.yml`'s shape with a test in the repo's
   rule-23 pattern (every out-of-suite file is guarded). The underlying
   measurement is already guarded by `tests/test_tenant_scoping.py`'s negative
   tests, so this would be a shape guard only.
