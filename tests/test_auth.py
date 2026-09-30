"""The bearer check: every rule in core's `docs/openapi-conventions.md` §Auth.

This is the file a reviewer reads first, so it is organised the way the contract is
rather than the way the code happens to be laid out. The table-driven sections are the
easy half; **the three at the end are the ones that catch real bugs**, and each is
called out here because they are the tests a verification-only implementation fails:

1. `test_a_validly_signed_token_with_no_scope_is_refused` — the one that matters most.
   An implementation that checks the signature and stops passes every other test in
   this file and still authorises anybody holding a stale token.
2. `test_a_forged_token_is_a_401_and_not_a_500` — an unhandled verification exception
   is itself an information leak about the server's internals.
3. `test_the_token_appears_nowhere_in_a_span_a_log_or_a_response` — muse is the service
   where this leak is worst, because a token in a retained log is a credential in a
   searchable store.

Two more properties are asserted that are easy to leave untested and expensive to get
wrong, so they have their own sections rather than being folded in above: the **bounded
refresh** (the amplification primitive a hand-rolled JWKS client always grows) and
**rotation** in both directions.

Every token here is signed by an ephemeral key generated at process start. No private
key is committed, in any format, for any reason.
"""

from __future__ import annotations

import logging
import time

import pytest

from muse.auth import (
    ALGORITHM,
    REQUIRED_CLAIMS,
    SCOPE,
    SCOPES_CLAIM,
    Principal,
    bearer_token,
    jwks_url_for,
)
from muse.errors import (
    Unauthenticated,
)
from muse.jwks import DEFAULT_REFRESH_INTERVAL_SECONDS, DEFAULT_TTL_SECONDS, JwksClient
from muse.providers import Price, ProviderRegistry
from muse.providers.fake import FakeProvider
from muse.routes import routes_from_yaml

from .conftest import asgi_client
from .support.fake_database import FakeDatabase
from .support.jwks import (
    ACCOUNT,
    AUDIENCE,
    ISSUER,
    Identity,
    SigningKey,
    claims,
    symmetric,
    tampered,
    token,
    unsigned,
)
from .support.test_app import FakeClock, auth_for, build_test_app
from .support.tracing import recording_telemetry, rendered

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MODEL = "gpt-4o-mini"
BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}

#: A string that must not survive anywhere. Distinct from `test_trace_propagation`'s
#: prompt canary on purpose: that one watches content, this one watches the *credential*.
TOKEN_CANARY = "CANARY-TOKEN-4b7e2a91-NEVER-LOG"


def app_for(identity: Identity, **auth_kwargs):
    """An app verifying against `identity`, with one provider that always works."""
    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    return build_test_app(
        registry=registry,
        database=FakeDatabase(),
        table=routes_from_yaml(
            "version: 1\nroutes:\n  - model: fast\n    candidates:\n      - provider: openai\n"
        ),
        auth=auth_for(identity, **auth_kwargs),
    )


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


async def call(app, headers: dict[str, str]):
    async with asgi_client(app) as client:
        return await client.post("/v1/route", json=BODY, headers=headers)


def problem_of(response) -> dict:
    """The problem body, asserted to be the right media type first.

    core: every non-2xx is `application/problem+json`. A 401 delivered as
    `application/json` produces a client that cannot parse our own errors, which is the
    whole reason the envelope exists.
    """
    assert response.headers["content-type"].startswith("application/problem+json"), (
        response.headers["content-type"]
    )
    return response.json()


# --- the header -------------------------------------------------------------


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Basic dXNlcjpwYXNz",
        "Bearer a b",
        "token",
    ],
)
def test_a_header_that_is_not_one_bearer_token_is_no_credential(header) -> None:
    """RFC 6750 §2.1: the scheme is case-insensitive, the token is everything after it,
    and there is exactly one token.

    `Bearer a b` is the interesting one. A `partition(" ")` implementation — which is
    what this one used to be — reads that as the token `a` and silently discards `b`.
    Anchoring the pattern is what makes it a refusal instead.
    """
    assert bearer_token(header) is None


