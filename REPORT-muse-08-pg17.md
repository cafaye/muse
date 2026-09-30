# REPORT-muse-08 — muse is on the fleet's Postgres 17

## The finding first

**muse was the only service in the fleet not on Postgres 17, and the compose file
carrying that pin had never successfully run.**

Three defects, two of which I did not go looking for. The first two are in the
compose file; the third (`PGDATA`) is what decides who the downgrade
instructions below are actually for.

1. **The pin.** `docker-compose.yml` said `postgres:18-alpine`. Everything else in
   the fleet was on 17. For a one-deploy-many-services platform that is not just an
   extra image in a list — it is a second upgrade path to test and a muse `pg_dump`
   that restores into no other service's database.

2. **The file did not parse.** `MUSE_VAULT_KEY: ${MUSE_VAULT_KEY:?set
   MUSE_VAULT_KEY, or run: uv run python -m muse.vault}` was an unquoted YAML
   scalar, and a plain scalar containing `: ` is a nested mapping. So:

   ```
   $ docker compose config --quiet
   yaml: line 65, column 67: mapping values are not allowed in this context
   $ echo $?
   1
   ```

   `docker compose up` never got as far as a container. **The stack had never
   booted.** Nothing in the repository had executed the file: no test read it, CI
   never invoked compose, and rule 3 means the suite opens no socket. So the
   "18" survived, and so did a file that could not start anything.

The second defect is why the first mattered, and it is the reason this packet had
to boot the stack rather than just edit a tag. **A version pin nobody has executed
is a comment** — and there was no way to execute this one.

I found (2) only because the brief told me to bring the stack up. Had I "fixed"
the pin and stopped, I would have shipped a downgrade that was still unbootable,
and reported it as verified.

### 3. And a third, which is why "downgrade" understates it

While writing the downgrade instructions I stopped assuming what a developer's 18
volume would contain, and measured it. `PGDATA` differs between the two images:

```
$ docker run --rm --entrypoint sh postgres:17-alpine -c 'echo "$PGDATA"'
/var/lib/postgresql/data
$ docker run --rm --entrypoint sh postgres:18-alpine -c 'echo "$PGDATA"'
/var/lib/postgresql/18/docker
```

muse's compose mounted `muse-db:/var/lib/postgresql/data` — the **pre-18** path. The
18 images moved to a major-version-specific subdirectory so `pg_upgrade --link`
works across a mount boundary, and they **refuse to start** when they find a
populated `/var/lib/postgresql/data`. So the honest reproduction of the pre-18
stack is:

```
$ docker run -d --name probe18 -e POSTGRES_PASSWORD=x \
    -v probe18v:/var/lib/postgresql/data postgres:18-alpine
$ docker ps -a --filter name=probe18 --format '{{.Status}}'
Exited (1) 4 seconds ago
$ docker run --rm -v probe18v:/v alpine:3 sh -c 'find /v -type f | wc -l'
0
$ docker logs probe18 | head -2
Error: in 18+, these Docker images are configured to store database data in a
       format which is compatible with "pg_ctlcluster" …
```

**On 18, with this compose file, the database never started and the named volume
held 0 files.** So there is a third reason the pin was never noticed, independent
of the YAML: even with the parse error fixed, `docker compose up -d db` would have
exited 1 on 18.

This is the useful half of the finding, because it changes who the downgrade
instructions are for. **Most developers have no 18 data to carry across** — the
answer for them is `docker compose down -v` and re-migrate, full stop. The
`pg_dump`/`pg_restore` path below is for the smaller group who got a working 18
database by correcting the mount themselves. The compose header and the CHANGELOG
both say this explicitly, because an instruction that sends everyone down a
`pg_dump` path with nothing to dump is worse than no instruction.

I also captured the canonical refusal, for anyone who does have a real 18 cluster,
by building one on 18's own supported mount point and starting 17 against it:

```
FATAL:  database files are incompatible with server
DETAIL:  The data directory was initialized by PostgreSQL version 18, which is
         not compatible with this version 17.11.
```

It **refuses**; it does not attempt a partial start or corrupt anything. (17 may
refuse slightly earlier on a parameter only 18 writes — `autovacuum_worker_slots`
is new in 18 — but it refuses either way.)

The mount path is now asserted in `tests/test_compose.py`, so a future pin bump
that changes `PGDATA` without changing this path is a red test rather than an
empty volume nobody notices.

---

## Verification: the pin, actually booted

Not "the file says 17". Booted, migrated, and driven.

```
$ docker compose up -d db
$ docker compose exec -T db psql -U muse -d muse -tAc "select version();"
PostgreSQL 17.11 on aarch64-unknown-linux-musl, compiled by gcc (Alpine 15.2.0) 15.2.0, 64-bit
$ docker compose exec -T db psql -U muse -d muse -tAc "show server_version_num;"
170011
$ docker inspect --format 'image={{.Config.Image}}' muse-db
image=postgres:17-alpine
```

