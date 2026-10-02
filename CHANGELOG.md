# Changelog

All notable changes to muse. Format follows Keep a Changelog; versioning is
conventional-compat (0.x, so anything may change while pre-1.0).

## [Unreleased]

### `RouteRequest.temperature` is a decimal string, and a JSON number is a 422

**BREAKING, on one field, with no deprecation window.** `temperature` was
`type: number` and is now `type: string` carrying a plain decimal in [0, 2].
`caf contract breaking --tiers all` reports it at all three tiers and all three
are honest:

```
property-same-type [SOURCE+JSON+WIRE] #/components/schemas/RouteRequest/temperature:
  Property "temperature" on schema "RouteRequest" changed type from "number" to "string".
```

**What a caller has to do.** Send the value as a string.

```diff
- {"model": "fast", "messages": [...], "temperature": 0.3}
+ {"model": "fast", "messages": [...], "temperature": "0.3"}
```

A JSON number is refused with a **422 naming `temperature`**, not coerced.

**Why a float could not stay.** Kubernetes' API conventions refuse them at
`api-conventions.md:603`: they "cannot be reliably round-tripped and have
varying precision across languages and architectures". The concrete cost is on
the write path and it is not theoretical — `cafaye-ts` had already generated
`temperature?: number` from the old document:

```ts
// a TypeScript caller computing a temperature
temperature: 0.1 + 0.2     // -> wire: 0.30000000000000004
```

JavaScript stringifies every number through `float64`, so all seventeen of
those digits reach LiteLLM as a sampling parameter. `0.1 + 0.2` is
`0.30000000000000004` in Python for the same reason, and a Ruby caller doing
the same arithmetic has it too. A decimal string has no `float64` anywhere on
the path: the caller writes the digits they mean.

**Why a string and not an integer with a scale.** An integer (milli-degrees,
`700` meaning 0.7) was the other option and it is not what the provider takes:
LiteLLM declares `temperature: float | None`, so a scale would have to be divided
back out on the way to the provider anyway, and every caller would have to know
the scale. A decimal string is Kubernetes' own advice for a
provider-passthrough parameter where exact bytes matter, and the conversion to a
float happens exactly once — in `RouteRequestBody.sampling_temperature()`, at the
same seam `cost_per_1k_tokens` converts money on, and for the same reason (rule 9).

**Why no deprecation window.** Accepting a number for one version and coercing it
would keep the `float64` round-trip alive on purpose, and would make the code
accept a shape the document does not describe — the "OpenAPI says one thing and
the service does another" defect, in a contract system. The break lands whole and
a caller gets a 422 naming the field, which is a one-line fix.

**Accepted forms.** `0`, `0.0`, `0.7`, `1`, `1.0`, `1.25`, `2`, `2.0`, `0.250`.
Refused with a 422: `2.5`, `3`, `-1`, `1e-3`, `NaN`, `""`, `.5`, `+1`, `01`,
`1.`, `1.5.5`, `0x1`, `" 1"`, `"1 "`. The range is enforced in
`TEMPERATURE_PATTERN` rather than in prose, because a pattern is what a generated
client can actually check, and `tests/test_openapi.py` asserts the document and
that constant are the same string. Absent still means the provider's own default,
and absent is still never sent to the provider.

**`gate.yml`'s `core-parity` floor is raised 933 → 964 in this commit**, measured
rather than counted: 937 passed before this change, 964 after.

### The local stack: kit's shared cluster, and a `bin/dev` that does not exist yet

**`docker-compose.yml` is now an OVERRIDE passed second beside kit's fetched
stack**, in the shape `billing`, `courier` and `identity` already have. This
changes the local loop, so it is here rather than only in the diff.

**What a developer has to do differently.** `docker compose up --build` no
longer brings up a database — this file no longer carries one. The stack is two
files:

```sh
KIT_COMPOSE_DIR=<dir holding $(cat kit.ref)>/templates/compose
docker compose --project-directory . \
  -f "$KIT_COMPOSE_DIR/docker-compose.yml" -f ./docker-compose.yml up -d --wait
```

`KIT_COMPOSE_DIR` is not optional and the failure is silent: kit's compose file
resolves its own initdb bind mount through `${KIT_COMPOSE_DIR:-.}`, compose
resolves a relative path against the project directory, and Docker *creates* a
missing bind source as an empty directory. Drop the variable and the cluster
comes up **healthy having run no init script at all** — no role, no database,
no `REVOKE CONNECT`. `docker-compose.yml`'s header has the measurement.

**The port moved.** The old `db:` service published `5433:5432`. Host-side
`psql` now goes to kit's `${KIT_POSTGRES_PORT:-15500}`, and it needs the
cluster password (`cafaye` by default) rather than none. A `ports:` under the
`postgres` override would have bought BOTH ports, because compose appends a
second file's `ports:` list rather than substituting it.

**`muse-db` is now nobody's volume.** It is still on your machine and nothing
mounts or deletes it. `docker compose down -v` no longer reaches it either —
that removes the volumes of the compose *project*, and the project is now kit's.
`docker-compose.yml` names both recoveries: `pg_dump`/`pg_restore` across, or
`docker volume rm muse-db`.

**Migrations are still a manual step**, unchanged in kind: kit creates the
database and the role, and creates no tables.

### Licence: muse is MIT, and it was AGPL-3.0-only

The one licence change in this packet, and the only deliberate departure from
the fleet decision, so the reasoning is here rather than only in the diff.

**`pyproject.toml` declared `AGPL-3.0-only` and `README.md` said so too.** The
declaration has been reviewed and is not deliberate: it is replaced with MIT, and
`openapi/v1.yaml`'s `info.license` block — a third statement of the same fact
that no reader would have known to look for — moves with it.

The reason is the registry model. muse is consumed as one node in a dependency
graph looked up through pantry, and copyleft attaches an obligation to every
downstream consumer of every node that reaches it. That defeats the thing the
registry exists to do: a consumer adds a platform dependency **without their own
licensing situation changing**. MIT is what makes that true; AGPL-3.0-only is
what would have made it false, and would have made adding muse a decision about
obligations rather than about dependencies.

`license = { file = "LICENSE" }` rather than a bare `license = "MIT"`, matching
`cafaye-py`, so the metadata points at the grant instead of restating it. The
`LICENSE` file at the repository root is canonical MIT text — verified verbatim
against SPDX `license-list-data`, not paraphrased — carrying
`Copyright (c) 2026 cafaye`, the same line the three repositories that already
shipped a licence use.

### muse-09 — tenant isolation: 12 account-scoped entry points, negatively tested

Packet `muse-09`. D18 measured cross-tenant negative tests at **identity 7, courier
19, muse 0**. This is muse's contribution: the enumeration, one negative test per entry
point, and the guards that keep the enumeration true.

#### The count, and why it is small

**12 account-scoped entry points. 7 read, 2 list, 2 update, 1 delete.** Of those twelve,
**three carry a tenant axis at all**:

| Entry point | Layer | Op | Tenant axis |
| --- | --- | --- | --- |
| `api.require_bearer` | auth | read | **account** |
| `auth.require_scope` | auth | read | platform |
| `POST /v1/route` | http | read | **account** |
| `GET /healthz` | http | read | platform |
| `GET /readyz` | http | read | platform |
| `metering.Meter.record` | service | update | **account** |
| `vault.Vault.get` | service | read | platform |
| `vault.Vault.has` | service | read | platform |
| `vault.Vault.providers` | service | list | platform |
| `vault.Vault.put` | service | update | platform |
| `vault.Vault.delete` | service | delete | platform |
| `outbox.OutboxPublisher.claim` | service | list | none |

