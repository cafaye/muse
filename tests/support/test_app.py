"""A `muse` application wired to fakes.

In `tests/support/` rather than in `src/`, because it is a test convenience: a
production module that can be handed a fake registry is a production module whose
production wiring is optional, and that is one path too many for a boot sequence to
have. The production path is `muse.main.build_container`; this builds the same
`Container` with test doubles in it.

The result is that every HTTP test drives the *real* handler, the *real* router and
the *real* meter, with no socket (AGENTS.md rule 3) and no provider.
"""

from __future__ import annotations

from muse.main import Container, Settings, create_app
from muse.metering import Meter
from muse.providers import ProviderRegistry
from muse.providers.credentials import CredentialResolver, StaticCredentials
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RouteTable
from muse.vault import Vault, load_vault_key

from .fake_database import FakeDatabase

#: A valid vault key for tests. Generated once and pinned: a test that minted a fresh
#: key per app could not compare two apps' ciphertexts, and "the test key" in a
#: fixture is one less thing to reason about than a generator call.
TEST_KEY = load_vault_key({"MUSE_VAULT_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})


def build_test_app(
    *,
    registry: ProviderRegistry,
    database: FakeDatabase,
    table: RouteTable,
    credentials: CredentialResolver | None = None,
    scrub_secrets: tuple[Secret, ...] = (),
):
    """An app whose container holds exactly what the test passed in.

    `scrub_secrets` is what the endpoint hands the router to remove from provider
    text. It is empty by default because every real adapter scrubs its own credential;
    a test using a double that does not is the case it exists for.
    """
    resolver = credentials or StaticCredentials({})
    vault = Vault(database, TEST_KEY)
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table),
        vault=vault,
        meter=Meter(database),
        credentials=resolver,
        scrub_secrets=scrub_secrets,
    )
    return create_app(container=container)