17.11, the alpine/musl build, the exact tag committed.

**Migrations from scratch**, against a database confirmed empty first
(`select count(*) from information_schema.tables where table_schema='public'` → `0`),
applied exactly as the compose header documents:

```
$ docker compose exec -T db psql -v ON_ERROR_STOP=1 -U muse -d muse < migrations/00001_outbox_events.sql
CREATE TABLE
CREATE INDEX
$ docker compose exec -T db psql -v ON_ERROR_STOP=1 -U muse -d muse < migrations/00002_vault_secrets.sql
CREATE TABLE
```

Clean. And the resulting `outbox_events` matches core's documented column list
exactly — `uuid`/`text`/`text`/`text`/`timestamptz`/`jsonb`/`timestamptz`/
`timestamptz`/`int`, the primary key on `id`, the partial index on
`(created_at, id) where published_at is null`, and all six CHECK constraints.

I also confirmed the constraints **fire**, because a declared constraint is not an
enforced one:

```
$ … -tAc "insert into outbox_events (id, event_type, …) values (gen_random_uuid(), 'NOT A VALID TYPE', …)"
ERROR:  new row for relation "outbox_events" violates check constraint "outbox_events_type_format"
$ … -tAc "insert into vault_secrets (provider, ciphertext) values ('Bad-Name', '\x00');"
ERROR:  new row for relation "vault_secrets" violates check constraint "vault_secrets_provider_format"
```

**muse's own code against that live 17 server** — `PsycopgDatabase`, `Vault` and
`Meter`, the production classes, not helpers written for the test:

- `Vault`: seal → store → read back round-trips; the plaintext is **absent** from
  the stored ciphertext; ciphertext carries nonce‖ct‖tag; `on conflict do update`
  rotates in place and keeps exactly one row; `has`/`delete` behave.
- `Meter`: the outbox insert lands in one transaction, `data` comes back as `jsonb`
  (a dict, not a string) with core's five payload fields, `subject` is core's
  reserved literal `platform`.
- `/readyz`'s `select 1 as ok` probe returns `{"ok": 1}`.

That is where it stopped, and the reason is the next section.

---

## Finding 4 (not mine, and the important one): the outbox publisher has never published a row

**`OutboxPublisher.publish_batch()` returns 0 against a real database, forever, on
any Postgres version.** Same for `Vault.providers()`, which always returns `()`.

`muse.outbox._claim`:

```python
result = await tx.fetchone(_CLAIM, (self._batch_size,))
rows = result.get("rows", ()) if result else ()
```

`muse.vault.Vault.providers` does the same. But `fetchone` returns **a row**, and a
row has no `rows` key. Reproduced in isolation, running the real SQL through the
real adapter:

```
outbox._claim does:  result = await tx.fetchone(_CLAIM, ...)
                     rows   = result.get('rows', ()) if result else ()
  fetchone returned keys : ['attempts', 'data', 'event_type', 'id', 'source', 'subject', 'time']
  'rows' in result       : False
  so muse iterates over  : 0 rows   <-- there IS 1 unpublished row

vault.providers does:   rows = await self._db.fetchone(_PROVIDERS)
                       return tuple(e['provider'] for e in rows.get('rows', ()))
  fetchone returned keys : ['provider']
  'rows' in result       : False
  so muse returns        : ()   <-- 'openai' IS stored
```

### Why the suite is green over it

The root cause is the `Database` protocol, which declares
`fetchone(...) -> Mapping | None` — *"return its first row as a mapping"*. **That
signature cannot express "give me a batch of rows."** The claim query is
`… limit %s for update skip locked` with a batch size of 100; there is no way to
ask this protocol for 100 rows. So both call sites reached for a shape that only one
implementation produces, and that implementation is
`tests/support/fake_database.py`:

```python
def _read_outbox(self, sql, params):
    if "where published_at is null" in sql:
        return {"rows": [ … ]}
```

**The fake was written to agree with the code instead of with the driver.** That
inverts the one job a test double has that a real database does not, and it is why
904 tests, 100% branch coverage, and a 907-test cross-repo run all pass over an
outbox that publishes nothing. No coverage threshold detects it, because the code
*is* executed — it just does the wrong thing against the only implementation that
exists in production.

This is the strongest argument in the packet for the brief's own instruction: a pin
that was never booted is a comment. The same is true of a test double that was
never compared against the thing it stands in for.

### Why I did not fix it here

Not avoidance — scope, and one genuine judgment call I want visible.

