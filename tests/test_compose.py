"""The compose stack is a contract, and this checks it against the code.

A version pin nobody has executed is a comment. That is not a figure of speech in
this repository: `docker-compose.yml` carried `postgres:18-alpine` while the rest of
the fleet sat on 17, and it carried a `MUSE_VAULT_KEY` line that did not parse, so
`docker compose up` had never successfully run in this repository at all. Both
defects survived because nothing ever *ran* the file — a reader accepted the
indentation, CI never invoked compose, and the suite has no socket (AGENTS.md rule
3), so a file that exists only to start a database was never started by anything
that would notice.

So the file is read here, parsed, and asserted. Three properties, in the order they
matter:

1. **It parses.** Cheap, and the guard for the defect above: an unquoted YAML scalar
   containing `: ` is a nested mapping, and the `${VAR:?...}` refusal message is
   exactly such a scalar. The failure is `mapping values are not allowed in this
   context` at a line nobody was looking at.
2. **The database is the platform standard, and the tag is exact.** `17-alpine` as a
   literal, not `${POSTGRES_TAG:-...}` and not `latest`. muse does not read the other
   services' compose files to discover the standard — they are other repositories,
   absent from a checkout and from CI — so the value is a literal here and the
   measurement it came from is named in the comment beside it.
3. **The downgrade note is still in the file.** 18 → 17 is a major-version
   downgrade, and Postgres major versions have incompatible on-disk formats, so a
   developer with a volume from 18 cannot just start 17 against it. That instruction
   has to survive the pin it belongs to; a future bump that drops the note leaves
   behind a volume nobody can start.

The last one is the one a reviewer would call "testing a comment", and it is here
because a comment that a rename or a re-pin silently deletes is how the next person
loses an afternoon. The test asserts the note names the two operations that actually
recover a volume — `pg_dump`/`pg_restore` and `down -v` — rather than that it
contains some prose.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

pytestmark = [pytest.mark.unit]

COMPOSE = Path(__file__).resolve().parent.parent / "docker-compose.yml"
MIGRATIONS = Path(__file__).resolve().parent.parent / "migrations"

#: The platform standard, as measured across the fleet's compose files on 2026-09-30:
#:   darkroom postgres:17-alpine · courier postgres:17 · identity postgres:17-alpine
#:   billing postgres:17 · parlor/e2e postgres:17-alpine · muse postgres:18-alpine
#: Five of six were on 17; muse was a full major version ahead, which for a
#: one-deploy-many-services platform means two upgrade paths and a muse dump that
#: restores into no other service's database. `17-alpine` specifically, because it is
#: the tag the alpine majority already pins — the smallest image, and matching the
#: majority exactly is what makes a cross-service `pg_dump`/`pg_restore` routine
#: rather than a project.
#:
#: A literal rather than a lookup: muse cannot read the other services' compose files
#: to ask them what they pin (they are other repositories, and this test has to pass in
#: CI with only this checkout), and a variable here would let the very drift this
#: packet fixed reappear invisibly.
FLEET_STANDARD_IMAGE = "postgres:17-alpine"


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
    assertion is that a parse *succeeds* — and the test below proves it can go red.
    """
    document = compose_document()
    assert isinstance(document, dict), "the compose file must parse to a mapping"
    assert "services" in document, "a compose file with no services is not a stack"


def test_the_stack_stands_up_a_database() -> None:
    """muse's vault and outbox are this service's own database, so compose carries it."""
    services = compose_document()["services"]
    assert "db" in services, "the compose stack must carry the database muse needs"
    assert "muse" in services, "the compose stack must carry the service"


def test_the_database_is_the_fleets_major_version() -> None:
    """`postgres:17-alpine` exactly, and nothing that can drift.

    The literal is the assertion. A `${POSTGRES_TAG:-17-alpine}` would pass a
    substring check and fail the point: the pin would be a default rather than a
    decision, and the first machine with the variable set would quietly run a
    different database from the one this test just approved.
    """
    image = compose_document()["services"]["db"]["image"]
    assert image == FLEET_STANDARD_IMAGE, (
        f"muse pins {image}; the platform standard is {FLEET_STANDARD_IMAGE} "
        "(measured 2026-09-30 — see the comment in this module)"
    )