The ratio is the measurement, and it is deliberately not inflated. muse stores **no
tenant-owned rows**: `vault_secrets` is keyed by `provider` (one platform credential
per vendor, by design) and `outbox_events` has no tenant column, because core's payload
schema still pins `subject: platform` until D9. So the tenant surface is the auth gate,
the one route, and the one write that meters the route — and the other nine entries are
load-bearing *because* they cannot see a tenant. A suite that claimed a surface muse
does not have would make a reader believe the gap was closed.

#### Absence, never 403

A 403 on the tenant axis is an **enumeration oracle**: it says "this exists and is not
yours", which is strictly more information than a 404 for a resource that never existed.

- **No account-scoped entry point answers 403.** muse's one 403 is on the *capability*
  axis (`InsufficientScope`), and `test_a_forbidden_body_does_not_vary_by_account`
  asserts three accounts receive a byte-identical body — so it cannot probe tenancy.
- **A path that does not exist answers 404 for every account**, asserted over five paths
  and three accounts.
- **`MissingAccount` is a 401, not a 403.** A 403 there would tell a caller holding a
  valid signature that the token is real and only its tenant is wrong.

#### The enumeration is checked, not asserted

Three guards make it load-bearing, because an enumeration nobody checks against the code
is a comment that ages badly:

- every route the running app serves (read off its generated OpenAPI document) must be
  in the table, **and the table must not claim a route the app does not serve**;
- every SQL statement in `src/muse/` must be attributed to an enumerated entry point,
  **count included** — nine of them;
- no query may mention a tenant column, no migration may add one, and no function may
  take an account argument.

A packet that grows a tenant surface has to move `TENANT_SCOPED_COUNT` on purpose, next
to the negative test for whatever it added.

#### Two guards that were wrong before they were right

Both were found by breaking them on purpose, not by reading them.

1. **The SQL guard could pass on a file holding two queries while naming one.** It
   checked that every enumerated query was present and every file with SQL was
   accounted for, but never that a file held no *more* than the enumeration named. A
   canary appending a tenant-free `select count(*)` to `metering.py` went green. The
   per-file count is now asserted, and the failure names the unnamed statements.

2. **24 of the 26 tests were never collected.** The file was first written as
   `tests/tenant_scoping.py`, mirroring `darkroom-09`'s Rust layout. `pyproject.toml`
   sets `testpaths` but not `python_files`, so pytest's default `test_*.py` applies and
   the whole file was skipped — the gate read `904 passed, 3 skipped` **before and after**
   the commit that added it. Renamed to `test_tenant_scoping.py`, and
   `test_no_test_file_is_left_uncollected` now walks `tests/` and fails on any module
   that defines a test without matching the collection pattern, because a guard that can
   pass because *it* is not running is the worst version of this failure.

#### The gap that is named and not closed

`Meter` knows the caller's `account_id` and cannot write it: core owns
`schemas/events/muse/tokens/consumed.schema.json`, which closes `data` at five fields
and documents `subject: platform` until D9. `test_a_metered_event_names_no_account`
asserts the absence is real *and* named — the payload is exactly core's five fields and
the subject is core's reserved literal. A test implying this was closed would be the
defect, not the coverage. Adding the tenant column to `outbox_events`, to a vault query,
or to a function signature is now a red suite rather than a review comment.

#### Canary evidence

10 breakages applied and reverted, 10 caught:

| Breakage | Guard that fired |
| --- | --- |
| a new route mounted | `test_every_http_route_is_enumerated` |
| a new tenant-free SQL statement | `test_every_sql_statement_in_the_source_is_enumerated` (was green) |
| a query grows `where account_id` | `test_no_query_filters_or_writes_by_a_tenant` |
| a function takes `account_id` | `test_no_function_takes_a_tenant_parameter` |
| a migration adds `account_id` | `test_no_migration_carries_a_tenant_column` |
| a row deleted from the table | the count ratchet + operation counts |
| a negative test renamed | `test_every_entry_point_names_a_test_that_actually_exists` |
| the 403 body leaks the tenant | `test_a_forbidden_body_does_not_vary_by_account` |
| `/readyz` grows an `account_id` field | `test_readyz_names_no_account` |
| a missing path answers 403 | `test_a_path_that_does_not_exist_is_404_for_every_account` |

Plus an 11th: a stray `tests/tenant_canary.py` is caught by
`test_no_test_file_is_left_uncollected`.

No 403 was found on the tenant axis, so nothing needed changing in `src/`. The
production tree is unchanged by this packet.

#### Numbers

- **26 new tests** in `tests/test_tenant_scoping.py`; suite **904 → 930** self-contained.
- `gate.yml` `core-parity` floor **907 → 933**, raised in the same commit as the tests,
  per the ratchet clause — and this time the clause caught a real defect rather than
  guarding a hypothetical one, since a floor left at 907 would have cleared a commit
  that added 26 tests and ran none of them.
- Gate: **933 passed** with `MUSE_CORE_SCHEMAS=../core/schemas`, **930 passed / 3
  skipped** without it. `gate-check --prove` is OK with the core tier and still fails
  `gate.proof-missing` without it. 100% branch coverage held.
- 3 skips are the pre-existing core-parity tests: `test_contracts.py::test_patterns_are_byte_identical_to_core`,
  the telemetry-vocabulary drift guard in `test_error_vocabulary.py`, and the
  manifest-drift guard in `test_openapi.py`.

### muse-08 — muse is on the fleet's Postgres 17, and the stack was never booted

Packet `muse-08`: the local stack moves from `postgres:18-alpine` to
`postgres:17-alpine` — the fleet majority, verified, not assumed.

> #### ⚠️ ACTION REQUIRED IF YOU HAVE A LOCAL `muse-db` VOLUME
>
> **First, the measured good news: with this compose file, a stack on 18 never
> started at all — so most people have no 18 data to carry across.** The 18 image
> moved its data directory: `PGDATA` is `/var/lib/postgresql/18/docker` in
> `postgres:18` and `/var/lib/postgresql/data` in `postgres:17`. The
> `muse-db:/var/lib/postgresql/data` mount this file carried is the *pre-18* path,
> and the 18 image **refuses to start** when it finds a populated
> `/var/lib/postgresql/data`. Verified: the container exits 1, and the named volume
> holds **0 files**. So if your only attempt was `docker compose up -d db` on this
> repository, deleting the volume and re-migrating is the whole of what you need:
>
> ```sh
> docker compose down -v
> docker compose up -d db
> docker compose exec -T db psql -U muse -d muse < migrations/00001_outbox_events.sql
> docker compose exec -T db psql -U muse -d muse < migrations/00002_vault_secrets.sql
> ```
>
> **If you did get a working 18 database** — by correcting the mount to
> `/var/lib/postgresql` yourself, or from a `docker run` of your own — then you have
> a real cluster, and Postgres major versions have incompatible on-disk formats. 17
> **refuses** such a directory rather than corrupting it. Captured verbatim:
>
> ```
> FATAL:  database files are incompatible with server
> DETAIL:  The data directory was initialized by PostgreSQL version 18, which is
>          not compatible with this version 17.11.
> ```
>
> Carry the rows across with a **logical** dump, which is exactly the thing that can
> cross the version boundary where a physical copy of the data directory cannot:
>
> ```sh
> docker compose exec -T db pg_dump -U muse -d muse > muse-18.dump
> docker compose down -v && docker compose up -d     # now on 17
> # apply 00001 and 00002, then
> docker compose exec -T db pg_restore -U muse -d muse --clean --if-exists < muse-18.dump
> ```
>
> There is no third option: `docker compose down` without `-v` leaves the named
> volume in place, and 17 still will not start against it. **Both recoveries lose
> stored provider credentials** unless you dumped first — re-enter real keys
> afterwards rather than assuming the vault still holds them.
>
> The same instructions are in the `docker-compose.yml` header, where a developer
> meets them *before* the failure rather than after it.

