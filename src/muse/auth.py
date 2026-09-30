"""Verifying a bearer token: `require_bearer` stops being a presence check.

Every check in core's `docs/openapi-conventions.md` §Auth, and none of them optional:

- `iss`, `aud`, `sub`, `exp`, `iat`, `jti` — present and correct (`REQUIRED_CLAIMS`).
- `account_id` — present, and **required**: muse bills per tenant, so a token with no
  tenant is a request whose cost cannot be attributed (`MissingAccount`).
- The capability the operation needs — `completions:write`, checked exactly.
- The signature, verified against identity's JWKS with the algorithm **pinned**.

**A valid signature is not an authorization.** A correctly signed token carrying no
scope is refused, and this is the property a verification-only implementation passes
every other test for and still fails. `require_scope` is not optional plumbing here.

## The pinned algorithm

`ALGORITHM` is passed to `joserfc`'s decode as `algorithms=[...]`, which is the library
refusing to *represent* anything else. That is strictly stronger than checking a header
after the fact and rejecting it: an implementation that reads `alg` from the token and
then decides has already parsed attacker-controlled input to choose a verifier, and
`alg: none` and HS256-with-the-public-key are both one missing check away. `joserfc`
raises `UnsupportedAlgorithmError` for all three before any key is looked up.

## The claim-name duality, and why it refuses rather than merges

core's conventions name a `scopes` claim; `guard`'s middleware reads a space-separated
`scope` string. Nobody has ruled — it is a public API decision and the manager's to
escalate — and identity is minting **both**, byte-identical, with a test holding them
equal, precisely so the decision can land without breaking an issued token.

So this module accepts either name, and **refuses a token carrying both where they
disagree.** Not a union, not a preference, not a merge: a token whose two authorisation
claims contradict each other is a token this service does not understand, and guessing
which one the issuer meant is how an escalation becomes a cross-tenant read. The claim
*name* is open; the *values* must not disagree until it is settled.

## Nothing about the token reaches a log, a span, or an error

The token is a credential, and a token in a retained log is a credential in a searchable
store. So `TokenRejected` messages name the *check* that failed and never the value, and
no claim is ever recorded on a span — `muse.telemetry.record` is the only path to an
attribute and nothing here calls it with one. `tests/test_auth.py` asserts this with a
unique marker placed inside the token.
"""

from __future__ import annotations

import hmac
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from joserfc import jwk, jws, jwt
from joserfc.errors import JoseError, UnsupportedAlgorithmError

from muse.errors import (
    InsufficientScope,
    MissingAccount,
    Unauthenticated,
)
from muse.jwks import JWKS_PATH, JwksClient

#: The one algorithm this build accepts.
#:
#: core's conventions allow RS256 **and** ES256. RS256 only, and the reason is the same
#: one `guard` gives: the algorithm is a property of the key set identity publishes, not
#: a hint the token carries, and accepting an algorithm no published key uses buys
#: nothing while widening what a forged token can ask for. Widening is a one-line change
#: and belongs with the moment identity publishes ES256 keys — recorded in `cafaye.yml`.
ALGORITHM = "RS256"

#: core's required claims (`openapi-conventions.md:134`), and `account_id` alongside
#: them because muse meters per tenant. `jti` is here for a reason worth stating: it is
#: the claim that makes a token individually revocable, so a service that does not
#: require it is quietly accepting a class of token the issuer believes it can withdraw.
REQUIRED_CLAIMS = ("iss", "aud", "sub", "exp", "iat", "jti", "account_id")

#: The capability `POST /v1/route` requires.
#:
#: The shape is core's (`resource:action`, `openapi-conventions.md:137`) and the
#: *resource* is this service's own surface, read off its route table the way `darkroom`
#: reads `assets:read` and `identity` reads `accounts:write` — not invented. The name
#: itself is provisional: it is a public API decision in the same family as the claim
#: name, and this packet adds no new scope namespace to core's document. It lives in one
#: constant so changing it is a one-line change.
SCOPE = "completions:write"