def test_extra_whitespace_after_the_scheme_is_tolerated() -> None:
    """Not the same as two tokens: `Bearer  <token>` is one token, and RFC 6750's
    `1*SP` is a run of spaces. Read strictly it would be a refusal nobody could debug
    from the error, since the header looks correct in every log."""
    assert bearer_token("Bearer  abc.def.ghi") == "abc.def.ghi"


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "BeArEr"])
def test_the_scheme_is_case_insensitive(scheme: str) -> None:
    assert bearer_token(f"{scheme} abc.def.ghi") == "abc.def.ghi"


def test_surrounding_whitespace_is_trimmed() -> None:
    """httpx and a proxy may both add it, and it is not the caller trying anything."""
    assert bearer_token("  Bearer abc.def.ghi  ") == "abc.def.ghi"


async def test_no_authorization_header_is_a_401_and_not_a_500() -> None:
    """The absence of a credential is an answer, not a crash."""
    response = await call(app_for(Identity()), {})
    assert response.status_code == 401
    assert problem_of(response)["code"] == "unauthorized"


# --- one test per check, each failing only that check -----------------------


async def test_a_fully_valid_token_is_served() -> None:
    """The positive case, so the refusals below cannot all be passing for one reason."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity)))
    assert response.status_code == 200


async def test_a_token_from_another_issuer_is_refused() -> None:
    """`iss`. A token from a different issuer is a different service's credential."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, iss="https://evil.test")))
    assert response.status_code == 401
    assert "iss" in problem_of(response)["detail"]


async def test_a_token_addressed_to_another_service_is_refused() -> None:
    """`aud`. A token minted for a sibling is not a token for muse, and accepting it
    would let a credential scoped to `guard` spend money here."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, aud="guard")))
    assert response.status_code == 401
    assert "aud" in problem_of(response)["detail"]


async def test_an_expired_token_is_refused() -> None:
    """`exp`. Checked against the wall clock, with no leeway."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, exp=int(time.time()) - 10)))
    assert response.status_code == 401
    assert problem_of(response)["detail"] == "token has expired"


async def test_a_token_that_is_not_yet_valid_is_refused() -> None:
    """`nbf`. core lists it as refused by `guard`, and a token minted for a clock that
    disagrees with ours should not be accepted on the strength of that disagreement."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, nbf=int(time.time()) + 600)))
    assert response.status_code == 401


@pytest.mark.parametrize("missing", REQUIRED_CLAIMS)
async def test_a_required_claim_that_is_absent_is_refused(missing: str) -> None:
    """Every claim core lists as required, one at a time.

    Parametrised over `REQUIRED_CLAIMS` rather than written out, so adding a claim to
    core's list and to `muse.auth` brings its test with it. A required claim that is
    only *usually* checked is a required claim that is not checked.

    The token is **signed without the claim**, not tampered afterwards: a tampered token
    would fail the signature check first and prove nothing about the claim check.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, **{missing: None})))
    assert response.status_code == 401
    assert missing in problem_of(response)["detail"]


async def test_a_token_signed_by_an_unpublished_key_is_refused() -> None:
    """The `kid` is not one identity publishes. The commonest forgery there is."""
    published = Identity()
    forged = SigningKey("attacker")
    response = await call(app_for(published), bearer(token(forged)))
    assert response.status_code == 401
    assert "does not publish" in problem_of(response)["detail"]


# --- the algorithm is pinned ------------------------------------------------