#### Changed

- **`postgres:18-alpine` → `postgres:17-alpine`.** The exact tag five of the six
  services already use (darkroom, identity, and parlor/e2e pin `17-alpine`; courier
  and billing pin `17`), so a cross-service `pg_dump`/`pg_restore` is a routine
  rather than a project. On a one-deploy-many-services platform the outlier is not
  just an extra image: it is a second upgrade path, and a muse dump that restores
  into no other service's database.
- **The compose file is now read by the suite** (`tests/test_compose.py`, 9 tests,
  hermetic — no socket, so rule 3 holds).

#### Fixed

- **`docker-compose.yml` did not parse, so this stack had never booted.**
  `MUSE_VAULT_KEY: ${MUSE_VAULT_KEY:?… or run: uv run python -m muse.vault}` was an
  unquoted YAML scalar, and the `: ` inside `run: uv` makes a plain scalar a nested
  mapping. `docker compose up` failed with `mapping values are not allowed in this
  context` before it looked at a container. The line is now quoted; the refusal on a
  missing key — the point of the line — is unchanged, and still a boot failure.
  **The version pin above could not have been booted, or verified, before this fix**,
  which is the whole reason both defects survived: nothing in this repository had ever
  executed the file.
- **The volume was mounted where 18 does not keep its data.** `PGDATA` is
  `/var/lib/postgresql/data` in `postgres:17` and `/var/lib/postgresql/18/docker` in
  `postgres:18`; the mount below is unchanged and is correct for 17. Worth recording
  because on 18 it was not merely a stale path: the 18 image refuses to start when it
  finds a populated `/var/lib/postgresql/data`, so `docker compose up -d db` exited 1
  and the named volume held **0 files**. The mount is now asserted, so a future pin
  bump that changes `PGDATA` without changing this path is a red test rather than an
  empty volume nobody notices.

#### Added

- **`tests/test_compose.py`** — asserts the file parses, that the database is the
  platform standard's *exact* tag (not `latest`, not an interpolation), that the
  volume mounts at 17's `PGDATA`, that the downgrade note still names both
  recoveries, that the header's migration filenames exist, and that
  `MUSE_VAULT_KEY` is a `${…:?…}` refusal rather than a literal.
  Ten breakages were applied by hand to confirm each assertion can go red; the first
  version of the credential regex passed a committed `sk-proj-…` key because it
  stopped at the hyphen, which is why they were worth applying.
- **`gate.yml`'s `core-parity` floor raised 898 → 907**, and `AGENTS.md` now says
  plainly that adding tests means raising it in the same commit. Left at 898, the
  9 new tests would have disarmed the ratchet: a run with one of the three core drift
  guards deleted (906) would still have cleared the floor. A commit that only made the
  suite bigger quietly weakened the guard that says the cross-repo tier ran.

#### Findings — reported, not fixed, and deliberately so

Both are **pre-existing and version-independent** — identical on 17 and on 18 —
and neither is fixed here. See `REPORT-muse-08-pg17.md` for the reasoning and for
what a fix would have to decide.

- **`OutboxPublisher` has never published a row against a real database.**
  `muse.outbox._claim` reads its result as `result.get("rows", ())`, and
  `muse.vault.Vault.providers` does the same — but a *row* mapping has no `rows` key.
  The `Database` protocol declares `fetchone(...) -> Mapping | None`, which cannot
  express "give me a batch of rows" at all, so both call sites invented a shape that
  only `tests/support/fake_database.py` produces. Against real postgres, `psycopg`
  returns the first row, `.get("rows", ())` is `()`, and the loop claims nothing and
  exits cleanly forever. `Vault.providers()` likewise always returns `()`.
  **The cause is that the fake was written to agree with the code rather than with
  the driver** — the one job a test double has that a real database does not. The
  suite is green over this: 904 tests, 100% coverage, and an outbox that publishes
  nothing. It surfaced only because `muse-08` booted the stack and ran muse's own
  classes against it, which is the argument for doing so. **This needs its own
  packet**, and probably a core ruling on the `Database` seam, since the "no way to
  ask for a batch" gap will exist in any service that implements the outbox this way.
- **No Postgres-18-only feature is in use.** Every statement is 9.5-era or older —
  `on conflict do update`, `for update skip locked`, `jsonb`, `uuid`, `timestamptz`,
  `bytea`, partial indexes, `CHECK` with `~` and `char_length`. Nothing in the
  downgrade is blocked on a feature muse would have to give up.

#### Verification

- `docker compose up -d db` on the new pin; `select version()` reports
  **`PostgreSQL 17.11 on aarch64-unknown-linux-musl … (Alpine 15.2.0)`**,
  `server_version_num = 170011`.
- Both migrations applied to an **empty** database from scratch, clean. The resulting
  `outbox_events` matches core's documented column list exactly, and the CHECK
  constraints were confirmed to *fire* (a malformed `event_type` and a malformed
  `provider` are both rejected by the server, not merely declared).
- muse's own `Vault`, `Meter` and `PsycopgDatabase` were driven against that live 17
  server: seal/store/rotate/read/delete round-trip, the plaintext confirmed absent
  from the stored ciphertext, the metering insert landing as `jsonb` with core's five
  payload fields, and the `/readyz` `select 1 as ok` probe. (The publisher's claim
  query is where it stopped — see the findings above.)
- **Suite, self-contained tier: 904 passed, 3 skipped** (895 + 9 new). The 3 skips are
  the pre-existing cross-repo guards and are gated on **`MUSE_CORE_SCHEMAS`**;
  coverage 100%, branch coverage on.
- **Suite, cross-repo tier with `MUSE_CORE_SCHEMAS=../core/schemas`: 907 passed, 0
  skipped.** Both counts reported separately because they are different claims, and
  `gate.yml`'s `core-parity` proof is the one that refuses the skipped run.
- `../core/harness/bin/gate-check .` and `--prove` both pass, and
  `bash tests/gate_self_test.sh` is 20/20 (3 controls, 13 breakages, 3 warnings,
  1 hygiene, 0 skipped).
- No sleeps, no raised retries, no loosened assertions, and no test was added that
  skips.

### muse-06 — the bearer token is verified, and a valid signature is not an authorization

Packet `muse-06`: `require_bearer` stops being a presence check. It verifies the
signature against `identity`'s published JWKS, checks the issuer, audience, expiry and
every required claim, enforces the capability the operation needs, and refuses a token
with no `account_id`.

#### Changed

- **The bearer token is verified.** RS256 only, against
  `{issuer}/.well-known/jwks.json`, cached by `kid` with a bounded TTL so `identity` is
  never on the hot path.
  **Breaking for every existing caller**: any non-empty string used to work, and a 401
  now means a real reason — a forged signature, an expired token, a wrong issuer or
  audience, a missing claim, or no `account_id`. Nothing about the request or response
  *shape* changed, which is why this is a minor version bump on `openapi/v1.yaml`: the
  path is the same and the body is the same, but the meaning of a `200` did.
- **A validly signed token carrying no scope is now a 403.** This is the change that
  matters. "The signature is valid" and "the caller may do this" are different
  questions, and an implementation that checks only the first authorises anybody
  holding a stale token.
- **`account_id` is required.** A token with none is refused rather than defaulted to
  `sub`, because muse bills and meters per tenant and the lenient answer makes an
  unattributable spend. **Stricter than `guard`**, which treats a missing
  `account_id` as `undefined` for rate-limit keying; the two are answering different
  questions and the divergence is recorded in `cafaye.yml` for a fleet-wide ruling.