#: The two claim names, and which is primary.
#:
#: `scopes` is core's and is checked first; `scope` is what `guard` reads and what
#: identity mirrors. See the module docstring for why a token carrying both and
#: disagreeing is refused.
SCOPES_CLAIM = "scopes"
SCOPE_CLAIM = "scope"

#: RFC 6750 §2.1: the scheme is case-insensitive, the token is everything after it, and
#: there is exactly one token. Anchored, so `Bearer a b` is refused rather than read as
#: a token `a` — a header with two tokens is a header nobody meant to send.
BEARER_RE = re.compile(r"^bearer +(\S+)$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Principal:
    """A verified caller.

    Frozen, and built only by `TokenVerifier.verify`, so a `Principal` in hand is a
    statement that a signature verified against identity's published keys and every
    claim above was checked. There is no constructor a caller can reach to make one of
    these from a dict.

    `token_id` is the `jti`, kept because it is the handle an operator revokes by. It is
    deliberately *not* recorded on any span (see the module docstring).
    """

    subject: str
    account_id: str
    scopes: frozenset[str]
    token_id: str
    issuer: str

    def has_scope(self, scope: str) -> bool:
        return scope in self.scopes


class TokenVerifier:
    """Verifies a compact JWS against identity's keys and the claims core requires."""

    def __init__(
        self,
        *,
        issuer: str,
        audience: str,
        jwks: JwksClient,
        clock: Callable[[], float],
        leeway: int = 0,
    ) -> None:
        self._issuer = issuer
        self._audience = audience
        self._jwks = jwks
        self._clock = clock
        self._leeway = leeway

    async def verify(self, token: str) -> Principal:
        """The principal this token names, or raise.

        Raises `Unauthenticated` (401) for anything about the token, and
        `SigningKeysUnavailable` (503) for anything about our ability to check it.
        """
        header = self._header(token)
        keys = await self._jwks.keys_for(header["kid"])
        if keys is None:
            # Named, not valued: the `kid` is attacker-controlled, and a token naming a
            # key identity has never published is the commonest forgery there is.
            raise Unauthenticated("token was signed by a key the issuer does not publish")

        claims = self._decode(token, keys)
        self._check_claims(claims)
        account_id = self._account_of(claims)
        return Principal(
            subject=claims["sub"],
            account_id=account_id,
            scopes=self._scopes_of(claims),
            token_id=claims["jti"],
            issuer=claims["iss"],
        )

    # --- one check per method, so a failing test names the check ------------

    def _header(self, token: str) -> dict[str, str]:
        """The protected header: algorithm pinned, and a `kid` that names a key.

        Read before any key is fetched. Both fields are attacker-controlled, and a
        malformed token must cost no network call and never aim traffic at identity.
        """
        try:
            header = jws.extract_compact(token.encode()).protected
        except (JoseError, ValueError, UnicodeDecodeError, AttributeError) as error:
            # Every joserfc failure here is "this is not a compact JWS I can read" — a
            # `DecodeError` on the segments, or a `MissingAlgorithmError` on a header
            # with no `alg` at all. Both are the caller's token being unusable, and
            # both must be a 401 rather than an exception that escapes as a 500.
            raise Unauthenticated("token is malformed") from error

        if header.get("alg") != ALGORITHM:
            # The value is echoed because it is the whole of what the caller got wrong
            # and it is not a secret: `alg` is a fixed vocabulary, not a value anybody
            # chose to hide.
            raise Unauthenticated(f"token algorithm {header.get('alg')!r} is not accepted")
        kid = header.get("kid")
        if not isinstance(kid, str) or not kid:
            raise Unauthenticated("token names no key")
        return {"kid": kid}

    def _decode(self, token: str, keys: jwk.KeySet) -> dict[str, Any]:
        """The payload, or refuse. The signature is checked here and nowhere else."""
        try:
            return dict(jwt.decode(token, keys, algorithms=[ALGORITHM]).claims)
        except UnsupportedAlgorithmError as error:  # pragma: no cover
            # Unreachable while `_header` holds: it refuses anything but `ALGORITHM`
            # before a key is fetched, so the library's own check never fires. Kept as
            # the second line rather than deleted because it is the one that would catch
            # a regression in `_header`, and a pinned algorithm that became unpinned
            # should still be a 401 rather than a 500.
            raise Unauthenticated("token algorithm is not accepted") from error
        except JoseError as error:
            raise Unauthenticated(f"token is not valid ({type(error).__name__})") from error

    def _check_claims(self, claims: Mapping[str, Any]) -> None:
        """Presence, type and value for every claim core lists as required.

        **Written here rather than delegated to `joserfc`'s `JWTClaimsRegistry`, and
        the reason is the one `guard` gives in `refusalOf`.** A verifier library's
        errors carry its messages, and those messages describe the platform's internals
        — `guard` notes that an expected audience "is this gateway's client id". A
        library-composed message in a `problem+json` body is the same leak wearing a
        nicer hat, and `joserfc`'s `InvalidClaimError.args[0]` is not even the claim
        name; it is the sentence `"invalid_claim: Claim 'sub' must be a StringOrURI
        value"`. So the mapping is ours, one check per rule, and the response says what
        was wrong in muse's vocabulary.

        The library keeps the two jobs it is actually better at: verifying the
        signature, and pinning the algorithm. Those are crypto (see the module
        docstring); a date comparison is not.
        """
        # `account_id` is deliberately absent from this loop. `_account_of` owns its
        # presence, type and emptiness together, so the refusal can say *why* a token
        # with no tenant is refused instead of "some claim is missing" — which is the
        # difference between a caller who can fix it and a caller who cannot.
        for name in REQUIRED_CLAIMS:
            if name != "account_id" and name not in claims:
                raise Unauthenticated(f"token is missing required claims: {name}")

        self._check_issuer(claims)
        self._check_audience(claims)
        self._check_times(claims)
        for name in ("sub", "jti"):
            # `jti` is type-checked for a reason beyond tidiness: it is the handle an
            # operator revokes by, and a number in that field is not a token id any
            # revocation list can carry.
            value = claims[name]
            if not isinstance(value, str) or not value:
                raise Unauthenticated(f"token claim {name!r} is not a non-empty string")

    def _check_issuer(self, claims: Mapping[str, Any]) -> None:
        issuer = claims["iss"]
        if not isinstance(issuer, str) or not issuer:
            raise Unauthenticated("token claim 'iss' is not a string")
        if not hmac.compare_digest(issuer, self._issuer):
            raise Unauthenticated("token issuer is not accepted")

    def _check_audience(self, claims: Mapping[str, Any]) -> None:
        """RFC 7519 §4.1.3: `aud` is a string, or an array of them when there is more
        than one intended recipient. Anything else is a claim this service cannot
        interpret, so it is refused rather than coerced."""
        audience = claims["aud"]
        if isinstance(audience, str):
            candidates = [audience]
        elif isinstance(audience, list) and all(isinstance(one, str) for one in audience):
            candidates = audience
        else:
            raise Unauthenticated("token claim 'aud' is not a string or an array of them")
        if not any(hmac.compare_digest(one, self._audience) for one in candidates):
            raise Unauthenticated("token audience is not accepted")

    def _check_times(self, claims: Mapping[str, Any]) -> None:
        """`exp`, `nbf` and `iat`, all as NumericDate seconds.

        `iat` is checked for presence and type but not compared: a token minted with a
        clock a minute fast is still a token we should serve, and refusing it would make
        clock skew an outage. Its failure mode — a token issued in the future — is
        covered by `nbf`, which is the claim that states it deliberately.
        """
        expiry = _numeric_date(claims["exp"], "exp")
        _numeric_date(claims["iat"], "iat")  # type only — see the docstring

        now = int(self._clock())
        if now >= expiry + self._leeway:
            raise Unauthenticated("token has expired")
        if "nbf" in claims:
            not_before = _numeric_date(claims["nbf"], "nbf")
            if now + self._leeway < not_before:
                raise Unauthenticated("token is not valid yet")

    def _account_of(self, claims: Mapping[str, Any]) -> str:
        """The tenancy claim, or refuse.

        core requires `account_id` for authenticated service traffic and every query is
        scoped by it. muse has no tenancy query yet — it meters into an outbox row keyed
        by nothing — but "not yet" is not a reason to accept a token with no tenant: it
        is the reason to refuse one, because the spend is then unattributable to anyone
        and the alternative (defaulting to `sub`) makes a user id a tenancy key.

        A non-string `account_id` is refused rather than ignored. `guard` ignores it,
        deliberately, because its use is a rate-limit key and "a worse thing to lose than
        a well-formed claim is to gain" — that reasoning is about a different question
        and does not transfer to a billing boundary.
        """
        value = claims.get("account_id")
        if not isinstance(value, str) or not value:
            raise MissingAccount(
                "token carries no account_id; muse meters per tenant, so a request whose "
                "cost cannot be attributed is refused"
            )
        return value

    def _scopes_of(self, claims: Mapping[str, Any]) -> frozenset[str]:
        """The capability set, from whichever claim name the issuer used.

        Both names accepted, because identity mints both. Disagreement refused, because
        a token whose two authorisation claims contradict each other is a token this
        service does not understand — and picking one is how an open question about a
        claim *name* becomes a cross-tenant read.

        An absent claim is the **empty set, never everything**: a gate that treats "no
        scopes" as "all scopes" is a gate with no gate.
        """
        present = [(name, claims[name]) for name in (SCOPES_CLAIM, SCOPE_CLAIM) if name in claims]
        if not present:
            return frozenset()

        parsed: list[frozenset[str]] = []
        for name, value in present:
            if not isinstance(value, str):
                # Not ignored-and-carried-on: the shape is a space-separated string in
                # both readings, so a non-string is a claim muse cannot interpret.
                raise Unauthenticated(f"token claim {name!r} is not a space-separated string")
            parsed.append(_split_scopes(value))

        if len(parsed) > 1 and parsed[0] != parsed[1]:
            raise Unauthenticated(
                f"token claims {SCOPES_CLAIM!r} and {SCOPE_CLAIM!r} disagree, and the "
                "issuer has not been asked to mint only one"
            )
        return parsed[0]


def _split_scopes(value: str) -> frozenset[str]:
    """Whitespace-separated scopes. Empty strings and runs of spaces are not scopes."""
    return frozenset(part for part in value.split() if part)


def _numeric_date(value: Any, name: str) -> int:
    """A NumericDate, or refuse.

    `bool` is excluded explicitly: it is an `int` in Python, so a token with
    `"exp": true` would otherwise verify as expiring in 1970+1 second — refused as
    expired, which is right by accident, and right by accident is not a control.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        raise Unauthenticated(f"token claim {name!r} is not a NumericDate")
    return value


def require_scope(principal: Principal, scope: str) -> None:
    """Raise `InsufficientScope` unless the principal holds `scope`.

    The scope that is missing is *this service's own configuration* and is safe to name.
    The scopes the caller does hold are not echoed back — a 403 that listed them would
    tell an attacker which of a guessed set is real.
    """
    if not principal.has_scope(scope):
        raise InsufficientScope(f"token is missing the {scope} scope")


def bearer_token(header: str | None) -> str | None:
    """The credential from an `Authorization` header, or `None`.

    RFC 6750 §2.1, anchored. `None` for both "no header" and "not a bearer header",
    because the action is the same and a caller learns nothing from the difference.
    """
    if not header:
        return None
    match = BEARER_RE.match(header.strip())
    return match[1] if match else None


def jwks_url_for(issuer: str) -> str:
    """The key-set URL for an issuer origin.

    The path is appended here rather than configured separately, so an issuer with a
    path in it is caught at construction instead of producing a key set nobody serves.
    """
    parsed = urlparse(issuer)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError(f"issuer must be an absolute http(s) URL, got {issuer!r}")
    if parsed.path not in {"", "/"}:
        raise ValueError(f"issuer must have no path, got {issuer!r}")
    return f"{parsed.scheme}://{parsed.netloc}{JWKS_PATH}"


__all__ = [
    "ALGORITHM",
    "REQUIRED_CLAIMS",
    "SCOPE",
    "SCOPES_CLAIM",
    "SCOPE_CLAIM",
    "Principal",
    "TokenVerifier",
    "bearer_token",
    "jwks_url_for",
    "require_scope",
]