async def test_an_unsigned_token_is_refused() -> None:
    """`alg: none` — a compact JWS with no signature at all.

    Refused on the **protected header**, before any key is fetched. That ordering is the
    property: a forged token must cost no network call and must never aim traffic at
    identity.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(unsigned(kid=identity.keys[0].kid)))
    assert response.status_code == 401
    assert "not accepted" in problem_of(response)["detail"]
    assert identity.fetches == [], "a forged token reached the network"


async def test_a_symmetrically_signed_token_is_refused() -> None:
    """HS256. The classic algorithm-confusion attack: an attacker who cannot produce an
    RSA signature signs the same claims with HS256, hoping a verifier that reads `alg`
    from the token treats the public modulus as a shared secret."""
    identity = Identity()
    response = await call(app_for(identity), bearer(symmetric(kid=identity.keys[0].kid)))
    assert response.status_code == 401
    assert "not accepted" in problem_of(response)["detail"]


async def test_only_rs256_is_accepted_and_the_algorithm_is_one_constant() -> None:
    """The pin, stated as data.

    `ALGORITHM` is passed to the library's decode rather than checked afterwards, so an
    algorithm outside it is unrepresentable rather than merely unlisted. Asserting it is
    a single named constant is what stops a second implementation appearing alongside
    the first — which is how a service ends up accepting two algorithms and having
    forgotten why it widened.

    ES256 is deliberately absent: core's conventions allow it, `guard` does not take
    it, and widening belongs with the moment identity publishes ES256 keys. Recorded in
    `cafaye.yml`.
    """
    assert ALGORITHM == "RS256"
    assert ALGORITHM not in ("ES256", "HS256", "none", "RS384")


async def test_a_token_with_an_edited_payload_is_refused() -> None:
    """The signature is checked. Editing `sub` after signing must not verify.

    This is what an attacker editing their own scope produces, and it is the reason
    claim checks are not a substitute for a signature check.
    """
    identity = Identity()
    forged = tampered(token(identity), scopes="", sub="attacker")
    response = await call(app_for(identity), bearer(forged))
    assert response.status_code == 401


@pytest.mark.parametrize(
    "garbage",
    [
        "not-a-jwt",
        "a.b",
        "a.b.c",
        "a.b.c.d",
        "....",
        "%%%.%%%.%%%",
        "e30",  # a bare JSON segment and nothing else
    ],
)
async def test_a_token_that_is_not_a_compact_jws_is_a_401_and_not_a_500(garbage: str) -> None:
    """The parsing path, and the reason the except clause in `_header` catches a whole
    family of joserfc errors rather than one.

    Each of these raises a *different* exception from the library — a `DecodeError` on
    the segments, a `MissingAlgorithmError` on a header with no `alg`, a JSON error on a
    segment that is not JSON. An unhandled one escapes as a 500, which tells the caller
    something about the server's internals and is exactly what a 401 must not do.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(garbage))
    assert response.status_code == 401
    assert problem_of(response)["detail"] == "token is malformed"
    assert identity.fetches == [], "a malformed token reached the network"


async def test_a_token_naming_no_key_is_refused() -> None:
    """No `kid` in the header. An unnamed token cannot be checked against anything.

    The header is re-encoded without it rather than the token being rebuilt, so what is
    sent is a well-formed compact JWS whose signature simply will not match. That is
    deliberate: the *header* check has to fire before the signature check, or this test
    would pass for the wrong reason and the ordering would be free to change.
    """
    from .support.jwks import claims

    identity = Identity()
    header, payload, signature = identity.keys[0].sign(claims()).split(".")
    # Two shapes, both landing here: a well-formed header with the `kid` taken out, and
    # a hand-built one that never had a payload at all.
    for candidate in (
        f"{_without_kid(header)}.{payload}.{signature}",
        f"{_without_kid(header)}..x",
    ):
        response = await call(app_for(identity), bearer(candidate))
        assert response.status_code == 401
        assert "names no key" in problem_of(response)["detail"]


def _without_kid(header_segment: str) -> str:
    """A protected header with `kid` removed and everything else intact."""
    import base64
    import json

    header = json.loads(_unb64url(header_segment))
    header.pop("kid", None)
    return base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()


def _unb64url(segment: str) -> bytes:
    import base64

    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


# --- tenancy ----------------------------------------------------------------


async def test_a_token_with_no_account_id_is_refused() -> None:
    """muse bills and meters per tenant, so a token with no tenant is a request whose
    cost cannot be attributed to anyone.

    The strict behaviour, deliberately, while the fleet-wide question is open. The
    lenient alternative — defaulting to `sub` — makes a user id a tenancy key, and a bug
    in one service that keys on `sub` becomes a cross-tenant read rather than a 403.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, account_id="")))
    assert response.status_code == 401
    assert "account" in problem_of(response)["detail"]


async def test_a_non_string_account_id_is_refused_rather_than_ignored() -> None:
    """Deliberately unlike `guard`, which ignores a non-string `account_id` because its
    use is a rate-limit key.

    That reasoning does not transfer to a billing boundary: a malformed tenancy claim
    is refused, because the alternative is that some future caller reads the string
    `"None"` and queries for an account that does not exist — or, worse, one that does.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, account_id=12345)))
    assert response.status_code == 401
    assert "account" in problem_of(response)["detail"]