- **An unreachable key set is a 503, never a 401.**
  **muse does not serve unauthenticated traffic when identity is down.** A 401 would
  tell the caller their credential is bad when the problem is that we could not
  *check* it. The request is refused rather than admitted on an unverified credential.
- **Four new error classes**, all `AuthError`s: `Unauthenticated`, `InsufficientScope`,
  `MissingAccount` and `SigningKeysUnavailable`. The first three report as
  `policy_denied`; `SigningKeysUnavailable` reports as `dependency_unavailable`,
  because an outage on the same dashboard as a fraud signal trains everyone to ignore
  that page.
- **`identity` is now a required dependency** in `cafaye.yml`. Not because muse needs
  more of it — because it can no longer serve anything without it, and a soft
  dependency is how `caf dev` starts a service that answers 503 to everything.
- **`joserfc` and `httpx` are now runtime dependencies.** `httpx` was already in the
  runtime closure through `litellm`, so the image gains nothing.

#### Added

- **The JWKS refresh is bounded.** A minimum interval between forced refreshes, a
  timestamped negative cache of unknown `kid`s, and a cold cache that fails closed
  rather than fetching again. Without the bound, "refresh on an unknown `kid`" is an
  amplification primitive: anyone who can send a request can send one with a random
  `kid` and make muse fetch the key set per request, unauthenticated, aimed at our own
  identity service. `tests/test_auth.py` sends five hundred unknown `kid`s and asserts
  the fetch count does not track the request count.
- **Rotation works in both directions**, and is asserted: a token signed by a key
  published *after* this process cached the old set is accepted within the refresh
  interval, and a key withdrawn from the published set stops being accepted once the
  TTL expires. The cache is replaced, never merged — a merged cache is a set in which a
  withdrawn signing key never leaves.
- **`403` as a documented response**, and the `503` gained a second cause.
- `MUSE_IDENTITY_ISSUER`, `MUSE_IDENTITY_AUDIENCE`, `MUSE_JWKS_URL`,
  `MUSE_JWKS_TTL_SECONDS`, `MUSE_JWKS_REFRESH_SECONDS`. These **default** rather than
  being required, the opposite of `MUSE_VAULT_KEY` and deliberately: a wrong issuer or
  audience cannot make a bad token good, so the worst a wrong default can do is refuse
  every token.

#### Open, and implemented safely under

None of these is settled; all three are recorded in `cafaye.yml` for whoever decides.

- **The capability claim's name.** core requires `scopes`; `guard` reads a
  space-separated `scope`; `identity` mints both, byte-identical. muse accepts either
  and **refuses a token carrying both where they disagree** — not merged, not
  preferred, not unioned. A token whose two authorisation claims contradict each other
  is a token muse does not understand, and guessing which one the issuer meant is how an
  escalation about a claim name becomes a cross-tenant read.
- **The required scope's exact string**, `completions:write`. It follows core's
  documented `resource:action` shape and names muse's own surface, and it is one
  constant. This packet adds no scope namespace to core's document.
- **A token with no `account_id`** — refused here, lenient in `guard`. See above.

### muse-05 — error.type is core's vocabulary, and a span cannot claim one thing while recording another

Packet `muse-05`: `error.type` moves from muse's class names to core's fleet
vocabulary, and a span's error class and its status become one thing recorded in
one place.

#### Changed

- **`error.type` is core's vocabulary, not `type(error).__name__`.**
  `ProviderAuthError` now goes on the wire as `provider_auth`, `CircuitOpen` as
  `circuit_open`, and so on through all 27 classes in `muse.errors`. The old
  spelling was a fine identifier and a useless one: every service spells the same
  failure its own way, and PLAN §7b's fleet-wide error view — partition by
  `service.name`, filter on span status `error`, drill down by `error.type` — is a
  query only when every service draws from one list. muse is the only service that
  emitted a class at all, which is the cheapest moment to fix it; after three more
  services start copying the shape it is a migration rather than an edit.
  **Breaking for anything that read the attribute**, which so far is
  `tests/test_trace_propagation.py` (updated to assert `provider_auth`, the
  specific value, in both places that asserted `ProviderAuthError`).
- **The mapping table is exhaustive and the lookup is by exact type.**
  `tests/test_error_vocabulary.py` walks `muse.errors` and fails on any class with
  no entry, and `error_type` does not walk the hierarchy, so a new subclass of
  `ProviderUnavailable` is refused rather than silently reported as
  `dependency_unavailable`. Same discipline as `is_retryable`.
- **`_OTHER` is reachable and is not a default.** It is mapped onto deliberately
  for `ProviderIndeterminate` — the case core kept it for, so instrumentation is
  never forced to invent a class. An unmapped class raises `UnclassifiedError`
  rather than becoming `_OTHER`, because a default is how a vocabulary stops
  being read. `record_error` catches that refusal, records `_OTHER` and logs the
  class name, so telemetry that cannot classify a failure does not turn the
  caller's 503 into a 500.

#### Added

- **`muse/errortype.py`** — the one table from muse's exception classes to the
  thirteen classes, with the reasoning for the four that are a judgement rather
  than a lookup (`ContentPolicyError` → `provider_rejected`,
  `CredentialUnavailable`/`VaultDecryptError` → `internal_error`,
  `AllCandidatesFailed` → `dependency_unavailable`, `ConfigError` →
  `internal_error`) written next to the rows.
- **The vocabulary is loaded from core's schema, not restated.** `error_types()`
  reads the enum out of `traces.schema.json`, vendored byte-identically under
  `src/muse/schemas/telemetry/`. `tests/test_error_vocabulary.py` asserts that copy
  is byte-identical to core's whenever `MUSE_CORE_SCHEMAS` points at a checkout,
  and the suite validates spans muse actually exported against the vendored
  schema — including the three ways the error biconditional can be wrong — on
  the default gate, with no core checkout needed.
- **`telemetry.record_error(span, error)`** — the only path from an exception to a
  span failure, setting `error.type` and the span's status together.

#### Fixed

- **A span recorded a class while claiming it had succeeded.** muse set
  `error.type` and left the span status `unset`, which is one half of the
  biconditional core-05 encoded as an `allOf`: a class without a failed status, so
  the fleet view — which filters on status `error` and only then drills down —
  never saw a failure muse had already classified. Both existing call sites (the
  provider failure and the breaker refusal) now set both halves, and
  `tests/test_trace_propagation.py` asserts the biconditional over *every* span
  one exporter holds, after asserting the classes are present so an empty export
  cannot make the absence vacuous.
- `tests/support/tracing.py` reports span status and kind as well as attributes.
  A payload without them cannot answer the question the biconditional asks.

Packet `muse-03b`: the gate was red on a clean checkout. A regression found at merge,
fixed at the declarations rather than at the test. While proving the fix, a second
defect of the same class turned up in the gate itself.

#### Fixed

- **`bin/prime` could not pass on a fresh clone.** `muse-03` put
  `opentelemetry-exporter-otlp` in the optional `otel` extra, but
  `test_a_configured_endpoint_gets_a_batch_processor` exercises
  `build_provider(endpoint=...)`, which imports that exporter — and `bin/prime` runs
  `uv sync` with no extras. The worktree that developed `muse-03` was green
  because its venv already carried the exporter from an `uv sync --extra otel`; every
  clean clone got
  `ConfigError: MUSE_OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not
  installed`. The worst kind of green: true where it was written, false everywhere
  else. **A test that needs a package makes it a test dependency**, so the dev group
  now asks for the extra (`muse[otel]`) rather than repeating the exporter's floor in
  a second place. The extra stays an extra for the image, where pulling grpcio and
  protobuf into every install is still the wrong default.
