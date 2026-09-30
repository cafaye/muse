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

import time

from muse.auth import TokenVerifier
from muse.breaker import BreakerRegistry
from muse.jwks import JwksClient
from muse.main import Container, Settings, create_app
from muse.metering import Meter
from muse.providers import ProviderRegistry
from muse.providers.credentials import CredentialResolver, StaticCredentials
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RouteTable
from muse.telemetry import Telemetry
from muse.vault import Vault, load_vault_key

from .fake_database import FakeDatabase
from .jwks import AUDIENCE, ISSUER, Identity

#: A valid vault key for tests. Generated once and pinned: a test that minted a fresh
#: key per app could not compare two apps' ciphertexts, and "the test key" in a
#: fixture is one less thing to reason about than a generator call.
TEST_KEY = load_vault_key({"MUSE_VAULT_KEY": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="})

#: The identity the whole suite verifies against. Module-level because `AUTH_HEADERS` is
#: a module-level constant in `conftest.py`: one published key, one minted token, and no
#: test can sign a token for a key that is not the one the app will fetch.
IDENTITY = Identity()


class FakeClock:
    """A clock a test moves by hand.

    The JWKS cache's two windows — the TTL and the minimum interval between forced
    refreshes — are *schedules*, not elapsed times. Asserting on them by sleeping would
    make the suite slower than the thing it describes and flaky at both ends; injecting
    the clock is what lets a test say "advance past the refresh interval" and assert on
    what the policy then does. Same technique as `Router(clock=..., unit=...)`
    (AGENTS.md rule 14).
    """

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


#: The key-set URL the fixture serves from, built the way production builds it.
JWKS_URL = f"{ISSUER}/.well-known/jwks.json"


def auth_for(
    identity: Identity = IDENTITY, *, clock=time.time, cache_clock: FakeClock | None = None
) -> TokenVerifier:
    """A verifier wired to `identity`, for a container that is not the default one.

    `clock` is the *claim* clock (what "now" means for `exp`), and `cache_clock` is the
    *cache* clock (what "now" means for the TTL and the refresh interval). They are
    separate because they answer different questions and a test is almost always about
    exactly one of them: an expiry test moves the first, a rotation test moves the
    second, and conflating them is how a test ends up passing for the wrong reason.
    """
    return TokenVerifier(
        issuer=ISSUER,
        audience=AUDIENCE,
        jwks=JwksClient(JWKS_URL, identity.fetch, clock=cache_clock or FakeClock()),
        clock=clock,
    )


def build_test_app(
    *,
    registry: ProviderRegistry,
    database: FakeDatabase,
    table: RouteTable,
    credentials: CredentialResolver | None = None,
    scrub_secrets: tuple[Secret, ...] = (),
    telemetry: Telemetry | None = None,
    breakers: BreakerRegistry | None = None,
    auth: TokenVerifier | None = None,
):
    """An app whose container holds exactly what the test passed in.

    `scrub_secrets` is what the endpoint hands the router to remove from provider
    text. It is empty by default because every real adapter scrubs its own credential;
    a test using a double that does not is the case it exists for.

    `telemetry` and `breakers` are threaded to *both* the container and the router,
    which is the whole point: two tracers would produce two disjoint traces under one
    trace id, and a test asserting the provider span nests under the request span
    would pass against one and fail against production.

    `auth` defaults to a real verifier over `IDENTITY`, not to a permissive stub. Every
    test in this suite therefore drives signature verification, which is the only way
    the auth tests can claim the other tests are unaffected by them.
    """
    resolver = credentials or StaticCredentials({})
    vault = Vault(database, TEST_KEY)
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table, telemetry=telemetry, breakers=breakers),
        vault=vault,
        meter=Meter(database),
        credentials=resolver,
        auth=auth or auth_for(),
        scrub_secrets=scrub_secrets,
        telemetry=telemetry if telemetry is not None else Telemetry.noop(),
    )
    return create_app(container=container)