# --- the capability ---------------------------------------------------------


async def test_a_validly_signed_token_with_no_scope_is_refused() -> None:
    """**The test that matters most.**

    A perfectly good signature, every claim core requires, an `account_id`, and no
    capability at all. It must be a 403.

    A verification-only implementation passes every other test in this file and still
    authorises anybody holding a stale token, because "the signature is valid" and "the
    caller may do this" are different questions. core's conventions are explicit:
    "Authorization: `scopes` for capability". The whole point of the packet is that a
    valid signature is not an authorization.
    """
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, scopes="")))
    assert response.status_code == 403
    assert problem_of(response)["code"] == "forbidden"


async def test_a_token_with_the_wrong_capability_is_refused() -> None:
    """Some capability is not *this* capability. The gate is exact-match, never a prefix
    match: `completions` is not `completions:write`, and a comparison that accepted the
    first because it starts with the second hands out a scope nobody wrote down."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, scopes="completions:read")))
    assert response.status_code == 403


async def test_a_403_names_the_missing_scope_and_not_the_held_ones() -> None:
    """The scope that is missing is *this service's own configuration* and is safe to
    name. The scopes the caller holds are not echoed back — a 403 that listed them would
    tell an attacker which of a guessed set is real."""
    identity = Identity()
    response = await call(
        app_for(identity),
        bearer(token(identity, scopes="accounts:write billing:read")),
    )
    detail = problem_of(response)["detail"]
    assert SCOPE in detail
    assert "accounts:write" not in detail
    assert "billing:read" not in detail


async def test_a_capability_the_token_holds_alongside_others_is_accepted() -> None:
    """The gate is membership, not equality. A token with three scopes and the right one
    is a token that may do this."""
    identity = Identity()
    response = await call(
        app_for(identity), bearer(token(identity, scopes=f"accounts:write {SCOPE} billing:read"))
    )
    assert response.status_code == 200


# --- the claim-name duality, and why it refuses ------------------------------


async def test_a_token_with_only_the_scopes_claim_is_accepted() -> None:
    """core's name (`openapi-conventions.md:134`)."""
    identity = Identity()
    assert SCOPES_CLAIM == "scopes"
    response = await call(app_for(identity), bearer(token(identity, scopes=SCOPE)))
    assert response.status_code == 200


async def test_a_token_with_only_the_scope_claim_is_accepted() -> None:
    """`guard`'s name (`jwt.ts:32`). identity mints both, byte-identical, with a test
    holding them equal, precisely so the ruling can land without breaking a token."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, scope=SCOPE, scopes=None)))
    assert response.status_code == 200


async def test_a_token_carrying_both_names_where_they_agree_is_accepted() -> None:
    """What identity actually mints today."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, scopes=SCOPE)))
    assert response.status_code == 200


async def test_a_token_carrying_both_names_where_they_disagree_is_refused() -> None:
    """**Not merged. Not preferred. Not unioned.**

    A token whose two authorisation claims contradict each other is a token this service
    does not understand, and guessing which one the issuer meant is how an escalation
    about a claim *name* becomes a cross-tenant read: a token with `scopes:
    "accounts:read"` and `scope: "completions:write"` is refused here, and a merge or a
    preference would admit it.

    This is the reason the dual-name problem is survivable rather than dangerous. The
    claim *name* is open; the *values* must not disagree until it is settled.
    """
    identity = Identity()
    contradictory = token(identity, scopes="", scope=SCOPE)
    response = await call(app_for(identity), bearer(contradictory))
    assert response.status_code == 401
    assert "disagree" in problem_of(response)["detail"]


async def test_a_scope_claim_that_is_not_a_string_is_refused() -> None:
    """An array claim. `guard` splits on whitespace and refuses a non-string outright;
    an array is a token guard itself would not parse, so accepting it here would make
    the two services disagree about what a valid token is."""
    identity = Identity()
    response = await call(app_for(identity), bearer(token(identity, scopes=["completions:write"])))
    assert response.status_code == 401