- **It is version-independent.** Identical on 17 and on 18. This packet is about
  the version; fixing an unrelated billing-path defect inside it would put a
  large, unreviewed change on top of a small, reviewable one.
- **It is not a one-liner, and the choice is a fleet question.** The fix is to give
  `Database` a way to return many rows (a `fetchall`, or a distinct return shape for
  a batch claim), then change `PsycopgDatabase`, `_PoolTransaction`, the fake, and
  both call sites. AGENTS.md rule 1 is emphatic about blast radius on this seam, and
  **core owns the outbox contract** — the brief forbids editing core, and whether
  every fleet service adopts one `Database` shape is a core decision, not a muse
  one.
- **The outbox is the billing path.** `billing` aggregates usage from these rows.
  A hurried change to a query's return shape is worse than a reported defect.

So it is reported, loudly, with a reproduction. It needs a packet.

---

## Finding 5: no Postgres-18-only feature is in use

Checked every statement muse issues, so the downgrade costs no capability:

| Feature | Introduced | Used by muse |
|---|---|---|
| `on conflict do update` | 9.5 | `vault.py` `_UPSERT` |
| `for update skip locked` | 9.5 | `outbox.py` `_CLAIM` |
| `jsonb` | 9.4 | `outbox_events.data` |
| `uuid` | 8.3 | `outbox_events.id` |
| `timestamptz`, `bytea`, partial index, `CHECK` with `~`/`char_length` | ancient | both migrations |

Nothing from 18: no `MERGE`, no `uuidv7()`, no `OLD/NEW` in `RETURNING`, no virtual
generated columns. **The downgrade is blocked on nothing.**

---

## What a developer with an 18 volume must do

**Read the third finding first, because it decides which of these you need.** With
this compose file, an 18 database never started and its volume held 0 files — so if
your only ever attempt was `docker compose up -d db` here, you have nothing to
carry across and **option 1 is the whole answer**.

For anyone who *does* have a real 18 cluster (they corrected the mount to
`/var/lib/postgresql` themselves, or ran their own container): Postgres major
versions have incompatible on-disk formats, and 17 **refuses** such a directory
rather than corrupting it —

```
FATAL:  database files are incompatible with server
DETAIL:  The data directory was initialized by PostgreSQL version 18, which is
         not compatible with this version 17.11.
```

`docker compose down` without `-v` does not help either: the volume is named, so it
survives the container. That is why this is called out at all.

**1. Delete the volume and re-migrate** — correct for a development stack, and the
right answer for anyone whose 18 database never started. Loses every stored provider
credential; re-enter real keys rather than assuming the vault still holds them.

```sh
docker compose down -v
docker compose up -d db
docker compose exec -T db psql -U muse -d muse < migrations/00001_outbox_events.sql
docker compose exec -T db psql -U muse -d muse < migrations/00002_vault_secrets.sql
```

**2. Carry the rows across** if you need them — `pg_dump` on 18, `pg_restore` on 17.
`pg_dump` is **logical** by default, which is exactly why it can cross the version
boundary that a physical copy of the data directory cannot. Restore into a 17
database that already has the migrations applied. (Note: there is deliberately no
`docker compose up -d db` line before the dump — on 18 that command exits 1, so if
you have a working 18 database it is already running from however you started it.)

```sh
docker compose exec -T db pg_dump -U muse -d muse > muse-18.dump
docker compose down -v && docker compose up -d     # now on 17
# apply 00001 and 00002, then
docker compose exec -T db pg_restore -U muse -d muse --clean --if-exists < muse-18.dump
```

Both options **lose stored provider credentials** unless you dumped first. In the
CHANGELOG entry, and in the `docker-compose.yml` header — the header deliberately,
because that is where a developer meets the problem *before* the failure rather than
after it.

---

## Changes, and the judgment in each

| File | What | Why |
|---|---|---|
| `docker-compose.yml` | `18-alpine` → `17-alpine` | The packet. Exact majority tag. |
| `docker-compose.yml` | Quoted `MUSE_VAULT_KEY` | Blocker: the file could not parse, so the pin could not be booted or verified. |
| `docker-compose.yml` | Downgrade instructions in the header | Required, and the header is where they are read. |
| `tests/test_compose.py` | New, 9 tests, hermetic | Makes the pin a checked contract instead of a comment. |
| `gate.yml` | `core-parity` floor 898 → 907 | My own change would have disarmed the ratchet. Below. |
| `AGENTS.md` | Rule 23, layout line, counts | The lesson, where the next reader will hit it. |
| `CHANGELOG.md` | muse-08 entry | The downgrade, prominently, plus every finding. |
| `tests/gate_self_test.sh` | Stub summary lines 898→907, 895+3skipped→904+3skipped | The self-test feeds the declaration its own counts; they move with the floor. |

