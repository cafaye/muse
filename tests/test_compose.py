"""The compose stack is a contract, and this checks it against the code.

A version pin nobody has executed is a comment. That is not a figure of speech in
this repository: `docker-compose.yml` carried `postgres:18-alpine` while the rest of
the fleet sat on 17, and it carried a `MUSE_VAULT_KEY` line that did not parse, so
`docker compose up` had never successfully run in this repository at all. Both
defects survived because nothing ever *ran* the file — a reader accepted the
indentation, CI never invoked compose, and the suite has no socket (AGENTS.md rule
3), so a file that exists only to start a database was never started by anything
that would notice.

muse-08 booted the stack and left this module behind. `m39` took the same
principle one layer further: muse no longer *has* a database container, so the
assertions below are no longer "the tag is right" but "there is no tag here at
all, and the reasons are on the file". A stale assertion is worse than a missing
one — it goes green over a shape the repository no longer has.

So the file is read here, parsed, and asserted. The properties, in the order they
matter:

1. **It parses.** Cheap, and the guard for the defect above: an unquoted YAML scalar
   containing `: ` is a nested mapping, and the `${VAR:?...}` refusal message is
   exactly such a scalar. The failure is `mapping values are not allowed in this
   context` at a line nobody was looking at.
2. **There is no second postgres.** muse joined kit's shared cluster, so this file
   must carry an `environment:` override and nothing else on the cluster service.
   Every way of *not* doing that is asserted separately, because each one was a
   real defect rather than a hypothetical:
   - a `db`/`postgres` service carrying an `image:` is a COPY of the platform;
   - `POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` on the cluster is a
     superuser grant, or an initdb that will not finish, or one service deciding
     the credential for all nine;
   - a `ports:` on the cluster APPENDS to kit's rather than replacing it, so it
     buys you both ports — which is what the old `5433:5432` would have done.
3. **muse's own database is declared, exactly once.** `KIT_POSTGRES_DATABASES`
   with a `${…:-muse}` default: the mechanism, and overridable, because one
   cluster serves the whole fleet and that list does not fit in one repository.
4. **The trap is still in the file.** `KIT_COMPOSE_DIR` is not optional, and
   omitting it is SILENT — Docker creates a missing bind source as an empty
   directory, so the cluster comes up healthy having run no init script at all.
   Same reasoning as (3) in the previous shape: a comment that a rename silently
   deletes is how the next person loses an afternoon, so the assertions are on
   the operations that actually recover (`KIT_COMPOSE_DIR`, the volume), not on
   prose.

Two of these are the kind a reviewer would call "testing a comment", and they are
here for the reason the module docstring gives: a guard that cannot fail is not a
guard. Each was confirmed to go red by breaking the file by hand.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"
KIT_REF = Path(__file__).resolve().parent.parent / "kit.ref"

#: The three environment keys a service may NOT set on the shared cluster, and the
#: one-line reason each is refused. Duplicated from kit's `tests/fleet_check.py`
#: (`_SHARED_CLUSTER_ENV`) rather than imported: kit is a different repository,
#: absent from a checkout and from CI, and a test that could not run in CI is not
#: a test. The measurements behind all three are in the compose file's own header
#: and in kit's source, which is where a reader goes to be convinced rather than
#: to be told.
SHARED_CLUSTER_ENV = ("POSTGRES_USER", "POSTGRES_DB", "POSTGRES_PASSWORD")


def compose_text() -> str:
    assert COMPOSE.is_file(), "docker-compose.yml is missing; the local stack is a contract"
    return COMPOSE.read_text(encoding="utf-8")


def compose_document() -> dict:
    """The parsed compose file.

    A test that returns a dict on parse failure would make every assertion below
    vacuously true — the guard that can pass by finding nothing (AGENTS.md,
    "Dependencies"). So the parse error is allowed to propagate, and the test that
    wants a specific message reads the text.
    """
    return yaml.safe_load(compose_text())


def test_the_compose_file_parses() -> None:
    """The file is valid YAML.

    This is the guard for a real defect: `MUSE_VAULT_KEY: ${MUSE_VAULT_KEY:?set
    MUSE_VAULT_KEY, or run: uv run python -m muse.vault}` was unquoted, and the `: `
    inside `run: uv` made the scalar a nested mapping. `docker compose up` failed with
    "mapping values are not allowed in this context" before it looked at a container,
    so this stack had never booted. A guard that cannot fail is not a guard, so the
    assertion is that a parse *succeedes* — and the test below proves it can go red.
    """
    document = compose_document()
    assert isinstance(document, dict), "the compose file must parse to a mapping"
    assert "services" in document, "a compose file with no services is not a stack"


def test_the_stack_carries_the_service_and_nothing_else() -> None:
    """`muse` and `postgres`, and no `db`.

    The old assertion was `"db" in services`. It is now the opposite statement, and
    it is stronger: a stack that grew a `db` again is not a stack that merely
    drifted, it is the fleet's original defect — one Postgres per service, each with
    its own major to upgrade and its own port to collide on. `billing`, `courier` and
    `identity` name their override `postgres` because that is the name kit ships;
    muse used `db`, which is why a check that looked for the NAME alone would have
    reported this repository clean while the copy stood right there.
    """
    services = compose_document()["services"]
    assert "muse" in services, "the compose stack must carry the service"
    assert "postgres" in services, (
        "muse joins kit's shared cluster, so the stack must carry the cluster's "
        "override entry — an override of kit's postgres, not a second container"
    )
    assert "db" not in services, (
        "a `db:` service is a COPY of the platform's postgres. Delete it and let "
        "kit's come from the fetched stack: two postgres majors to upgrade and a "
        "port collision for the second developer on the machine."
    )
    assert len(services) == 2, (
        f"this file is an OVERRIDE beside the fetched stack, so it owns two "
        f"services; it declares {sorted(services)}. Anything else here is shared "
        f"infrastructure this repository has taken ownership of."
    )


def test_the_cluster_override_brings_no_image() -> None:
    """No `image:`, no `build:` — which is what makes the entry an override.

    `check_stale_copy` in kit's gate matches a service's image against the images kit
    ships, on the bare repository name, because six services name their database `db`
    and a name-based check finds nothing in five repositories out of six. muse was
    one of those: it ran `postgres:17-alpine`, the image kit's cluster is BUILT FROM,
    and only the image check found it.

    So this is asserted here in the repository that carried the defect, where the
    measurement is one `grep` away. The absence is what a service gets a database
    from: `KIT_POSTGRES_DATABASES` and nothing else.
    """
    cluster = compose_document()["services"]["postgres"]
    assert not cluster.get("image"), (
        "the postgres entry is an OVERRIDE of kit's container. An `image:` here "
        "makes it a second copy, and a copy is two majors to upgrade."
    )
    assert not cluster.get("build"), "a build: here forks the platform's cluster image"
    # Over the PARSED values rather than the file text, because the header quotes
    # `postgres:17-alpine` and `kit-postgres:${KIT_POSTGRES_TAG:-17}` at length —
    # a prose assertion here would be green over a copy and red over a comment.
    offenders = {
        name: str(cfg.get("image"))
        for name, cfg in (compose_document()["services"] or {}).items()
        if isinstance(cfg, dict) and "postgres" in str(cfg.get("image") or "")
    }
    assert not offenders, (
        f"{offenders} — no service in this file may run a postgres image. The major "
        "is kit's to choose (`kit-postgres:${KIT_POSTGRES_TAG:-17}`), and a service "
        "that pins one carries two upgrade paths on one machine."
    )


def test_the_cluster_override_never_touches_the_shared_cluster_identity() -> None:
    """`POSTGRES_USER` / `POSTGRES_DB` / `POSTGRES_PASSWORD` are absent.

    All three were `muse` on this file until the adoption, and each is wrong on a
    shared cluster in a way that does not merely fail:

    - `POSTGRES_USER` — the image creates it as a SUPERUSER, so the override hands
      muse the ability to read every other service's database, undoing the
      `LOGIN NOSUPERUSER` in kit's `10-cluster.sh` outright.
    - `POSTGRES_DB` / `POSTGRES_USER` together also stop initdb DEAD: the image
      creates both before any init script is sourced, so the script's own
      `CREATE ROLE` / `CREATE DATABASE` collide and `ON_ERROR_STOP=1` makes the
      container exit 3.
    - `POSTGRES_PASSWORD` is the credential of EVERY role on the cluster, so one
      service's file would decide it for all of them.

    Asserted key by key rather than as a set, so the failure names the key that
    came back rather than printing three of them.
    """
    environment = compose_document()["services"]["postgres"].get("environment") or {}
    for key in SHARED_CLUSTER_ENV:
        assert key not in environment, (
            f"POSTGRES_* key {key!r} on the shared cluster. The cluster's identity "
            "is a decision for the whole fleet and belongs in `.env` as "
            "KIT_POSTGRES_USER / KIT_POSTGRES_DB / KIT_POSTGRES_PASSWORD — the "
            "variables kit already declares. kit's tests/fleet_check.py fails this "
            "repository over it the moment kit.ref exists, and the failure has no "
            "discretion left in it."
        )


def test_the_cluster_override_publishes_no_ports() -> None:
    """No `ports:` on the cluster, and no `volumes:` either.

    `ports:` is a LIST, and compose APPENDS a second file's list rather than
    substituting for it. This file used to publish `5433:5432` from its own `db:`
    service; a `ports:` written under the `postgres` override would have left the
    cluster listening on kit's `${KIT_POSTGRES_PORT:-15500}` AND on 5433 — which is
    not the override it reads like, and it collides with whatever else wanted 5433.

    The documented way to MOVE a published port is the VARIABLE, in `.env`:
    `KIT_POSTGRES_PORT=15433` replaces rather than appends. So the assertion is
    absence, and the file says which mechanism is the real one.

    `volumes:` is the same class of claim one layer down: a `muse-db` mount on the
    shared cluster is a volume nothing ever provisions, which is how a service ends
    up with a database the init script never made.
    """
    cluster = compose_document()["services"]["postgres"]
    assert not cluster.get("ports"), (
        "compose APPENDS a second file's `ports:` list rather than replacing it, so "
        "this buys the cluster both ports and collides with another developer's "
        "stack. Move the port with its variable instead: KIT_POSTGRES_PORT=… in .env"
    )
    assert not cluster.get("volumes"), (
        "kit owns `postgres-data`. A second volume mount here is a volume nothing "
        "provisions, which is how a service ends up with a database the init "
        "script never created."
    )
    assert not cluster.get("healthcheck"), (
        "kit's probe is a real query (`psql … -tAc 'select 1'`), not a "
        "`pg_isready` decoration; replacing it with this repository's own is how a "
        "healthcheck reports ready against a database that does not exist"
    )


def test_muses_database_is_declared_once_and_is_overridable() -> None:
    """`KIT_POSTGRES_DATABASES: ${KIT_POSTGRES_DATABASES:-muse}`.

    Three things in one assertion, because they are one decision:

    - the KEY is the mechanism: each comma-separated name becomes both a
      `NOSUPERUSER` role and a database that role owns, provisioned by kit's
      `initdb/10-cluster.sh` on a fresh volume;
    - the DEFAULT is `muse`, so a single-service checkout works with no `.env` at
      all — which is the case here, since this repository has no `.env` and no
      `bin/dev` to write one;
    - it is a `${…:-muse}` and NOT a literal `muse`, because this is the ONE key
      anybody has to extend to put a second service on this cluster. A committed
      literal could only be extended by editing this file and re-reviewing it,
      which would make "one shared cluster, nine services" a description rather
      than something a stack could do.
    """
    environment = compose_document()["services"]["postgres"]["environment"]
    assert "KIT_POSTGRES_DATABASES" in environment, (
        "muse's database and role come from KIT_POSTGRES_DATABASES and from "
        "nowhere else. Without it there is no override at all, and the fetched "
        "stack is a cluster holding some other service's database."
    )
    value = str(environment["KIT_POSTGRES_DATABASES"])
    assert value == "${KIT_POSTGRES_DATABASES:-muse}", (
        f"KIT_POSTGRES_DATABASES is {value!r}. It must be the kit variable with a "
        "`muse` default: a literal could not be extended from `.env`, and an "
        "empty default is refused by 10-cluster.sh with a message naming the fix."
    )


def test_the_service_reaches_the_cluster_by_service_name() -> None:
    """`postgres:5432/muse`, with the password interpolated and the scheme unchanged.

    Three properties, and the last is the one a well-meaning edit breaks:

    - the HOST is the compose service name, not `db` and not `localhost`. muse runs
      on kit's `platform` network, and `localhost` inside a container is that
      container;
    - the password is `${KIT_POSTGRES_PASSWORD:-cafaye}`, not the literal `muse`.
      Every role `10-cluster.sh` creates takes the cluster's `POSTGRES_PASSWORD`, so
      a literal here is a credential no role holds — an authentication failure at
      boot, not a security improvement. And a cluster password changed in `.env`
      cannot leave this file authenticating as a password that no longer exists;
    - the SCHEME stays `postgres://`. psycopg accepts `postgresql://` identically,
      so changing it breaks nothing, and changing it *for a reason borrowed from
      another service* does: `identity` needs `?sslmode=disable` because Go's
      `lib/pq` defaults `sslmode` to `require`, and psycopg 3 defaults it to
      `prefer`. A parameter added here would be a no-op wearing the credit of a
      decision, and the next reader would believe the cluster requires TLS.
    """
    environment = compose_document()["services"]["muse"]["environment"]
    url = str(environment["MUSE_DATABASE_URL"])
    assert "@postgres:5432/muse" in url, (
        f"muse must reach the shared cluster by compose service name; got {url!r}. "
        "`localhost` inside a container is that container, and `db` no longer "
        "exists now that kit's cluster does."
    )
    assert "${KIT_POSTGRES_PASSWORD" in url, (
        "the URL must interpolate kit's cluster password. `10-cluster.sh` hands "
        "every role $POSTGRES_PASSWORD, so a literal is a credential no role holds"
    )
    assert url.startswith("postgres://"), (
        f"{url!r} changed scheme. psycopg accepts both spellings, so this breaks "
        "nothing — which is exactly why it must not drift away from the one muse's "
        "own tests and driver already use."
    )


def test_the_service_is_on_kits_network() -> None:
    """`networks: [platform]`, because every hostname here is over the compose network.

    kit puts everything it ships on a network called `platform`. A service in the
    SECOND compose file that declares no `networks:` lands on the project's `default`
    instead, and `default` and `platform` are two networks in two different DNS
    domains — so `postgres` stops resolving the moment the container starts, with a
    connection refused rather than a DNS error, which is why it reads like a
    database problem.

    `courier` documents this at length on its own file and notes that no other
    adopter declares it; `identity` and `billing` get away with it. Asserted here
    because the failure mode is invisible until a developer's stack is the one that
    breaks.
    """
    assert "platform" in compose_document()["services"]["muse"].get("networks") or [], (
        "muse must declare kit's `platform` network: without it the service lands on "
        "the project default and every hostname in MUSE_DATABASE_URL stops resolving"
    )


def test_the_service_waits_only_on_the_database() -> None:
    """`depends_on` names postgres and nothing else, and the condition is `healthy`.

    The database is a readiness dependency in the ordinary sense: without it there
    is no vault and no outbox. The collector is not, and that is the load-bearing
    half — a service that waits for the collector serves no traffic while the
    collector is down, which is strictly worse than serving traffic with no traces,
    and it teaches people to switch telemetry off. kit's gate fails a `depends_on`
    naming the collector.

    `service_healthy` rather than `service_started`, because kit's probe is a real
    query against the admin database: `healthy` is the state in which initdb has
    finished and muse's own database exists.
    """
    depends_on = compose_document()["services"]["muse"].get("depends_on") or {}
    assert set(depends_on) == {"postgres"}, (
        f"depends_on is {sorted(depends_on)}; only the database belongs in a "
        "readiness path. Nothing but the collector may be in one."
    )
    assert depends_on["postgres"]["condition"] == "service_healthy", (
        "`service_started` is the state before initdb has finished, which is the "
        "state in which this service's database does not exist yet"
    )


def test_kit_ref_is_a_pin_and_the_only_one() -> None:
    """A full 40-character sha, one value, and comments around it.

    An abbreviated sha resolves happily through `git fetch` and is ambiguous
    across remotes, so two machines can disagree about what it meant; a branch is a
    MOVING reference, so the redaction allowlist and the port block this repository's
    dev loop runs would change between two runs of the same command. kit's
    `bin/dev` refuses both at run time and `tests/fleet_check.py` refuses them in
    review, and the two rules are written out twice on purpose.

    The value has to be a commit that exists on kit's REMOTE. A pin taken from a
    local `HEAD` resolves on the machine that wrote it and on no other, and the
    failure lands on a teammate's laptop with a message about a missing commit
    rather than about the mistake that caused it.
    """
    assert KIT_REF.is_file(), (
        "kit.ref is absent. `bin/dev` is the only callable path to the stack — a "
        "compose file cannot be `uses:`-ed — so a repository with no kit.ref has "
        "not said which bytes of kit it runs."
    )
    values = [
        line.strip()
        for line in KIT_REF.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    assert len(values) == 1, f"kit.ref must hold exactly one value; got {values}"
    assert re.fullmatch(r"[0-9a-f]{40}", values[0]), (
        f"{values[0]!r} is neither a 40-character commit sha nor a v<semver> tag"
    )


def test_the_kit_compose_dir_trap_is_documented_in_the_file() -> None:
    """The failure is SILENT, so the file has to name it.

    kit's compose file resolves its own build context and its initdb bind mount
    through `${KIT_COMPOSE_DIR:-.}`, and compose resolves a relative path against the
    PROJECT DIRECTORY — which `--project-directory .` sets to muse's root. Drop the
    variable and the initdb mount resolves to `<muse>/postgres/initdb`, a directory
    that does not exist, which Docker CREATES as an empty one. The cluster comes up
    healthy having run no init script at all: no role, no database, no
    `REVOKE CONNECT`, and a first `psql` saying `database "muse" does not exist`.

    Measured on courier's file before the workaround was deleted:

        source: /Users/…/wt-m39-courier-27/postgres/initdb        <- courier's tree
        source: /Users/kaka/.cache/cafaye/kit/f6d3b74…/postgres/initdb

    This repository has no `bin/dev` to set the variable, so the manual form in the
    header is the whole loop and the header has to carry the warning itself.
    Asserted on the variable and on the directory it protects, not on prose.
    """
    text = compose_text()
    assert "KIT_COMPOSE_DIR" in text, (
        "the compose header must name KIT_COMPOSE_DIR. Without it the initdb mount "
        "resolves against this repository and the cluster comes up healthy with no "
        "database in it."
    )
    assert "postgres/initdb" in text, (
        "the header must name what the variable actually protects — the directory "
        "the mount resolves to when it is missing"
    )


def test_the_orphaned_volume_is_documented_in_the_file() -> None:
    """`muse-db` is nobody's volume any more, and the file has to say so.

    The old `db:` service mounted a NAMED volume, `muse-db`. This file no longer
    declares it, so nothing mounts it again and nothing deletes it — and the rows in
    it are every provider credential a developer has stored, which are not
    reproducible from this repository.

    The sharpest edge is `docker compose down -v`, which USED to be the recovery and
    now silently is not: it removes the volumes of the compose PROJECT it is run
    against, and after the adoption that project is kit's, whose volumes do not
    include `muse-db`. A command that used to do the job and now does nothing is
    the worst kind of note to leave out of a file, so the file names it.

    Asserted on the two operations that actually move the rows — `pg_dump` /
    `pg_restore`, and the volume removal — rather than on prose, because prose is
    what a future re-pin deletes.
    """
    text = compose_text()
    assert "muse-db" in text, (
        "the compose file must tell a developer holding the old `muse-db` volume "
        "what became of it; nothing mounts it and nothing deletes it now"
    )
    assert "pg_dump" in text and "pg_restore" in text, (
        "the note must name the operation that actually carries the rows across a "
        "volume boundary: a LOGICAL dump, not a copy of the data directory"
    )
    assert "down -v" in text, (
        "the note must explain that `down -v` no longer reaches `muse-db`, because "
        "the project it removes volumes from is now kit's"
    )
    assert "incompatible" in text or "cannot" in text or "not readable" in text, (
        "the 18 → 17 history must still say why one major cannot start against the "
        "other's data directory"
    )


def test_every_migration_the_header_documents_exists() -> None:
    """The header's `psql` lines name files that are really there.

    A developer copying a filename out of a comment into a shell is doing the most
    error-prone thing this repository asks of anyone, and a typo there costs a
    confusing `could not open file` rather than a clear failure.
    """
    text = compose_text()
    named = set(re.findall(r"migrations/(\d{5}_[a-z_]+\.sql)", text))
    on_disk = {path.name for path in MIGRATIONS.glob("*.sql")}
    assert on_disk, "muse has no migrations, so the header documents nothing real"
    assert named, "the compose header must document how to apply the migrations"
    missing = named - on_disk
    assert not missing, f"the compose header names migrations that do not exist: {missing}"


def test_the_vault_key_has_no_default_anywhere_in_the_stack() -> None:
    """AGENTS.md rule 10, asserted on the file rather than trusted.

    A vault that boots with a default key is a vault whose keys are readable by anyone
    who can read this repository, so the compose value must be an interpolation that
    *refuses* when the variable is absent — `${MUSE_VAULT_KEY:?...}` — and never a
    literal.
    """
    environment = compose_document()["services"]["muse"]["environment"]
    value = str(environment["MUSE_VAULT_KEY"])
    assert "${" in value, (
        "MUSE_VAULT_KEY in compose must come from the environment; a literal here is "
        "a key committed to a repository"
    )
    assert ":?" in value, (
        "MUSE_VAULT_KEY must use ${...:?...} so an unset variable is a refusal "
        "(a boot failure) rather than an empty string muse has to notice later"
    )


def test_no_credential_is_written_into_the_stack() -> None:
    """The file is committed, so nothing in it may look like a key.

    `POSTGRES_PASSWORD: muse` and `MUSE_DATABASE_URL: postgres://muse:muse@db` were a
    local development stack's well-known throwaway values, and the second is the kind
    of string a secret scanner reads as a credential. Both are gone from this file —
    the URL now interpolates kit's cluster password — and rather than leave that as
    a fact about the past, this names what would actually be a leak: a real provider
    API key, or a vault key literal.
    """
    text = compose_text()
    # `[A-Za-z0-9_-]`, not `[A-Za-z0-9]`: real provider keys are hyphenated after the
    # `sk-` prefix (`sk-proj-…`, `sk-ant-…`), and the first version of this regex
    # stopped at the hyphen — so it passed a committed `sk-proj-AAAABBBB…` and the
    # guard was green over the thing it exists to catch. The bug was found by
    # breaking the guard on purpose, which is the only way to know a guard is one.
    committed_key = re.search(r"\bsk-[A-Za-z0-9_-]{16,}", text)
    assert committed_key is None, "an API key is committed in the compose file"
    assert not re.search(r"eyJ[A-Za-z0-9_-]{10,}", text), "a JWT is committed in the compose file"

    vault = compose_document()["services"]["muse"]["environment"]["MUSE_VAULT_KEY"]
    inner = str(vault).split(":", 2)[-1].rstrip("}")  # what follows `${MUSE_VAULT_KEY:?`
    assert not re.fullmatch(r"[A-Za-z0-9+/=]{40,}", inner), (
        "MUSE_VAULT_KEY looks like a literal key with a refusal message around it"
    )


def test_the_compose_file_has_no_merge_conflict_residue() -> None:
    """Kit's newest own gate checks this in every repository it derives from.

    `git merge` conflict markers are the shape a conflicted file takes, and a compose
    file that carries them does not fail loudly — it fails as a YAML parse error, or
    worse, it parses and brings up half a stack. Worth the four lines.
    """
    text = compose_text()
    for marker in ("<<<<<<< ", ">>>>>>> "):
        assert marker not in text, (
            f"the marker {marker!r} is committed in the compose file: a conflicted "
            "stack does not fail loudly, it fails as a YAML parse error"
        )
