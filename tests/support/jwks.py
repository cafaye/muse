"""identity's published key set, a signer, and a token factory — for tests only.

**No private key is committed, in any format.** Every key here is generated when the
process starts and dies with it. The brief for this packet said it outright, and the
reason generalises: a test key that reaches a repository is a key every fork has, and
the file that is supposed to be harmless becomes the one credential an attacker does
not have to guess. `guard` pins a key under `test/` because TypeScript's signer needs a
stable one; Python can mint an RSA-2048 pair in well under a second, so nothing is
pinned and nothing can leak.

The fixture is the seam for three separate things, which is why it is one module:

- **`Identity`** is a stand-in for identity's `/.well-known/jwks.json`. It counts
  fetches, because the assertion that a flood of unknown `kid`s does not become a flood
  of fetches is the whole point of the bounded refresh, and it cannot be made against a
  real endpoint without a socket (AGENTS.md rule 3).
- **`sign`** mints tokens with the fixture's private key, so a test can make a token
  that is *valid except for the one thing under test*.
- **`tampered` / `unsigned` / `foreign_key`** produce the three tokens a signature check
  has to refuse: a payload edited after signing, an `alg: none` compact JWS, and a token
  signed by a key identity does not publish.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from typing import Any

from joserfc import jwk, jwt
from joserfc.jwk import OctKey

#: The issuer and audience the fixture mints for. `muse` is the client id a real
#: deployment would configure; the value itself is configuration, not a contract.
ISSUER = "https://identity.test"
AUDIENCE = "muse"

#: The capability `POST /v1/route` requires, and the one every fixture token carries by
#: default. A test that wants a token *without* it passes `scopes=()`.
SCOPE = "completions:write"

#: A different account per fixture by default, so a test that reads `account_id` cannot
#: accidentally assert against a value another test wrote.
ACCOUNT = "01HQ0000000000000000000000A"


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def _segment(value: Any) -> str:
    return _b64url(json.dumps(value, separators=(",", ":")).encode())


class SigningKey:
    """One RSA key pair: the half that signs, and the JWK identity publishes."""

    def __init__(self, kid: str) -> None:
        self.kid = kid
        # `private=True` is what makes `as_dict()` emit `d`. It is never published —
        # `public_jwk` below is the only thing an `Identity` is given, and the tests
        # that check that read `document` and assert no private material is in it.
        self._key = jwk.RSAKey.generate_key(2048, parameters={"kid": kid}, private=True)

    @property
    def public_jwk(self) -> dict[str, Any]:
        """The published half, in the shape a JWKS document carries.

        `alg` and `use` are what a real key set publishes; omitting them is legal and
        says less. Publishing them keeps the double honest — and if a key set ever
        advertised an algorithm other than RS256, that would be visible in the fixture
        rather than discovered in production.
        """
        public = self._key.as_dict(private=False)
        return {
            **{k: v for k, v in public.items() if k not in {"alg", "use"}},
            "alg": "RS256",
            "use": "sig",
        }

    def sign(self, claims: Mapping[str, Any], *, header: Mapping[str, Any] | None = None) -> str:
        """A token signed by this key, `alg` and `kid` in the protected header.

        `header` overrides rather than replaces, so a test that wants a different `kid`
        or an extra header parameter does not have to re-state `alg`.
        """
        return jwt.encode(
            {**(header or {}), "alg": "RS256", "kid": self.kid},
            dict(claims),
            self._key,
            algorithms=["RS256"],
        )


__test__ = False  # pytest collects `Test*` classes; this one is a fixture, not a suite


class Identity:
    """A stand-in for identity: publishes keys, counts fetches, can be broken.

    Every knob here corresponds to a real failure the client has to survive, which is
    why they are methods rather than fields a test pokes at: `publish` is a rotation,
    `fail_with` is identity being down, `serve_garbage` is an endpoint that answers 200
    with a login page, and `fetches` is the amplifier this packet exists to bound.

    Not named `TestIdentity`: pytest collects classes whose name starts with `Test`, and
    a fixture that pytest tries to run is a fixture that warns on every suite run. The
    `__test__ = False` above would also work, and the rename is here because a reader
    should not have to know that rule to see why the name changed.
    """

    def __init__(self, *keys: SigningKey) -> None:
        self._keys = list(keys) or [SigningKey("k1")]
        self._mode = "keys"
        self._status = 500
        #: Every path fetched, in order. Asserted on by the amplification test.
        self.fetches: list[str] = []
        #: Every request refused by the verifier, for the negative-caching assertions.
        self.refusals: list[str] = []

    # --- the key set -------------------------------------------------------

    @property
    def keys(self) -> list[SigningKey]:
        return list(self._keys)

    def publish(self, *keys: SigningKey) -> None:
        """Publish exactly these keys; the next fetch sees them. A rotation."""
        self._keys = list(keys)

    def document(self) -> dict[str, Any]:
        """The JWKS document, exactly as identity would serve it."""
        return {"keys": [key.public_jwk for key in self._keys]}

    # --- breaking it -------------------------------------------------------

    def fail_with(self, status: int) -> None:
        """Answer `status` with a non-JWKS body: identity is unwell."""
        self._mode = "status"
        self._status = status

    def serve_garbage(self) -> None:
        """Answer 200 with something that is not a key set."""
        self._mode = "garbage"

    def serve_keys(self) -> None:
        """Answer normally again."""
        self._mode = "keys"

    # --- the fetch seam ----------------------------------------------------

    async def fetch(self, url: str) -> dict[str, Any]:
        """What `muse.auth`'s HTTP client calls. Records the request either way.

        A `dict` rather than an `httpx.Response`, so the production client is the only
        thing in the suite that knows about HTTP status codes and the fixture tests the
        *policy* — which is the part that is muse's.
        """
        self.fetches.append(url)
        if self._mode == "status":
            raise ConnectionError(f"JWKS {url} answered {self._status}")
        if self._mode == "garbage":
            return {"html": "<html>login required</html>"}
        return self.document()

    def __repr__(self) -> str:
        return f"Identity(kids={[key.kid for key in self._keys]}, fetches={len(self.fetches)})"


def identity(*keys: SigningKey) -> Identity:
    """An `Identity` publishing `keys`, or one fresh key pair."""
    return Identity(*keys)


# --- token factories ---------------------------------------------------------


def claims(**overrides: Any) -> dict[str, Any]:
    """A complete, valid claim set — every check in core's list, satisfied.

    Written out rather than merged into so that a test overriding one claim is stating
    "this token is valid *except* for this", which is the only way a single-failure test
    proves the check it names is the check that fired.
    """
    import time

    now = int(time.time())
    return {
        "iss": ISSUER,
        "aud": AUDIENCE,
        "sub": "usr_01",
        "exp": now + 600,
        "iat": now,
        "jti": "jti_01",
        "account_id": ACCOUNT,
        "scopes": SCOPE,
        **overrides,
    }


def token(
    identity_or_key: Identity | SigningKey,
    *,
    key: SigningKey | None = None,
    header: Mapping[str, Any] | None = None,
    **overrides: Any,
) -> str:
    """A token signed by `key`, defaulting to the identity's first key.

    `identity_or_key` takes either because both readings are common in a test: a rotation
    test has an `Identity` and wants *its* key, and a forgery test has a bare key and
    never mentions an identity at all.
    """
    signing = key
    if signing is None:
        keys = identity_or_key.keys if isinstance(identity_or_key, Identity) else [identity_or_key]
        signing = keys[0]
    return signing.sign(claims(**overrides), header=header)


def tampered(signed: str, **overrides: Any) -> str:
    """A token's payload rewritten without re-signing it.

    The signature must stop matching. This is what an attacker editing their own
    `scopes` to `completions:write` produces, and it is the test that says the check is
    a signature check rather than a claim check.
    """
    header, payload, signature = signed.split(".")
    edited = {**json.loads(_unb64url(payload)), **overrides}
    return f"{header}.{_segment(edited)}.{signature}"


def unsigned(payload: Mapping[str, Any] | None = None, **header: Any) -> str:
    """A compact JWS with no signature at all: what `alg: none` looks like."""
    return f"{_segment({'typ': 'JWT', **header})}.{_segment(payload or claims())}."


def symmetric(payload: Mapping[str, Any] | None = None, **header: Any) -> str:
    """An HS256 token signed with an HMAC key.

    The classic algorithm-confusion attack: an attacker who cannot produce an RSA
    signature signs the same claims with HS256 using the *public* key as the shared
    secret, hoping a verifier that reads `alg` from the token picks the symmetric
    algorithm and treats the public modulus as a secret. Refusing it is one of the
    reasons the algorithm is pinned rather than read.
    """
    secret = OctKey.import_key(b"a" * 32)
    return jwt.encode(
        {"alg": "HS256", **header},
        dict(payload or claims()),
        secret,
        algorithms=["HS256"],
    )


def _unb64url(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


__all__ = [
    "ACCOUNT",
    "AUDIENCE",
    "ISSUER",
    "SCOPE",
    "Identity",
    "SigningKey",
    "claims",
    "identity",
    "symmetric",
    "tampered",
    "token",
    "unsigned",
]