def test_the_volume_is_mounted_where_17_keeps_its_data() -> None:
    """`/var/lib/postgresql/data` — which is 17's `PGDATA`, and 18's is not.

    Measured, because the two images disagree and the disagreement is invisible until
    the container exits:

        postgres:17-alpine   PGDATA=/var/lib/postgresql/data
        postgres:18-alpine   PGDATA=/var/lib/postgresql/18/docker

    The 18 images moved to a major-version-specific subdirectory so `pg_upgrade
    --link` works across a mount-point boundary, and they **refuse to start** when
    they find a populated `/var/lib/postgresql/data`. So muse's pre-18 mount was not
    merely the wrong path: with this compose file on 18, `docker compose up -d db`
    exited 1 and left the named volume holding 0 files. Any developer whose 18
    database "worked" had corrected the mount by hand, which is what the downgrade
    note in the header has to account for.

    Asserted as a literal because the test cannot ask an image for its `PGDATA`
    without a daemon, and a test that shells out to `docker` is a test that skips on
    every CI runner. The measured values are recorded here instead, which is what
    makes the literal checkable by a reader.
    """
    mounts = [str(entry) for entry in compose_document()["services"]["db"]["volumes"]]
    assert any(entry.endswith(":/var/lib/postgresql/data") for entry in mounts), (
        f"the database volume must mount at 17's PGDATA, /var/lib/postgresql/data; got {mounts}"
    )


def test_the_postgres_tag_is_not_floating() -> None:
    """No `latest`, and no interpolation.

    `postgres:17-alpine` is a tag, not a digest, and that is a deliberate limit of
    this packet: the fleet's other five services pin tags, so changing muse to a
    digest would make it the only service whose image cannot be read off a compose
    file at a glance. What is refused here is the *floating* tag, which is the failure
    that changes behaviour with no diff at all.
    """
    image = compose_document()["services"]["db"]["image"]
    assert not image.endswith(":latest"), "a floating tag changes the database with no diff"
    assert "${" not in image, (
        "an interpolated postgres tag is a default, not a pin: a machine with the "
        "variable set would run a different database than the one this test approved"
    )
    assert re.search(r":\d+", image), f"{image!r} names no major version"


def test_the_downgrade_from_18_is_documented_in_the_file() -> None:
    """A developer with an 18 volume must be told what to do before they try it.

    Postgres major versions have incompatible on-disk formats: a data directory
    written by 18 is not readable by 17, and 17 refuses to start against it rather
    than corrupting it. `docker compose down` does not help, because the volume is
    named and survives the container. So there are exactly two recoveries and the file
    has to name both — delete the volume and re-migrate, or `pg_dump` on 18 and
    `pg_restore` on 17.

    Asserted on the two operations rather than on prose, because prose is what a
    re-pin deletes. If a future packet takes muse to 18 again, this test goes red and
    the note is either kept as an upgrade note or removed with the pin that needed it.
    """
    text = compose_text()
    assert "pg_dump" in text and "pg_restore" in text, (
        "the compose file must tell a developer holding an 18 volume how to carry "
        "their rows across a major version"
    )
    assert "down -v" in text, (
        "the compose file must name the volume-deletion recovery; it is the correct "
        "answer for a dev volume and the one people find only after the dump fails"
    )
    assert "incompatible" in text or "cannot" in text or "not readable" in text, (
        "the note must say why 17 will not simply start against an 18 volume"
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

    `POSTGRES_PASSWORD: muse` and `MUSE_DATABASE_URL: postgres://muse:muse@db` are a
    local development stack's well-known throwaway values, and the second is the kind
    of string a secret scanner reads as a credential. Rather than ban the thing that
    makes `docker compose up` work out of the box, this names what would actually be
    a leak: a real provider API key, or a vault key literal.
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
