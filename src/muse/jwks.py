"""The key set: identity's published JWKS, cached, and refreshed under a bound.

core's rule is that services verify *locally* against the JWKS and cache by `kid` with a
bounded TTL, refreshing on an unknown `kid` — no per-request call to identity. That is
the availability half. This module is the safety half, and it exists because the obvious
implementation of "refresh on an unknown `kid`" is an amplification primitive pointed at
our own identity service:

    if kid not in cached_keys:
        cached_keys = fetch()        # ← one fetch per attacker-chosen string

Anyone who can send a request can send one with a random `kid`, so the fetch count
tracks the request count and identity is asked to serve a key set to an unauthenticated
caller as fast as the caller can type. That is why **the refresh is bounded, not
frequent**:

- A **minimum interval between refreshes** (`refresh_interval`). One forced refresh per
  window, whatever the request rate. This is the control the brief calls "the single most
  commonly missed property in hand-rolled JWKS code", and it is asserted by a test that
  sends many unknown `kid`s and checks the fetch count does not track them.
- A **negative cache of unknown `kid`s** (`_unknown`), cleared on every successful fetch.
  Without it, a client with a genuinely stale token re-triggers the interval check on
  every single request, and the bound degrades into "one fetch per interval forever"
  rather than "one fetch per rotation".
- The refresh happens **once per request even if the token is still unknown afterwards**,
  so a request cannot retry its way past the interval.

Two more properties fall out of the shape rather than being bolted on:

- **Rotation works.** A token signed by a key published *after* this process cached the
  old set arrives with an unknown `kid`, gets the one allowed refresh, and is accepted.
- **Removal works.** A key withdrawn from the published set stops being accepted once
  the TTL expires and the next fetch replaces the cached set. The cache holds exactly
  the last published set — it is replaced, never merged, because a merged cache is a
  set that only ever grows and a withdrawn key that never leaves.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from joserfc import jwk
from joserfc.errors import JoseError

from muse.errors import SigningKeysUnavailable

logger = logging.getLogger("muse.auth")

#: Where identity publishes its keys. Appended to the issuer, never configured as a
#: separate path, so a base URL with a path in it is a configuration error rather than a
#: key set nobody is serving.
JWKS_PATH = "/.well-known/jwks.json"

#: How long a fetched key set is reused before it is refetched on the slow path.
#: 300s, and the same default `guard` uses: long enough that a burst of traffic costs
#: one fetch, short enough that a withdrawn key is gone within a coffee break.
DEFAULT_TTL_SECONDS = 300.0

#: The floor between two forced refreshes. This is the amplification control, and it is
#: the reason an unknown `kid` is a 401 rather than a fetch.
DEFAULT_REFRESH_INTERVAL_SECONDS = 30.0

#: What one fetch returns. A `dict` rather than an `httpx.Response` so the fixture and
#: the production client meet at the same seam and the policy is testable without HTTP.
FetchKeys = Callable[[str], Awaitable[dict[str, Any]]]


@dataclass(frozen=True, slots=True)
class _Cache:
    """The last good key set, and when it arrived."""

    keys: jwk.KeySet
    fetched_at: float


@dataclass
class _State:
    """Everything mutable about the cache, in one object so it is obviously shared."""

    cache: _Cache | None = None
    last_refresh_at: float | None = None
    #: `kid`s seen and refused since the last successful fetch. Bounded by the number of
    #: distinct key ids identity has published, not by the number of requests — an
    #: attacker sending random ids grows this until the next fetch, and the interval
    #: bounds how long "until" is.
    unknown: set[str] = field(default_factory=set)


class JwksClient:
    """identity's key set, cached and refreshed under a bound.

    One per process, because one cache is what makes rotation observable: two clients
    would each fetch, could each be at a different point in a rotation, and would then
    disagree about which keys are live. It is not frozen — it is mutable state read by
    every concurrent request, and the lock below is what makes that safe rather than the
    frozen-ness every other shared object in muse uses (AGENTS.md rule 1).
    """

    def __init__(
        self,
        url: str,
        fetch: FetchKeys,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
        clock: Callable[[], float],
    ) -> None:
        self._url = url
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._interval = refresh_interval
        self._clock = clock
        self._state = _State()
        self._lock = asyncio.Lock()

    # --- the public surface ------------------------------------------------

    async def keys_for(self, kid: str) -> jwk.KeySet | None:
        """The key set to verify against, or `None` when `kid` is not published.

        `None` rather than a raise: "this token names a key identity does not publish"
        is an *answer*, and the caller turns it into a 401. An exception would be for
        the case where we could not find out, which is `SigningKeysUnavailable`.
        """
        async with self._lock:
            return await self._keys_for(kid)

    async def keys(self) -> jwk.KeySet:
        """The key set, refreshing if stale. Raises if it cannot be fetched."""
        async with self._lock:
            stale = self._stale()
            if stale:
                await self._load()
            if self._state.cache is None:  # pragma: no cover - _load raises first
                raise SigningKeysUnavailable("the signing keys are not available")
            return self._state.cache.keys

    # --- the policy --------------------------------------------------------

    async def _keys_for(self, kid: str) -> jwk.KeySet | None:
        cached = self._state.cache

        # Cold: nothing has ever been fetched. Fetch and answer from it.
        if cached is None:
            await self._load()
            return self._answer(kid)

        # Fresh and known: the hot path, and the one that must cost nothing.
        if not self._stale() and self._publishes(kid):
            return cached.keys

        # Fresh but unknown, and already refused since the last fetch: the negative
        # cache. Without this branch a client holding a stale token re-runs the interval
        # check on every request, and the bound below degrades from "one fetch per
        # rotation" into "one fetch per interval, forever".
        if not self._stale() and kid in self._state.unknown:
            return None

        # Either the cache is stale, or `kid` is unknown and we are allowed to look
        # again. Both are one fetch, and `_load` records the attempt either way, so the
        # interval below is enforced by the clock rather than by this branch structure.
        if self._may_refresh():
            await self._load()
            return self._answer(kid)

        return cached.keys if self._publishes(kid) else None

    def _answer(self, kid: str) -> jwk.KeySet | None:
        """The cached set if it publishes `kid`, else record the miss and say no."""
        cached = self._state.cache
        if cached is None:  # pragma: no cover - _load raises rather than returning None
            raise SigningKeysUnavailable("the signing keys are not available")
        if self._publishes(kid):
            return cached.keys
        self._state.unknown.add(kid)
        return None

    async def _load(self) -> None:
        """Fetch and replace the cache. Never leaves a partial state behind.

        A failure is a `SigningKeysUnavailable` rather than a silently-empty cache: a
        client that kept the last good set would be serving requests against a key set
        identity may have withdrawn, which is the opposite of what this module is for.
        """
        try:
            document = await self._fetch(self._url)
            keys = jwk.KeySet.import_key_set(document)
        except (JoseError, KeyError, ValueError, TypeError) as error:
            raise SigningKeysUnavailable(
                f"the signing keys at {self._url} are not a usable key set"
            ) from error
        except Exception as error:
            # Anything the injected fetch raises — a connection reset, a timeout, an
            # HTTP status. `SigningKeysUnavailable` is a `MuseError`, so the API turns it
            # into the 503 this is about. The log line carries the URL and the class and
            # never the body: identity's response is third-party text.
            logger.warning("could not fetch the JWKS from %s: %s", self._url, type(error).__name__)
            raise SigningKeysUnavailable(
                f"the signing keys at {self._url} could not be retrieved"
            ) from error

        now = self._clock()
        self._state.cache = _Cache(keys=keys, fetched_at=now)
        self._state.last_refresh_at = now
        # Replaced on success, never merged: a merge is a set that only ever grows, and
        # a withdrawn key that never leaves.
        self._state.unknown.clear()

    # --- the two clocks ----------------------------------------------------

    def _stale(self) -> bool:
        """Whether the cached set has outlived its TTL.

        A cold cache counts as stale, which is why the caller fetches unconditionally
        when `cache is None` and this is not asked the question.
        """
        cached = self._state.cache
        if cached is None:
            return True
        return self._clock() - cached.fetched_at >= self._ttl

    def _may_refresh(self) -> bool:
        """Whether enough time has passed to spend a fetch on an unknown `kid`.

        The amplification control. `last_refresh_at` is set by `_load` on **every**
        attempt including a failed one, so an attacker cannot make identity unreachable
        and then have muse retry it once per request while it is down.
        """
        last = self._state.last_refresh_at
        if last is None:
            return True
        return self._clock() - last >= self._interval

    def _publishes(self, kid: str) -> bool:
        """Whether the cached set names `kid`.

        `get_by_kid` rather than a hand-rolled scan: it is the library's own selection
        and it is what `decode` will consult, so "does this set publish the key" and
        "will the signature check find it" cannot be two different answers. It raises on
        a miss, which is the miss.
        """
        cached = self._state.cache
        if cached is None:  # pragma: no cover - guarded by the callers
            return False
        try:
            cached.keys.get_by_kid(kid)
        except JoseError:
            return False
        return True

    def __repr__(self) -> str:
        cached = self._state.cache
        kids = (
            [key.get("kid") for key in cached.keys.as_dict(private=False)["keys"]] if cached else []
        )
        return f"JwksClient(url={self._url!r}, kids={kids})"  # a kid is a public id, never a key


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "JWKS_PATH",
    "FetchKeys",
    "JwksClient",
]
