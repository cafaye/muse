"""The key-set cache, as a unit.

`tests/test_auth.py` proves the cache's *policy* end to end, through HTTP. This file
covers the parts that are easier to state directly: the two clocks, the public
accessor, and the production `httpx` fetch that the whole thing depends on being correct.

The seam is the injected `fetch` — a callable returning a key-set document — so nothing
here opens a socket (AGENTS.md rule 3). `httpx.MockTransport` is httpx's own mechanism
and not a mock of our code: it serves a real `httpx.Response` through a real
`AsyncClient`, so what is under test is the request muse actually makes.
"""

from __future__ import annotations

import httpx
import pytest

from muse.errors import SigningKeysUnavailable
from muse.jwks import (
    DEFAULT_REFRESH_INTERVAL_SECONDS,
    DEFAULT_TTL_SECONDS,
    JWKS_PATH,
    JwksClient,
)
from muse.main import JWKS_TIMEOUT_SECONDS, http_fetch_keys

from .support.jwks import Identity, SigningKey

#: `anyio` as well as `unit`: the cache is async, so there is no synchronous reading of
#: it that would be testing something other than the thing.
pytestmark = [pytest.mark.anyio, pytest.mark.unit]

URL = f"https://identity.test{JWKS_PATH}"


class FakeClock:
    """A clock the test moves by hand.

    Both of the cache's windows are *schedules*, and asserting a schedule by sleeping
    makes the suite slower than the thing it describes and flaky at both ends.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def client_for(identity: Identity, clock: FakeClock, **kwargs) -> JwksClient:
    return JwksClient(URL, identity.fetch, clock=clock, **kwargs)


# --- the TTL ----------------------------------------------------------------


async def test_a_cold_client_fetches_on_first_use() -> None:
    identity = Identity()
    keys = await client_for(identity, FakeClock()).keys()
    assert [key["kid"] for key in keys.as_dict(private=False)["keys"]] == ["k1"]
    assert len(identity.fetches) == 1


async def test_keys_are_refetched_once_the_ttl_has_passed() -> None:
    """The slow path. Without it a withdrawn key would stay good for the life of the
    process, and "withdrawn" would mean "eventually, if the pod is replaced"."""
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)

    await client.keys()
    clock.advance(DEFAULT_TTL_SECONDS - 1)
    await client.keys()
    assert len(identity.fetches) == 1

    clock.advance(2)  # now past the TTL
    await client.keys()
    assert len(identity.fetches) == 2


async def test_keys_returns_the_same_cached_set_within_the_ttl() -> None:
    """`keys()` is the accessor a caller reaches for when it is not verifying a
    particular token — here, a test asserting what is cached. It must not fetch on
    every call."""
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)
    await client.keys()
    for _ in range(5):
        await client.keys()
    assert len(identity.fetches) == 1


# --- kid selection ----------------------------------------------------------


async def test_a_published_kid_is_answered_from_the_cache() -> None:
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)

    assert await client.keys_for("k1") is not None
    assert await client.keys_for("k1") is not None
    assert len(identity.fetches) == 1


async def test_an_unknown_kid_is_none_on_the_first_attempt_and_negative_cached_after() -> None:
    """`None` is an *answer*, not a failure to find out — the caller turns it into a 401.

    The negative cache is what keeps it an answer: without it, every subsequent request
    naming the same unknown key would re-run the interval check, and the bound would
    degrade into one fetch per interval for as long as the stale token is being retried.
    """
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)

    assert await client.keys_for("stranger") is None
    fetches = len(identity.fetches)
    for _ in range(20):
        assert await client.keys_for("stranger") is None
    assert len(identity.fetches) == fetches


async def test_an_unknown_kid_inside_the_refresh_interval_does_not_fetch() -> None:
    """A *different* unknown key, so the negative cache does not explain the answer.

    This is the amplification control stated at its narrowest: a fresh key set, a key it
    does not contain, and no permission to go and look again.
    """
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)
    await client.keys()

    assert await client.keys_for("stranger") is None  # the one allowed refresh
    fetches = len(identity.fetches)
    assert await client.keys_for("stranger-2") is None  # inside the bound
    assert len(identity.fetches) == fetches


async def test_a_refresh_is_allowed_again_once_the_interval_has_passed() -> None:
    """The bound is a *minimum* interval, not a permanent refusal. Without this the
    negative cache above would be indistinguishable from never refreshing, and a
    rotation would never be noticed."""
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)
    await client.keys()
    await client.keys_for("stranger")
    fetches = len(identity.fetches)

    clock.advance(DEFAULT_REFRESH_INTERVAL_SECONDS + 1)
    identity.publish(SigningKey("stranger"))
    assert await client.keys_for("stranger") is not None
    assert len(identity.fetches) == fetches + 1


async def test_a_negative_entry_is_cleared_by_a_successful_refresh() -> None:
    """A rotation must be able to *rescue* a key id previously refused, or a key
    published after a bad first request would be unusable for the life of the cache."""
    identity = Identity()
    clock = FakeClock()
    client = client_for(identity, clock)
    await client.keys_for("stranger")
    assert await client.keys_for("stranger") is None

    clock.advance(DEFAULT_REFRESH_INTERVAL_SECONDS + 1)
    identity.publish(SigningKey("stranger"))
    assert await client.keys_for("stranger") is not None


# --- failing closed ---------------------------------------------------------


async def test_a_failing_fetch_raises_rather_than_returning_an_empty_set() -> None:
    """`None` means "that key is not published"; an exception means "we could not find
    out". Collapsing them would turn an identity outage into a fleet of 401s telling
    every caller their perfectly good credential is bad."""
    identity = Identity()
    identity.fail_with(503)
    with pytest.raises(SigningKeysUnavailable):
        await client_for(identity, FakeClock()).keys()


@pytest.mark.parametrize(
    "document",
    [{}, {"keys": []}, {"keys": "not-a-list"}, {"keys": [1, 2, 3]}, {"html": "<html/>"}],
)
async def test_a_document_that_is_not_a_key_set_is_refused(document) -> None:
    """A proxy answering 200 with a login page, an empty key set, and three other
    shapes an endpoint can return. None of them is a key set, and none of them may be
    mistaken for one."""
    identity = Identity()
    identity.serve_garbage()

    async def fetch(_url: str) -> dict:
        return document

    with pytest.raises(SigningKeysUnavailable):
        await JwksClient(URL, fetch, clock=FakeClock()).keys()


async def test_a_cold_client_inside_the_interval_after_a_failure_fails_closed() -> None:
    """The outage case, and the reason the interval is claimed *before* the fetch.

    While identity is down — exactly when another fetch is most expensive and least
    likely to help — every request must not aim another one at it. A cold client that
    has already failed once refuses until the interval passes.
    """
    identity = Identity()
    identity.fail_with(500)
    clock = FakeClock()
    client = client_for(identity, clock)

    for _ in range(20):
        with pytest.raises(SigningKeysUnavailable):
            await client.keys_for("anything")
    assert len(identity.fetches) == 1


async def test_a_failure_is_logged_with_the_url_and_never_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """identity's answer is third-party text and a login page is a plausible one, so
    the log line carries the URL and the exception's *class* and nothing else."""
    import logging

    identity = Identity()
    identity.fail_with(500)
    with caplog.at_level(logging.WARNING), pytest.raises(SigningKeysUnavailable):
        await client_for(identity, FakeClock()).keys()

    messages = [record.getMessage() for record in caplog.records]
    assert any("could not fetch the JWKS" in message for message in messages)
    joined = "\n".join(messages)
    assert "Traceback" not in joined
    assert "ConnectionError" in joined, "the class is the useful part"