async def test_an_absent_scope_claim_is_the_empty_set_and_never_everything() -> None:
    """The two dangerous shapes of "no scope": absent, and blank. Both are an empty
    capability set — never an empty check, which is a gate with no gate."""
    identity = Identity()
    for value in (None, "", "   "):
        response = await call(app_for(identity), bearer(token(identity, scopes=value)))
        assert response.status_code == 403, f"scopes={value!r} was not refused"


# --- identity down: a 503, and never a 401 ----------------------------------


async def test_an_unreachable_key_set_is_a_503_and_not_a_401() -> None:
    """**Not 401, and certainly not 200.**

    A 401 tells the caller their credential is bad, and the problem is that we could not
    *check* it — so a 401 sends a caller with a perfectly good token to re-authenticate
    against a healthy identity and then retry forever. muse is up and identity is not,
    which is the one case where retrying the same request is the right response.
    """
    identity = Identity()
    identity.fail_with(503)
    response = await call(app_for(identity), bearer(token(identity)))
    assert response.status_code == 503
    assert problem_of(response)["code"] == "unavailable"


async def test_a_key_set_that_is_not_a_key_set_is_a_503() -> None:
    """An endpoint answering 200 with a login page. Refused rather than parsed, because
    a proxy that swallows the request would otherwise hand back something that looks
    like an empty key set and refuse every token for the TTL."""
    identity = Identity()
    identity.serve_garbage()
    response = await call(app_for(identity), bearer(token(identity)))
    assert response.status_code == 503


async def test_muse_does_not_serve_the_request_when_identity_is_down() -> None:
    """The operational contract, as an assertion.

    **muse does not serve unauthenticated traffic when identity is down.** The tempting
    alternative is to fall back to "the key set was cached, let us through" — and that
    is exactly the behaviour that makes a forged token's validity depend on whether the
    attacker happened to choose a moment when identity was unreachable.

    The cache is replaced on refresh and *not kept* on failure, so there is no warm
    fallback to fall into by accident. The TTL is advanced past first so the request
    genuinely has no usable cache, rather than passing because it happened to be fresh.
    """
    identity = Identity()
    clock = FakeClock()
    app = app_for(identity, cache_clock=clock)

    # Warm it, so this is specifically about losing it rather than about never having it.
    assert (await call(app, bearer(token(identity)))).status_code == 200

    identity.fail_with(503)
    clock.advance(DEFAULT_TTL_SECONDS * 2)  # past the TTL: the cache is not usable
    response = await call(app, bearer(token(identity)))
    assert response.status_code == 503
    assert problem_of(response)["code"] == "unavailable"


# --- the bounded refresh ----------------------------------------------------


async def test_a_flood_of_unknown_kids_does_not_become_a_flood_of_fetches() -> None:
    """**The amplification primitive, and the property this packet most needs.**

    Anyone who can send a request can send one with a random `kid`. The naive
    implementation — "refresh on an unknown `kid`" — turns that into one JWKS fetch per
    request, aimed at our own identity service, by an unauthenticated caller.

    So the assertion is not "the fetch count is small" but the shape of the claim: **the
    fetch count does not track the request count.** Five hundred unknown `kid`s must not
    produce five hundred fetches.

    One key pair, five hundred `kid`s: the attack varies the `kid`, not the key, and
    minting 500 RSA-2048 pairs to simulate it would cost a minute of CPU to prove
    something about a counter.
    """
    identity = Identity()
    app = app_for(identity)
    stranger = SigningKey("stranger")

    requests = 500
    async with asgi_client(app) as client:
        for index in range(requests):
            forged = stranger.sign(claims(), header={"kid": f"unknown-{index}"})
            response = await client.post("/v1/route", json=BODY, headers=bearer(forged))
            assert response.status_code == 401

    fetches = len(identity.fetches)
    assert fetches <= 2, (
        f"{requests} requests with unknown kids produced {fetches} JWKS fetches; the "
        "refresh is unbounded and is an amplification primitive aimed at identity"
    )