- **`bin/prime` asserted nothing about the lockfile.** Its own comment claimed
  `uv sync --frozen` "refuses to re-resolve a stale lock", and the flag on line 12 was
  `--frozen`. It does not: `--frozen` means *do not update the lock*, which is exactly
  what lets a lock that disagrees with `pyproject.toml` install silently; the assertion
  is `--locked`. Demonstrated against a stale lock: `uv sync --frozen` exited 0 with 77
  packages and no exporter, `uv run pytest` exited 0 with 760 passed, and `uv run`
  quietly **rewrote `uv.lock` on the way there**. The gate passed over a lockfile nobody
  had committed — the same "green in one worktree, false elsewhere" shape as the
  exporter defect, and the one a clean-checkout proof cannot catch, because a clean
  checkout has a correct lock. Every `uv sync`/`uv run` in `bin/prime` now carries
  `--locked`, including the `uv run` lines: `uv run` re-resolves by default, so a
  guarded sync followed by a bare `uv run` reopened the hole. The Dockerfile's syncs
  carried the same false comment and are now `--locked` too.
- **`pydantic` and `starlette` are now declared.** `api.py` and `main.py` import both
  at module scope, and both were reaching the venv only as `fastapi`'s transitive
  dependencies — the same accident one fastapi repin away from a broken build.

#### Added

- `tests/test_dependencies.py` — a recurrence guard for both halves, asserted from
  the declarations rather than from the venv. Every module `src/` and `tests/` import
  must be provided by the closure of `[project.dependencies]` plus the dependency
  groups as `uv.lock` records it, resolved at **submodule** granularity, because
  `opentelemetry` is declared while `opentelemetry.exporter.otlp` is not. Every
  third-party root must additionally be a *direct* declaration, since transitive
  availability is not a declaration. Ownership is read from the installed RECORD
  files, so the check fails both with the extra installed (the owner is outside the
  closure) and with it absent (the module cannot be found) — which is what makes it
  trustworthy in a dirty venv. A third test names the OTLP exporter specifically, so
  the regression this packet fixes fails with a message that points at itself.
- Four more assertions in that module, each of which closes a way the guard could have
  been decorative:
  - **the lock agrees with `pyproject.toml`** — the fact `--locked` checks, read from
    two files on disk so it also holds in a dirty tree.
  - **`bin/prime` passes `--locked` everywhere** — read from the script, so the gate
    cannot quietly lose the assertion. The whole script is checked, not line 12,
    because the exposure was the *combination* with an unguarded `uv run`.
  - **the walk found something** — every assertion above is a set-difference over a
    helper's result, so an empty result makes all of them pass. Breaking `_imports`,
    `_declared`, `_gate_closure` or `_owners` one at a time left the two general
    guards green every time; only the by-name OTLP test noticed. The floor is now
    asserted before any absence is, which is the canary test's own discipline from
    `test_trace_propagation.py` ("the canary is absent" is also true of an export that
    produced no spans).
  - **the exporter stays out of the image** — `[project.dependencies]` must not name
    the expensive distributions and every Dockerfile `uv sync` must carry `--no-dev`,
    so the fix cannot be "simplified" into shipping grpcio and protobuf everywhere.
- `AGENTS.md` rule 19 and a README section: a green gate is a claim about a clean
  machine, and `rm -rf .venv && bin/prime` is how you check it.

#### Deliberately not done

- `test_a_configured_endpoint_gets_a_batch_processor` is unchanged. It tests real
  behavior — an endpoint configured means a batch processor is wired — so the
  dependency moved instead of the test.
- No `pytest.mark.skipif` on the exporter. Skipping would trade a broken gate for a
  gate that quietly tests less, which is the failure mode this packet exists to end.
- Coverage stays at 100%; the added tests are in `tests/`, so they do not dilute it.

#### Proof

The salvage was unverified, so it was verified before being trusted: reverting
`pyproject.toml` and `uv.lock` to `f411eb5` and deleting `.venv` reproduces the
merge-red gate (4 failed, 756 passed), with all three original dependency tests firing
on it. With the fix applied, `rm -rf .venv && bin/prime` gives **763 passed, 2 skipped,
coverage 100.00%, exit 0**. The salvage's shape was also checked against the objection
that it makes the dev environment carry a production dependency: it does not. The
Dockerfile's command sequence installs 69 distributions with no
`opentelemetry-exporter-otlp`, no grpcio and no protobuf, against the gate's 85.

Packet `muse-03`: OpenTelemetry, trace propagation, and bounded retry budgets. PLAN §7
adopts W3C `traceparent` with traces in the platform collector, and bounded retry
budgets with circuit breakers ("briefs forbid naive retries").

#### Added

- **Dependencies**, each justified in `README.md`:
  - `opentelemetry-api`, `opentelemetry-sdk` — PLAN §7's "adopt from first deploy".
    The SDK's own provider and span types rather than a hand-rolled span, so traces
    are the shape every other OpenTelemetry tool expects.
  - `opentelemetry-exporter-otlp` as the **optional `otel` extra**, imported only
    when an endpoint is configured. As a hard dependency it would pull grpcio and
    protobuf into every install to serve a path most deployments never reach.
- `muse/telemetry.py` — `TraceParent` and `parse_traceparent`, plus
  `ALLOWED_SPAN_ATTRIBUTES` and `record()`: the redaction boundary for telemetry.
- `TraceMiddleware` in `muse/main.py` — continues a well-formed inbound
  `traceparent`, starts a new trace when it is absent or malformed, and echoes both
  `traceparent` and the existing `X-Trace-Id`. **Pure ASGI**, not
  `@app.middleware("http")`: the decorator runs the downstream app in a separate
  task, so the router would have seen no trace and every provider span would have
  been orphaned.
- Outbound propagation: `LiteLLMProvider` reads the ambient OpenTelemetry context
  and sends `traceparent` as `extra_headers`, so the `Provider` protocol does not
  grow an argument every future adapter would have to remember to honour.
- Three spans: `muse.request`, `muse.route` (the routing decision), and
  `muse.provider.call`.
- `muse/breaker.py` — a circuit breaker per provider. Opens after N consecutive
  *transient* failures, admits one half-open probe, closes on success. Consulted
  before the provider is touched, so a held-back provider is never dialled.
- `budget_seconds` and `backoff_jitter` in `config/routes.yaml`; a per-route
  `retry_indeterminate` flag.
- `MUSE_BREAKER_THRESHOLD`, `MUSE_BREAKER_RESET_SECONDS`,
  `MUSE_OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`.
- `traceparent` in `openapi/v1.yaml`, as an optional request parameter and on every
  response, with a test that drives the running app and reads the real headers.
- Tests: `test_telemetry.py`, `test_breaker.py`, `test_retry_budget.py`,
  `test_trace_propagation.py`, `test_resilience_config.py`, plus a
  `tests/support/tracing.py` that swaps the SDK's in-memory exporter in for the OTLP
  one.

#### Changed

- **A read timeout is no longer retried by default.** litellm's `Timeout` with no
  HTTP status behind it now maps to the new `ProviderIndeterminate` rather than a
  retryable `ProviderTimeout`. The request was in flight when the deadline passed, so
  the vendor may have completed and billed it, and without an idempotency key there
  is no way to ask which happened — retrying can charge a customer twice for one
  request. A route opts in with `retry_indeterminate: true`. A **408** stays
  retryable: that is the server saying the request never arrived complete.
  Two adapter tests changed with this and say why.
- **The backoff is jittered.** Without it, every muse that failed at the same instant
  retried at the same instant, so a provider recovering from an outage was hit by a
  synchronised wave. The window is `[ceiling * (1 - jitter), ceiling]`, so the floor
  rises with the exponential (the backoff cannot go backwards) and jitter can never
  push a delay past the cap. `jitter: 0` restores the exact previous schedule.