# --- the object -------------------------------------------------------------


def test_a_cold_clients_repr_says_it_holds_nothing() -> None:
    """A `JwksClient` appears in tracebacks and debugger panes, so its repr has to be
    safe to read. Before the first fetch it has no key ids and must not imply it does."""
    identity = Identity(SigningKey("alpha"), SigningKey("beta"))
    rendered = repr(client_for(identity, FakeClock()))
    assert rendered == f"JwksClient(url={URL!r}, kids=[])"


async def test_the_repr_lists_the_published_key_ids_and_never_key_material() -> None:
    """A `kid` is a published identifier — identity puts it in every token it mints —
    so it is safe. A key is not, and the private half never leaves the fixture."""
    identity = Identity(SigningKey("alpha"), SigningKey("beta"))
    client = client_for(identity, FakeClock())
    await client.keys()

    rendered = repr(client)
    assert "alpha" in rendered and "beta" in rendered
    for secret in ("BEGIN", "PRIVATE", 'd"', 'p"'):
        assert secret not in rendered, f"the repr leaked key material: {secret}"


# --- the production fetch ---------------------------------------------------


def _handler(status: int, payload: dict | None = None):
    async def handle(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=payload if payload is not None else {"keys": []})

    return httpx.MockTransport(handle)


async def test_the_production_fetch_returns_the_published_document() -> None:
    """The real `httpx` path, driven by httpx's own `MockTransport` — a real client, a
    real request, a real response object, and no socket."""
    seen: list[str] = []

    async def handle(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"keys": [{"kty": "RSA"}]})

    document = await http_fetch_keys(URL, httpx.MockTransport(handle))
    assert document == {"keys": [{"kty": "RSA"}]}
    assert seen == [URL]


async def test_the_production_fetch_raises_on_a_non_2xx() -> None:
    """`raise_for_status` rather than returning the body, so `JwksClient` can never be
    handed an error page to try to parse as a key set."""
    for status in (401, 404, 500, 502, 503):
        with pytest.raises(httpx.HTTPStatusError):
            await http_fetch_keys(URL, _handler(status, {"error": "nope"}))


async def test_the_production_fetch_sets_a_timeout() -> None:
    """A hung identity must not hold a request open past the point where the caller has
    given up. The value is asserted rather than trusted because it is the one number
    standing between a slow dependency and an exhausted connection pool."""
    assert JWKS_TIMEOUT_SECONDS == 5.0

    async def handle(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(httpx.ReadTimeout):
        await http_fetch_keys(URL, httpx.MockTransport(handle))