async def test_the_first_unknown_kid_does_refresh_the_key_set() -> None:
    """The bound must not have been bought by never refreshing.

    Without this, a cache that simply ignored every unknown `kid` would pass the
    amplification test perfectly and would never notice a rotation. The first unknown
    `kid` is always worth one fetch; it is the second, third and four hundredth that
    are not.
    """
    identity = Identity()
    await call(app_for(identity), bearer(token(SigningKey("stranger"))))
    assert len(identity.fetches) == 1


async def test_a_failed_refresh_is_also_bounded() -> None:
    """An attacker who can make identity unreachable must not then get one fetch per
    request *while it is unreachable*.

    The interval is recorded on every attempt including a failed one, which is why this
    is a separate test from the success path — a bound that only counts successes is a
    bound an outage removes.
    """
    identity = Identity()
    identity.fail_with(500)
    app = app_for(identity)
    stranger = SigningKey("stranger")
    async with asgi_client(app) as client:
        for index in range(50):
            forged = stranger.sign(claims(), header={"kid": f"unknown-{index}"})
            response = await client.post("/v1/route", json=BODY, headers=bearer(forged))
            assert response.status_code == 503
    assert len(identity.fetches) <= 2, (
        "a failing key set is refetched per request; identity being down is the moment "
        "a fetch storm is most expensive"
    )


async def test_a_cached_key_set_is_reused_rather_than_refetched() -> None:
    """The availability half of core's rule: no per-request call to identity.

    Ten valid requests, one fetch. This is the assertion that the cache exists at all,
    and it is here rather than in `test_jwks.py` because "one fetch for ten requests"
    is only meaningful through the whole path.
    """
    identity = Identity()
    app = app_for(identity)
    for _ in range(10):
        assert (await call(app, bearer(token(identity)))).status_code == 200
    assert len(identity.fetches) == 1


# --- rotation ---------------------------------------------------------------


async def test_a_token_signed_by_a_newly_published_key_is_accepted() -> None:
    """Rotation in the direction that matters.

    A token signed by a key identity published *after* this process cached the old set
    must still be served, or a rotation takes the service down for the length of the TTL
    — which is the reason a bounded cache is risky and an unbounded one is a denial of
    service.

    The clock is advanced past the refresh interval rather than slept on, so the
    assertion is about the policy's schedule and not about how fast the machine is
    (AGENTS.md rule 14). It also states the cost honestly: a rotation is noticed within
    `refresh_interval`, not instantly, and that trade is the price of the bound.
    """
    identity = Identity()
    clock = FakeClock()
    app = app_for(identity, cache_clock=clock)

    first = SigningKey("k1")
    second = SigningKey("k2")
    identity.publish(first)
    assert (await call(app, bearer(token(first)))).status_code == 200
    assert len(identity.fetches) == 1

    identity.publish(first, second)
    # Inside the interval: the bound holds the line and the new key is refused.
    assert (await call(app, bearer(token(second)))).status_code == 401

    clock.advance(DEFAULT_REFRESH_INTERVAL_SECONDS)
    response = await call(app, bearer(token(second)))
    assert response.status_code == 200
    assert len(identity.fetches) == 2


async def test_a_key_removed_from_the_published_set_stops_being_accepted() -> None:
    """Rotation in the other direction, and the reason the cache is *replaced* rather
    than merged.

    A cache that only ever grows is a cache where a revoked signing key keeps working
    until the process restarts, which for a key rotation means "withdrawn" never takes
    effect at all.
    """
    identity = Identity()
    old = SigningKey("k-old")
    new = SigningKey("k-new")
    identity.publish(old, new)
    app = app_for(identity)

    # A client holding a token from the old key, with the old key already withdrawn.
    identity.publish(new)
    response = await call(app, bearer(token(old)))
    assert response.status_code == 401
    assert "does not publish" in problem_of(response)["detail"]