- The retry stop condition is now muse's own rather than tenacity's, so the clock is
  injectable. A count alone did not bound time: six attempts against a 4s ceiling is
  20s of waiting before the last attempt is sent. The deadline is checked before each
  wait, so muse never begins a sleep that would carry the request past it.

#### Security

- **No span attribute may carry prompt, completion or credential text.** Enforced by
  an allowlist at one choke point (`muse.telemetry.record`), not by discipline at each
  call site: the realistic leak is a well-meaning `muse.prompt` added in six months,
  not an attacker. A `Secret` is refused on its *type*, never compared by value.
  `error.message` is deliberately **not** allowlisted — a vendor's content-policy
  rejection quotes the offending content back, so the class name is recorded and the
  message is not. The canary test drives a completion through the real app with a
  unique string in both prompt and completion and asserts it appears nowhere in the
  rendered span payload; mutation testing confirms it fails when a prompt is recorded.

#### Notes

- **muse has no `Idempotency-Key`.** PLAN §7 assigns one to muse and it is not built,
  which is the direct reason `ProviderIndeterminate` defaults to un-retried. Flagged
  for the manager: it is the one thing standing between muse and safely retrying
  ambiguous timeouts, and it also matters for the caller-visible double-billing risk.
- **`timeout_seconds` in `routes.yaml` is still not enforced.** It is declared and
  documented but nothing applies it, so a single hung provider call is bounded only by
  the caller's own timeout. Pre-existing from muse-02 and left alone deliberately: the
  packet is about retry budgets, and the only way to test a real `asyncio.timeout`
  expiry is a real sleep, which this repo's flake policy forbids. Follow-up packet.
- A held-back provider does not fail the request — the fallback still serves it. A
  breaker that failed the whole request would be strictly worse than none, since one
  vendor being down would take out every route with a healthy fallback.
- Only transient failures count against the breaker. A 400 or a 401 is muse sending
  something the provider is right to refuse, and counting it would let one caller with
  a malformed request take the route down for everybody.
- `error.type` uses its OpenTelemetry dotted spelling (set through `record`'s mapping
  form, since a dotted name is not a legal keyword argument); muse's own attributes
  are underscored. Both conventions are documented on the allowlist.
- 757 tests, 100% branch coverage. Dependency floors satisfy the machine-wide uv
  `exclude-newer = "7 days"` quarantine and so may trail the true latest.

### muse-04 — CI, and the tier that would have skipped

Packet `muse-04`: muse had no CI. It calls kit's reusable workflow, and adds the
two jobs kit cannot own — because the shared workflow alone would have gone green
while skipping the only two tests that catch drift with core.

#### Added

- **`.github/workflows/ci.yml`**, calling
  `cafaye/kit/.github/workflows/ci.reusable.yml@master` for `language: python`
  with `coverage-fail-under: 100`. kit owns the shared half — install, ruff, the
  suite, the coverage gate — and muse holds a `uses:` rather than a copy, so a fix
  to how the fleet builds reaches muse without a per-repo PR.
- **`gate + core parity`**, the job that makes the badge mean something. It checks
  out `cafaye/core` (sparse, `schemas` only, at a pinned SHA), sets
  `MUSE_CORE_SCHEMAS`, and runs `bin/prime` — the same command a developer runs,
  not a CI-only variant. It then asserts `git diff --exit-code -- uv.lock`, fails
  on **any** skipped test, and proves the core-parity guard can go red by mutating
  a throwaway copy of core's `eventType.pattern` and failing if the test still
  passes. A caller cannot inject `env:` into a called reusable workflow, so kit's
  python job reports `763 passed, 2 skipped` and exits 0; this job reports
  `765 passed` and treats any skip as a red build.