**The `gate.yml` ratchet deserves its own note, because it is a trap I walked into
by doing something good.** Adding 9 tests takes the suite from 895 to 904 and the
core-tier run from 898 to 907. The `core-parity` floor's documented property is
that it is *exact*: "dropping one of the three drift guards takes it under." At 898,
a run with one of those guards deleted reports 906 — which still clears 898. **My
addition would have quietly disarmed the ratchet**, in a commit whose only effect
was to make the suite bigger. Raised to 907, and `AGENTS.md` now says that adding
tests means raising it in the same commit. Verified against synthetic summary
lines, all three directions:

```
906 passed, 0 skipped   ->  core-parity matched 906, floor 907 -> gate.floor (FAIL)   ← ratchet bites
907 passed, 0 skipped   ->  core-parity matched 907, floor 907 -> ok                  ← real run
904 passed, 3 skipped   ->  core-parity NO MATCH            -> proof-missing (FAIL)   ← tier skipped
```

### The guards were each broken on purpose

Ten breakages, applied by hand, to confirm `tests/test_compose.py` can actually go
red — including the real defect:

```
unquote MUSE_VAULT_KEY (the actual bug)  -> 6 failed
back to 18-alpine                       -> 1 failed  (the fleet-standard test)
postgres:latest                          -> 2 failed
${POSTGRES_TAG:-17-alpine}               -> 2 failed
delete the downgrade note                -> 1 failed  (pg_dump/pg_restore/down -v)
typo a migration filename                -> 1 failed
MUSE_VAULT_KEY: dev-key-not-a-real-key   -> 1 failed
commit an sk-proj-… key                  -> 1 failed  (after a fix — see below)
commit a JWT                             -> 1 failed
```

**One of them caught the guard, not the bug.** My first credential regex was
`\bsk-[A-Za-z0-9]{16,}`, and I broke the file with `sk-proj-AAAABBBB…` — and it
went **green**. Real provider keys are hyphenated after the prefix
(`sk-proj-`, `sk-ant-`), so the regex stopped at the hyphen. Widened to
`[A-Za-z0-9_-]` and re-broken, it goes red. The guard was green over exactly the
thing it existed to catch, which is the argument for breaking guards on purpose
being a rule rather than a habit.

---

## Test results, pass and skip reported separately

They are different claims, so they are not one number.

| Run | Result | Skips |
|---|---|---|
| `bin/prime` (self-contained tier) | **904 passed** | **3** |
| `MUSE_CORE_SCHEMAS=../core/schemas bin/prime` | **907 passed** | **0** |

- **The 3 skips are pre-existing and gated on `MUSE_CORE_SCHEMAS`**:
  `test_contracts.py::test_patterns_are_byte_identical_to_core`, the
  telemetry-vocabulary guard in `test_error_vocabulary.py`, and the manifest guard
  in `test_openapi.py`. They are the cross-repo drift guards; they are not related
  to this packet and they skip loudly rather than passing quietly.
- Coverage **100%**, branch on. Ruff clean, format clean.
- `../core/harness/bin/gate-check .` and `--prove .` both pass (2 expected
  `gate.requirement-unproven` warnings, documented as correct).
- **No sleeps, no raised retries, no loosened assertions, and no test added that
  skips.**

### A note on what "DB-tier tests" meant here

The brief asked for muse's DB-tier tests. **muse has no live-database tier** — by
design, rule 3: the suite drives an in-memory `Database` and opens no socket, which
is why it is fast, hermetic and parallel-safe. So I ran the suite both ways (above)
*and* did the live proof out of band, driving the production classes against the
live 17. I did not add a socket test to `tests/`: that would trade a permanent
repository property for a one-time proof. The permanent guard I added
(`test_compose.py`) is hermetic, and the live proof is in this report and the
CHANGELOG with the commands to reproduce it.

---

## Notes for the manager

- **Not pushed**, per the packet. Committed on `worker/muse-08-pg17`.
- **No other service's compose file was touched.** The `17` → `17-alpine`
  consolidation for courier and billing is image-tag-only and rides with those
  repos' own packets. Verified: `courier` and `billing` still say `postgres:17`.
- **Two compose files, one line each, and a broken YAML scalar.** All in muse.
- **Finding 2 needs its own packet** and probably a core ruling on the `Database`
  seam, since the "no way to ask for a batch" gap will exist in any service that
  implements the outbox the same way.
- The port in the committed file is still `5433`. The proof ran on `5544` through an
  **uncommitted** override, because another worker on this machine already held
  5433 (identity's) and the packet says one suite at a time. Nothing in the
  repository was changed for the port.
- Left running for inspection: the `muse-db` container on 17.11, migrated, with the
  two tables present and empty.