async def test_the_cache_is_replaced_on_a_successful_refresh_and_not_merged() -> None:
    """Stated as a fact about the cached document rather than inferred from a status.

    The 401 above already proves the token stops verifying. This proves *why*: the
    fetched set contains the new key and not the old one, so the cache was replaced.

    `JwksClient.keys()` rather than the private cache: it is the public accessor, and a
    test that reached into `_state` would be asserting on the implementation rather than
    on the property.
    """
    identity = Identity()
    clock = FakeClock()
    old, new = SigningKey("k-old"), SigningKey("k-new")
    identity.publish(old)
    app = app_for(identity, cache_clock=clock)
    await call(app, bearer(token(old)))

    identity.publish(new)
    clock.advance(DEFAULT_REFRESH_INTERVAL_SECONDS)
    await call(app, bearer(token(new)))

    published = [
        key["kid"]
        for key in (await app.state.container.auth._jwks.keys()).as_dict(private=False)["keys"]
    ]
    assert published == ["k-new"], "the cached set accumulated keys instead of replacing"


# --- nothing about the token reaches anywhere -------------------------------


async def test_the_token_appears_nowhere_in_a_span_a_log_or_a_response(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """**The credential, by every route it could leave by.**

    A unique marker is placed inside the token's *claims* — so it is inside the
    signature-verified payload — and then the response body, the response headers, the
    rendered span payload and every captured log record are searched for it.

    muse is the service where this leak is worst: a token in a retained log is a
    credential in a searchable store, and the redaction boundary that governs prompt
    content governs the credential too. This asserts the *absence* end of that boundary
    rather than trusting that no call site happens to record one.
    """
    identity = Identity()
    telemetry, exporter = recording_telemetry()
    from .support.test_app import auth_for

    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = build_test_app(
        registry=registry,
        database=FakeDatabase(),
        table=routes_from_yaml(
            "version: 1\nroutes:\n  - model: fast\n    candidates:\n      - provider: openai\n"
        ),
        telemetry=telemetry,
        auth=auth_for(identity),
    )

    marked = token(
        identity,
        sub=TOKEN_CANARY,
        jti=TOKEN_CANARY,
        account_id=TOKEN_CANARY,
        scopes=f"{SCOPE} {TOKEN_CANARY}",
    )

    with caplog.at_level(logging.DEBUG):
        served = await call(app, bearer(marked))
        # And on the failure paths, where an error message is the natural leak.
        refused = await call(app, bearer(tampered(marked, sub="attacker")))
        unsigned_response = await call(app, bearer(unsigned(payload={"x": TOKEN_CANARY})))

    assert served.status_code == 200
    assert refused.status_code == 401
    assert unsigned_response.status_code == 401

    # The canary really is inside the token that was sent, so the absences below mean
    # something. "The canary is absent" is also true of a request that never happened.
    # Decoded, because a compact JWS base64-encodes its payload — asserting against the
    # wire form would be asserting against a string the canary is deliberately not in.
    assert TOKEN_CANARY in _unb64url(marked.split(".")[1]).decode()

    spans = rendered(exporter)
    assert TOKEN_CANARY not in spans, "the token's contents reached a span"
    assert TOKEN_CANARY not in served.text, "the token's contents reached the response body"
    assert TOKEN_CANARY not in refused.text
    assert TOKEN_CANARY not in unsigned_response.text
    for name, value in refused.headers.items():
        assert TOKEN_CANARY not in value, f"the token reached the {name} header"
    for record in caplog.records:
        assert TOKEN_CANARY not in record.getMessage(), "the token reached a log record"


async def test_the_whole_token_string_appears_nowhere_either() -> None:
    """The other half, and the stricter one.

    The canary test puts its marker in *claims*, which a selective implementation might
    never read. This asserts the compact JWS itself — the exact bytes a caller sent —
    is absent from the response and the spans. A service that logged `request.headers`
    would pass the canary test and fail this one.
    """
    identity = Identity()
    telemetry, exporter = recording_telemetry()
    from .support.test_app import auth_for

    registry = ProviderRegistry()
    registry.register(FakeProvider(name="openai", price=Price(1000, 2000)))
    app = build_test_app(
        registry=registry,
        database=FakeDatabase(),
        table=routes_from_yaml(
            "version: 1\nroutes:\n  - model: fast\n    candidates:\n      - provider: openai\n"
        ),
        telemetry=telemetry,
        auth=auth_for(identity),
    )

    full = token(identity)
    response = await call(app, bearer(full))
    assert response.status_code == 200

    assert full not in response.text
    assert full not in rendered(exporter)


def test_no_span_attribute_name_could_carry_a_token() -> None:
    """The unit-level statement behind the two tests above.

    There is no attribute name a token or a claim could be assigned to, so those tests
    are not resting on "muse happened not to do it this time". The redaction boundary is
    an allowlist, and this is the property that makes it one.

    `muse_tokens_in` and `muse_tokens_out` are named in the exemption with the reason:
    they are *counts*, and core's redaction schema blesses `llm.tokens_in` by name. A
    count of tokens is not a token, and a boundary that could not say so would be a
    boundary somebody widens at the next 3am.
    """
    from muse.telemetry import ALLOWED_SPAN_ATTRIBUTES

    counts = {"muse_tokens_in", "muse_tokens_out"}
    forbidden = ("jwt", "bearer", "authorization", "credential", "secret", "key")
    suspicious = sorted(
        name
        for name in ALLOWED_SPAN_ATTRIBUTES
        if name not in counts and any(word in name.lower() for word in (*forbidden, "token"))
    )
    assert suspicious == [], f"these attribute names could carry a credential: {suspicious}"


# --- configuration, and the units -------------------------------------------


def test_the_key_set_url_is_derived_from_the_issuer_and_not_configured_separately() -> None:
    """core's documented location. A base URL with a path in it is caught here rather
    than producing a key set nobody is serving."""
    assert jwks_url_for("https://identity.cafaye.com") == (
        "https://identity.cafaye.com/.well-known/jwks.json"
    )
    assert jwks_url_for("https://identity.cafaye.com/") == (
        "https://identity.cafaye.com/.well-known/jwks.json"
    )


@pytest.mark.parametrize(
    "issuer", ["", "identity.cafaye.com", "ftp://identity.test", "https://id.test/nested"]
)
def test_an_issuer_that_is_not_a_bare_http_origin_is_a_configuration_error(issuer: str) -> None:
    """At construction, so a typo in an environment variable is a startup failure
    rather than a 503 on every request."""
    with pytest.raises(ValueError):
        jwks_url_for(issuer)


def test_a_principal_is_frozen_and_immutable() -> None:
    """One per request, read by the handler. A mutable principal would be mutable
    state shared with whatever else the request touches."""
    principal = Principal(
        subject="usr_01",
        account_id=ACCOUNT,
        scopes=frozenset({SCOPE}),
        token_id="jti_01",
        issuer=ISSUER,
    )
    assert principal.has_scope(SCOPE)
    assert not principal.has_scope("accounts:write")
    assert principal.issuer == ISSUER
    assert principal.account_id == ACCOUNT
    with pytest.raises(AttributeError):
        principal.subject = "someone-else"  # type: ignore[misc]


def test_the_audience_is_a_configuration_value_and_defaults_to_this_service() -> None:
    """`muse`'s own name, the way `guard` uses its own client id. Anything wider would
    accept a token minted for a sibling."""
    from muse.main import DEFAULT_IDENTITY_AUDIENCE, DEFAULT_IDENTITY_ISSUER

    assert DEFAULT_IDENTITY_AUDIENCE == "muse"
    assert DEFAULT_IDENTITY_ISSUER == "https://identity.cafaye.com"


async def test_the_expiry_clock_is_injected_so_expiry_never_needs_a_sleep() -> None:
    """Rule 14, asserted rather than assumed.

    `TokenVerifier`'s notion of now is a parameter, so a token that expired an hour ago
    can be checked against a clock the test chose — no `sleep`, no flake, and the
    assertion is about the policy rather than about elapsed wall time.

    Two verifiers, two clocks, **one token**: the second accepting what the first
    refused is what proves the refusal was the clock and not the signature.
    """
    from muse.auth import TokenVerifier

    identity = Identity()
    now = int(time.time())
    minted = token(identity, exp=now - 600, iat=now - 7200)

    def verifier_at(when: int) -> TokenVerifier:
        return TokenVerifier(
            issuer=ISSUER,
            audience=AUDIENCE,
            jwks=JwksClient(f"{ISSUER}/.well-known/jwks.json", identity.fetch),
            clock=lambda: when,
        )

    with pytest.raises(Unauthenticated, match="expired"):
        await verifier_at(now).verify(minted)

    assert (await verifier_at(now - 1800).verify(minted)).subject == "usr_01"