- **`pins`**, because the runtime pin has to be written twice (GitHub exposes no
  file context to a reusable workflow call's `with:`). It asserts
  `.python-version`, `mise.toml`'s `[tools]` and the workflow's own `versions:` /
  setup-uv values all say the same thing, so a bump cannot be done halfway.

#### Fixed

- **The README claimed 562 tests.** It is 765, and the two numbers mean different
  things: 765 with a core checkout, `763 passed, 2 skipped` without one. The
  README now says both counts and says plainly that the second proves nothing.

### muse-03b — dependencies

Packet `muse-03b`: the gate was red on a clean checkout. A regression found at merge,
fixed at the declarations rather than at the test. While proving the fix, a second
defect of the same class turned up in the gate itself.

#### Fixed

- **`bin/prime` could not pass on a fresh clone.** `muse-03` put
  `opentelemetry-exporter-otlp` in the optional `otel` extra, but
  `test_a_configured_endpoint_gets_a_batch_processor` exercises
  `build_provider(endpoint=...)`, which imports that exporter — and `bin/prime` runs
  `uv sync` with no extras. The worktree that developed `muse-03` was green
  because its venv already carried the exporter from an `uv sync --extra otel`; every
  clean clone got
  `ConfigError: MUSE_OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not
  installed`. The worst kind of green: true where it was written, false everywhere
  else. **A test that needs a package makes it a test dependency**, so the dev group
  now asks for the extra (`muse[otel]`) rather than repeating the exporter's floor in
  a second place. The extra stays an extra for the image, where pulling grpcio and
  protobuf into every install is still the wrong default.
- **`bin/prime` asserted nothing about the lockfile.** Its own comment claimed
  `uv sync --frozen` "refuses to re-resolve a stale lock", and the flag on line 12 was
  `--frozen`. It does not: `--frozen` means *do not update the lock*, which is exactly
  what lets a lock that disagrees with `pyproject.toml` install silently; the assertion
  is `--locked`. Demonstrated against a stale lock: `uv sync --frozen` exited 0 with 77
  packages and no exporter, `uv run pytest` exited 0 with 760 passed, and `uv run`
  quietly **rewrote `uv.lock` on the way there**. The gate passed over a lockfile nobody
  had committed — the same "green in one worktree, false elsewhere" shape as the
  exporter defect, and the one a clean-checkout proof cannot catch, because a clean
  checkout has a correct lock. Every `uv sync`/`uv run` in `bin/prime` now carries
  `--locked`, including the `uv run` lines: `uv run` re-resolves by default, so a
  guarded sync followed by a bare `uv run` reopened the hole. The Dockerfile's syncs
  carried the same false comment and are now `--locked` too.
- **`pydantic` and `starlette` are now declared.** `api.py` and `main.py` import both
  at module scope, and both were reaching the venv only as `fastapi`'s transitive
  dependencies — the same accident one fastapi repin away from a broken build.

### Added

- `tests/test_dependencies.py` — a recurrence guard for both halves, asserted from
  the declarations rather than from the venv. Every module `src/` and `tests/` import
  must be provided by the closure of `[project.dependencies]` plus the dependency
  groups as `uv.lock` records it, resolved at **submodule** granularity, because
  `opentelemetry` is declared while `opentelemetry.exporter.otlp` is not. Every
  third-party root must additionally be a *direct* declaration, since transitive
  availability is not a declaration. Ownership is read from the installed RECORD
  files, so the check fails both with the extra installed (the owner is outside the
  closure) and with it absent (the module cannot be found) — which is what makes it
  trustworthy in a dirty venv. A third test names the OTLP exporter specifically, so
  the regression this packet fixes fails with a message that points at itself.
- Four more assertions in that module, each of which closes a way the guard could have
  been decorative:
  - **the lock agrees with `pyproject.toml`** — the fact `--locked` checks, read from
    two files on disk so it also holds in a dirty tree.
  - **`bin/prime` passes `--locked` everywhere** — read from the script, so the gate
    cannot quietly lose the assertion. The whole script is checked, not line 12,
    because the exposure was the *combination* with an unguarded `uv run`.
  - **the walk found something** — every assertion above is a set-difference over a
    helper's result, so an empty result makes all of them pass. Breaking `_imports`,
    `_declared`, `_gate_closure` or `_owners` one at a time left the two general
    guards green every time; only the by-name OTLP test noticed. The floor is now
    asserted before any absence is, which is the canary test's own discipline from
    `test_trace_propagation.py` ("the canary is absent" is also true of an export that
    produced no spans).
  - **the exporter stays out of the image** — `[project.dependencies]` must not name
    the expensive distributions and every Dockerfile `uv sync` must carry `--no-dev`,
    so the fix cannot be "simplified" into shipping grpcio and protobuf everywhere.
- `AGENTS.md` rule 19 and a README section: a green gate is a claim about a clean
  machine, and `rm -rf .venv && bin/prime` is how you check it.

### Deliberately not done

- `test_a_configured_endpoint_gets_a_batch_processor` is unchanged. It tests real
  behavior — an endpoint configured means a batch processor is wired — so the
  dependency moved instead of the test.
- No `pytest.mark.skipif` on the exporter. Skipping would trade a broken gate for a
  gate that quietly tests less, which is the failure mode this packet exists to end.
- Coverage stays at 100%; the added tests are in `tests/`, so they do not dilute it.

### Proof

The salvage was unverified, so it was verified before being trusted: reverting
`pyproject.toml` and `uv.lock` to `f411eb5` and deleting `.venv` reproduces the
merge-red gate (4 failed, 756 passed), with all three original dependency tests firing
on it. With the fix applied, `rm -rf .venv && bin/prime` gives **763 passed, 2 skipped,
coverage 100.00%, exit 0**. The salvage's shape was also checked against the objection
that it makes the dev environment carry a production dependency: it does not. The
Dockerfile's command sequence installs 69 distributions with no
`opentelemetry-exporter-otlp`, no grpcio and no protobuf, against the gate's 85.

Packet `muse-03`: OpenTelemetry, trace propagation, and bounded retry budgets. PLAN §7
adopts W3C `traceparent` with traces in the platform collector, and bounded retry
budgets with circuit breakers ("briefs forbid naive retries").

### Added

- **Dependencies**, each justified in `README.md`:
  - `opentelemetry-api`, `opentelemetry-sdk` — PLAN §7's "adopt from first deploy".
    The SDK's own provider and span types rather than a hand-rolled span, so traces
    are the shape every other OpenTelemetry tool expects.
  - `opentelemetry-exporter-otlp` as the **optional `otel` extra**, imported only
    when an endpoint is configured. As a hard dependency it would pull grpcio and
    protobuf into every install to serve a path most deployments never reach.
- `muse/telemetry.py` — `TraceParent` and `parse_traceparent`, plus
  `ALLOWED_SPAN_ATTRIBUTES` and `record()`: the redaction boundary for telemetry.
- `TraceMiddleware` in `muse/main.py` — continues a well-formed inbound
  `traceparent`, starts a new trace when it is absent or malformed, and echoes both
  `traceparent` and the existing `X-Trace-Id`. **Pure ASGI**, not
  `@app.middleware("http")`: the decorator runs the downstream app in a separate
  task, so the router would have seen no trace and every provider span would have
  been orphaned.
- Outbound propagation: `LiteLLMProvider` reads the ambient OpenTelemetry context
  and sends `traceparent` as `extra_headers`, so the `Provider` protocol does not
  grow an argument every future adapter would have to remember to honour.
- Three spans: `muse.request`, `muse.route` (the routing decision), and
  `muse.provider.call`.
- `muse/breaker.py` — a circuit breaker per provider. Opens after N consecutive
  *transient* failures, admits one half-open probe, closes on success. Consulted
  before the provider is touched, so a held-back provider is never dialled.
- `budget_seconds` and `backoff_jitter` in `config/routes.yaml`; a per-route
  `retry_indeterminate` flag.
- `MUSE_BREAKER_THRESHOLD`, `MUSE_BREAKER_RESET_SECONDS`,
  `MUSE_OTEL_EXPORTER_OTLP_ENDPOINT`, `OTEL_SERVICE_NAME`.
- `traceparent` in `openapi/v1.yaml`, as an optional request parameter and on every
  response, with a test that drives the running app and reads the real headers.
- Tests: `test_telemetry.py`, `test_breaker.py`, `test_retry_budget.py`,
  `test_trace_propagation.py`, `test_resilience_config.py`, plus a
  `tests/support/tracing.py` that swaps the SDK's in-memory exporter in for the OTLP
  one.

### Changed

- **A read timeout is no longer retried by default.** litellm's `Timeout` with no
  HTTP status behind it now maps to the new `ProviderIndeterminate` rather than a
  retryable `ProviderTimeout`. The request was in flight when the deadline passed, so
  the vendor may have completed and billed it, and without an idempotency key there
  is no way to ask which happened — retrying can charge a customer twice for one
  request. A route opts in with `retry_indeterminate: true`. A **408** stays
  retryable: that is the server saying the request never arrived complete.
  Two adapter tests changed with this and say why.
- **The backoff is jittered.** Without it, every muse that failed at the same instant
  retried at the same instant, so a provider recovering from an outage was hit by a
  synchronised wave. The window is `[ceiling * (1 - jitter), ceiling]`, so the floor
  rises with the exponential (the backoff cannot go backwards) and jitter can never
  push a delay past the cap. `jitter: 0` restores the exact previous schedule.
- The retry stop condition is now muse's own rather than tenacity's, so the clock is
  injectable. A count alone did not bound time: six attempts against a 4s ceiling is
  20s of waiting before the last attempt is sent. The deadline is checked before each
  wait, so muse never begins a sleep that would carry the request past it.

### Security

- **No span attribute may carry prompt, completion or credential text.** Enforced by
  an allowlist at one choke point (`muse.telemetry.record`), not by discipline at each
  call site: the realistic leak is a well-meaning `muse.prompt` added in six months,
  not an attacker. A `Secret` is refused on its *type*, never compared by value.
  `error.message` is deliberately **not** allowlisted — a vendor's content-policy
  rejection quotes the offending content back, so the class name is recorded and the
  message is not. The canary test drives a completion through the real app with a
  unique string in both prompt and completion and asserts it appears nowhere in the
  rendered span payload; mutation testing confirms it fails when a prompt is recorded.

### Notes

- **muse has no `Idempotency-Key`.** PLAN §7 assigns one to muse and it is not built,
  which is the direct reason `ProviderIndeterminate` defaults to un-retried. Flagged
  for the manager: it is the one thing standing between muse and safely retrying
  ambiguous timeouts, and it also matters for the caller-visible double-billing risk.
- **`timeout_seconds` in `routes.yaml` is still not enforced.** It is declared and
  documented but nothing applies it, so a single hung provider call is bounded only by
  the caller's own timeout. Pre-existing from muse-02 and left alone deliberately: the
  packet is about retry budgets, and the only way to test a real `asyncio.timeout`
  expiry is a real sleep, which this repo's flake policy forbids. Follow-up packet.
- A held-back provider does not fail the request — the fallback still serves it. A
  breaker that failed the whole request would be strictly worse than none, since one
  vendor being down would take out every route with a healthy fallback.
- Only transient failures count against the breaker. A 400 or a 401 is muse sending
  something the provider is right to refuse, and counting it would let one caller with
  a malformed request take the route down for everybody.
- `error.type` uses its OpenTelemetry dotted spelling (set through `record`'s mapping
  form, since a dotted name is not a legal keyword argument); muse's own attributes
  are underscored. Both conventions are documented on the allowlist.
- 757 tests, 100% branch coverage. Dependency floors satisfy the machine-wide uv
  `exclude-newer = "7 days"` quarantine and so may trail the true latest.

## [0.2.0] — 2026-09-30

Packet `muse-02`: providers, router, vault, metering, and the HTTP surface. muse can
now serve a completion and bill for it.

### Added

- **Dependencies**, each justified in `README.md`:
  - `litellm` — the routing and metering engine, embedded as a library through its
    Python API (PLAN §2b). No code copied.
  - `cryptography` — AES-256-GCM for the credentials vault.
  - `tenacity` — per-candidate retry with exponential backoff, with an injectable sleep.
  - `psycopg` + `psycopg-pool` — postgres, and a pool rather than a connection so a
    transaction is an isolated handle rather than shared state.
  - `pyyaml` — `config/routes.yaml`.
  - `jsonschema` (dev only) — validates `cafaye.yml` against core's schema when a core
    checkout is available.
- `muse/providers/` — the `Provider` protocol (`complete`, `health`,
  `cost_per_1k_tokens`), a registry that refuses to register a name twice, the LiteLLM
  adapter for openai and anthropic, and `FakeProvider` / `ScriptedProvider`.
  - Prices are integer micro-dollars per 1k tokens. The single place a float touches
    money is the adapter's conversion from litellm's per-token table, which rounds *up*:
    a sub-micro-dollar price must not become zero, which would make a month of usage
    invisible until the invoice did not add up.
  - The adapter scrubs the credential out of provider error text and health details,
    and maps litellm's exception taxonomy onto muse's — with
    `ContextWindowExceededError` checked *before* `BadRequestError`, which it
    subclasses upstream. Reordering them does not crash; it files a too-long prompt as
    a malformed request.
- `muse/routes.py` — `config/routes.yaml` and its loader. A candidate's model is
  resolved at `Route` construction; an unknown key is a load error rather than a
  warning, because a misspelled `max_attemtps` that is silently dropped leaves a route
  retrying once while its author believes it retries three times.
- `muse/router.py` — request to ordered candidates, first success wins, failures
  advance. Real tenacity backoff with an injected sleep, so the suite asserts the
  durations the policy asked for. The price is resolved *before* dispatch, so a model
  muse cannot price is never called.
- `muse/vault.py` — AES-256-GCM under `MUSE_VAULT_KEY`, which must be present, base64
  and exactly 32 bytes or the process does not start. The provider name is the
  additional authenticated data, so a ciphertext copied between rows fails to decrypt
  rather than decrypting under the wrong vendor. Plaintext comes back as a `Secret`.
- `muse/providers/credentials.py` — `VaultCredentials` reads through the vault per
  request, so a rotated key takes effect on the next request rather than at the next
  deploy.
- `muse/metering.py` — one `muse.tokens.consumed` event per served completion, in a
  transaction, with exactly five payload fields. A metering failure is not swallowed:
  the completion was paid for, and returning it without its record would lose the spend
  silently.
- `muse/outbox.py` — the publisher skeleton. core's claim query with
  `for update skip locked`, ack-then-mark, exponential backoff with a cap, and a
  transport interface with no implementation: this packet has no NATS client, and
  `InMemoryTransport` is named for what it is.
- `muse/db.py` — the `Database` protocol and the psycopg adapter.
- `muse/redaction.py` — `Secret`, whose `repr`/`str`/`__format__` are the redaction
  marker, and `redact()` for provider-supplied text.
- `muse/contracts.py` — core's `eventType`, `serviceName` and `subject` patterns, with
  a parity test against a core checkout.
- `migrations/00001_outbox_events.sql` — core's outbox table, column for column.
- `migrations/00002_vault_secrets.sql` — one encrypted key per provider, no plaintext
  column.
- `config/routes.yaml` — the committed routing table: `fast` and `smart`, each with a
  fallback.
- `POST /v1/route` — the completion endpoint. Every non-2xx is
  `application/problem+json` with a cafaye `code`, and `X-Trace-Id` in the header and
  the body.
- `openapi/v1.yaml` — the committed HTTP contract, checked against the running app in
  both directions so it cannot drift from the code.
- `cafaye.yml` — migrated to core's manifest schema, with
  `exposes.events: [muse.tokens.consumed]`.

### Changed

- `/readyz` fills the `db` slot the v0 scaffold reserved: `ok` when the store answers,
  `error` when it does not, and the status is `degraded` rather than `unavailable`
  because a route whose provider needs no database read can still be served. The key
  and the shape are unchanged, which is what "later packets fill the value in" meant.
- 404 and 405 are now `application/problem+json` rather than Starlette's
  `{"detail": ...}`. core says every non-2xx is problem+json, and a client that can
  parse our errors should be able to parse the two it hits while integrating.
- `muse.app` is now the composition root: `create_app(settings, container)` builds a
  `Container` during the lifespan, and accepts one so tests do not need a database.

### Notes

- `muse.tokens.consumed`'s action `consumed` is **not** in core's v0 action
  vocabulary. The packet brief named it, so it is implemented as specified and flagged
  for the manager in `cafaye.yml`: either the vocabulary gains `consumed` or this
  becomes `muse.usage.recorded`. Both are one-line changes; the vocabulary is core's.
- The payload schema for `muse.tokens.consumed` belongs in core, not here — core owns
  every payload schema because a publisher that owns its own is a contract nobody else
  can rely on. It is not written in this packet.
- The outbox publisher has no transport. This packet has no NATS client, and
  `InMemoryTransport` is what the tests and the local stack use.
- Dependency floors are the newest releases satisfying the machine-wide uv
  `exclude-newer = "7 days"` quarantine, so they may trail the true latest. See the
  Dependencies section of `AGENTS.md`.
- 592 tests, 100% branch coverage. The one excluded function is
  `PsycopgDatabase.open()`, which dials.

## [0.1.0] — 2026-09-30

Initial scaffold (packet `muse-01`). No LLM logic yet — see `AGENTS.md`.

### Added

- `pyproject.toml` managed by uv; runtime deps `fastapi`, `uvicorn`; dev group
  `pytest`, `httpx`, `anyio`, `pytest-cov`, `ruff`. `uv.lock` committed.
- `src/muse/main.py` — `create_app()` application factory (no module-level
  singleton; `uvicorn --factory muse.main:create_app`).
- `GET /healthz` → `{"status":"ok"}`. Liveness checks nothing on purpose.
- `GET /readyz` → `{"status":"ok","checks":{"db":"skipped"}}`. The `checks` map
  and its `db` slot are reserved for the Phase 4 credentials vault, so later
  packets fill in a value instead of reshaping the response.
- `tests/` — 23 tests written before the implementation, driven in-process over
  `httpx.ASGITransport` (no socket, no network). Covers probe status codes and
  exact JSON shapes, app-factory isolation, and the 404/405 surface.
- 100% branch-coverage gate (`--cov-fail-under=100`) plus strict markers and
  strict config.
- `Dockerfile` — multi-stage, `python:3.14-slim`, uv 0.12.20, non-root runtime
  user, `HEALTHCHECK`, frozen lockfile install.
- `docker-compose.yml`, `bin/prime` (the gate), `mise.toml` (python + uv pins),
  `.python-version`, `.gitignore`, `AGENTS.md`, `README.md`, and a `cafaye.yml`
  manifest draft.

### Notes

- Dependency floors are the newest releases that satisfy the machine-wide
  uv `exclude-newer = "7 days"` supply-chain quarantine, so fastapi 0.141.1 and
  uvicorn 0.53.0 may trail the true latest. See the Dependencies section of
  `AGENTS.md`.
- `cafaye.yml` is a draft: `core` owns the schema and does not exist yet.

[Unreleased]: https://github.com/cafaye/muse/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/cafaye/muse/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/cafaye/muse/releases/tag/v0.1.0
