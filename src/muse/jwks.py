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
import time
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
    #: `kid`s we have looked for and not found, mapped to when we looked.
    #:
    #: This is the *record* of a check rather than a second control: the interval in
    #: `_may_refresh` is what actually bounds fetching, and an entry here is only live for
    #: that same interval. It earns its place twice — it names the specific reason a key
    #: id is not usable ("we looked, at T"), and it is cleared by any successful fetch so
    #: a rotation can rescue an id previously refused.
    #:
    #: Timestamped rather than a plain set, and that is the part that is easy to get
    #: wrong: an entry that outlives the interval would keep refusing a key id that a
    #: rotation has since published, and the caller would be told "unknown key" for as
    #: long as the process lived.
    unknown: dict[str, float] = field(default_factory=dict)


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
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._url = url
        self._fetch = fetch
        self._ttl = ttl_seconds
        self._interval = refresh_interval
        # `monotonic` by default, and monotonic *not* `time.time`: both of these clocks
        # measure intervals, and a wall clock that steps backwards mid-rotation would
        # make a cached set look fresh. Injected in tests so the bound is asserted
        # against a schedule the policy asked for rather than against elapsed time.
        self._clock = clock or time.monotonic
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
            cached = self._state.cache
            if cached is None or self._stale(cached):
                return await self._load()
            return cached.keys

    # --- the policy --------------------------------------------------------

    async def _keys_for(self, kid: str) -> jwk.KeySet | None:
        cached = self._state.cache

        if cached is not None and not self._stale(cached):
            # The hot path, and the one that must cost nothing.
            if _publishes(cached.keys, kid):
                return cached.keys
            # Unknown to us, and we looked for it recently enough that looking again
            # would be an unfunded fetch. This is the amplification control.
            if self._may_look_again(kid):
                return self._answer(await self._load(), kid)
            # We hold a usable key set that simply does not name this key, so the answer
            # is a refusal and not another fetch.
            return None

        # Stale, or cold: we have nothing to answer with until a fetch succeeds.
        if self._may_refresh():
            return self._answer(await self._load(), kid)

        # Cold *and* inside the interval, which means the last attempt failed. This is
        # the case that a naive implementation gets wrong: "no cache" looks like a
        # reason to always fetch, so during an outage — when fetching is most expensive
        # — every request aims another one at identity. Fail closed instead, which is
        # both cheaper and more honest: we cannot check the token, so we say so.
        raise SigningKeysUnavailable(f"the signing keys at {self._url} could not be retrieved")

    def _answer(self, keys: jwk.KeySet, kid: str) -> jwk.KeySet | None:
        """The freshly fetched set if it publishes `kid`, else record the miss and say
        no.

        Takes the keys rather than reading the cache, so there is no window in which it
        could be handed a cache some other request has replaced.
        """
        if _publishes(keys, kid):
            return keys
        self._state.unknown[kid] = self._clock()
        return None

    async def _load(self) -> jwk.KeySet:
        """Fetch and replace the cache, returning the new set.

        Never leaves a partial state behind.

        A failure is a `SigningKeysUnavailable` rather than a silently-empty cache: a
        client that kept the last good set would be serving requests against a key set
        identity may have withdrawn, which is the opposite of what this module is for.

        **The interval is claimed before the fetch, not after it.** That ordering is the
        whole amplification control on the failure path, and it is not obvious: recording
        the attempt on success only means that while identity is *down* — which is
        exactly when fetching is most expensive — every request is allowed another one,
        so the bound silently disappears during an outage. Claiming the slot first also
        dedupes concurrent requests, because the claim is synchronous and the await
        happens after it.
        """
        self._state.last_refresh_at = self._clock()
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
        # Replaced on success, never merged: a merge is a set that only ever grows, and
        # a withdrawn key that never leaves.
        self._state.unknown.clear()
        return keys

    # --- the two windows, and the two questions they answer --------------------

    def _stale(self, cached: _Cache) -> bool:
        """Whether `cached` has outlived its TTL.

        Takes the cache rather than reading it, so "does a cache exist" and "how old is
        it" are two separate questions with two separate answers at the call site —
        which is what lets the cold path read as a refusal rather than as a special case
        buried here.
        """
        return self._clock() - cached.fetched_at >= self._ttl

    def _may_look_again(self, kid: str) -> bool:
        """Whether one more fetch is allowed, to look for this specific `kid`.

        Two conditions, and the second is the one that makes the first mean something:
        we must not have looked for *this key id* inside the interval, and we must not
        have fetched anything inside the interval. Either alone would let a flood of
        distinct ids defeat the bound.
        """
        checked_at = self._state.unknown.get(kid)
        if checked_at is not None and self._clock() - checked_at < self._interval:
            return False
        return self._may_refresh()

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

    def __repr__(self) -> str:
        cached = self._state.cache
        kids = (
            [key.get("kid") for key in cached.keys.as_dict(private=False)["keys"]] if cached else []
        )
        return f"JwksClient(url={self._url!r}, kids={kids})"  # a kid is a public id, never a key


def _publishes(keys: jwk.KeySet, kid: str) -> bool:
    """Whether `keys` names `kid`.

    `get_by_kid` rather than a hand-rolled scan: it is the library's own selection and
    it is what `decode` will consult, so "does this set publish the key" and "will the
    signature check find it" cannot be two different answers. It raises on a miss, which
    is the miss.
    """
    try:
        keys.get_by_kid(kid)
    except JoseError:
        return False
    return True


__all__ = [
    "DEFAULT_REFRESH_INTERVAL_SECONDS",
    "DEFAULT_TTL_SECONDS",
    "JWKS_PATH",
    "FetchKeys",
    "JwksClient",
]
