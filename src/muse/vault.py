"""The credentials vault: one API key per provider, encrypted at rest.

AES-256-GCM under a key supplied by the environment, stored one row per provider in
`vault_secrets`. The design decisions that are not obvious, and why:

- **The key is never defaulted.** `MUSE_VAULT_KEY` must be present, must be base64,
  and must decode to exactly 32 bytes, or the process does not start. A vault that
  boots with a fallback key is a vault whose keys are readable by anyone who has read
  the source, and "we will notice in staging" is not a control.
- **The provider name is the additional authenticated data.** GCM authenticates its
  AAD, so a ciphertext copied from one row to another fails to decrypt instead of
  decrypting under the wrong vendor. Without this, one copy-paste in an operator's
  `psql` session is an undetected key swap.
- **The nonce is prepended to the ciphertext and drawn fresh per seal.** Reusing a
  nonce under one key destroys the confidentiality of both messages, and it is the
  one mistake in this module with no symptom until it is far too late. There is no
  parameter through which a caller could supply one.
- **Plaintext comes back as a `Secret`, never as a `str`.** `muse.redaction.Secret`
  makes an accidental `%s` safe; returning a bare string here would undo every
  guarantee it provides, at the one place the value is born.
- **The store is a parameter, not a global.** The `Database` protocol means the vault
  is exercised end to end against an in-memory store with no socket (AGENTS.md rule 3).
"""

from __future__ import annotations

import base64
import binascii
import os
import re
import secrets as _secrets
from collections.abc import Mapping

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from muse.db import Database
from muse.errors import VaultConfigError, VaultDecryptError, VaultKeyError
from muse.redaction import Secret, coerce

#: The environment variable holding the base64-encoded vault key.
VAULT_KEY_ENV = "MUSE_VAULT_KEY"

#: AES-256's key size. 16 would be AES-128 and 64 is not a thing; a key of either
#: length means the wrong variable was passed, so both are refused.
KEY_LENGTH = 32

#: The GCM nonce length in bytes. 96 bits is the size GCM is defined for; the other
#: legal sizes exist because GHASH can absorb them, not because they are a good idea.
NONCE_LENGTH = 12

#: The GCM authentication tag length in bytes. The full 16 is the default for
#: AESGCM.encrypt and reducing it would be a change nobody would notice in review.
TAG_LENGTH = 16

#: The row's key version. v1 always writes 1; the column exists so a later rotation
#: can re-encrypt in place without a migration or a second column.
KEY_VERSION = 1

#: Provider names are `lowercase_snake_case`, matching the CHECK constraint in
#: `migrations/00002_vault_secrets.sql` and the names a routes file uses.
PROVIDER_RE = re.compile(r"^[a-z][a-z0-9]*(_[a-z0-9]+)*$")

_UPSERT = """
insert into vault_secrets (provider, ciphertext, key_version)
values (%s, %s, %s)
on conflict (provider) do update
   set ciphertext = excluded.ciphertext,
       key_version = excluded.key_version,
       updated_at = now()
"""

_SELECT = "select ciphertext, key_version from vault_secrets where provider = %s"

_DELETE = "delete from vault_secrets where provider = %s"

_PROVIDERS = "select provider from vault_secrets order by provider"


def load_vault_key(environ: Mapping[str, str] | None = None) -> bytes:
    """The vault key from the environment, or refuse.

    Raises `VaultKeyError` — a `ConfigError`, so the app factory turns it into a boot
    failure rather than a first-request failure. The message names the variable and
    never its value.
    """
    source = os.environ if environ is None else environ
    raw = (source.get(VAULT_KEY_ENV) or "").strip()
    if not raw:
        raise VaultKeyError(
            f"{VAULT_KEY_ENV} is not set; muse stores provider credentials encrypted "
            "and will not start without a key"
        )
    try:
        key = base64.b64decode(raw, validate=True)
    except (binascii.Error, ValueError) as error:
        # The value is not in the message, and `error` from `b64decode` does not
        # contain it either — but a variable's value in a boot error is a credential
        # in whatever the operator pastes the error into.
        raise VaultKeyError(
            f"{VAULT_KEY_ENV} is not valid base64; generate one with "
            "`python -m muse.vault` and set the variable to its output"
        ) from error
    if len(key) != KEY_LENGTH:
        raise VaultKeyError(
            f"{VAULT_KEY_ENV} must decode to {KEY_LENGTH} bytes for AES-256, got {len(key)}"
        )
    return key


def generate_key() -> str:
    """A fresh base64 vault key, for an operator to put in their secret store.

    Offered as a command, never as a fallback. A key muse invented for itself is a
    key nobody wrote down, and a key nobody wrote down is a key that cannot be rotated.
    """
    return base64.b64encode(_secrets.token_bytes(KEY_LENGTH)).decode()


def seal(plaintext: bytes, key: bytes, provider: str) -> bytes:
    """Encrypt `plaintext` for `provider`.

    The result is `nonce || ciphertext || tag`. A fresh nonce per call, from the OS
    CSPRNG. The provider name is the AAD, which is what binds a ciphertext to its row.
    """
    nonce = _secrets.token_bytes(NONCE_LENGTH)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, provider.encode())


def unseal(sealed: bytes, key: bytes, provider: str) -> bytes:
    """Decrypt a value sealed for `provider`.

    Every failure mode — wrong key, tampered ciphertext, a ciphertext moved to another
    provider, a row from a future version — raises `VaultDecryptError` naming the
    provider and nothing about the value. That is deliberate: an operator's question is
    "which row is unreadable", and GCM's whole guarantee is that the *why* cannot be
    recovered without the key.
    """
    if len(sealed) < NONCE_LENGTH + TAG_LENGTH:
        raise VaultDecryptError(
            f"the stored value for provider {provider!r} is too short to be a sealed "
            "value; the row was not written by this vault"
        )
    nonce, body = sealed[:NONCE_LENGTH], sealed[NONCE_LENGTH:]
    try:
        return AESGCM(key).decrypt(nonce, body, provider.encode())
    except InvalidTag as error:
        # GCM cannot say whether the key, the ciphertext, or the provider name is
        # wrong, and neither can we. The provider is the actionable part.
        raise VaultDecryptError(
            f"the stored value for provider {provider!r} failed authentication: wrong "
            "vault key, or the value has been altered since it was written"
        ) from error


class Vault:
    """One encrypted credential per provider.

    Stateless apart from its key and its database, so one vault serves every request.
    """

    def __init__(self, database: Database, key: bytes) -> None:
        self._db = database
        self._key = key

    async def get(self, provider: str) -> Secret | None:
        """The key for `provider`, or `None` if none is stored.

        `None` rather than raising, because "this provider has no credential yet" is a
        normal state during onboarding and a raising read would make every caller
        handle an exception for a fact it can branch on.
        """
        name = _checked(provider)
        row = await self._db.fetchone(_SELECT, (name,))
        if row is None:
            return None
        return Secret(self._plaintext(row, name))

    async def put(self, provider: str, secret: Secret | str) -> None:
        """Store or replace the key for `provider`.

        One statement, so no transaction: a single statement is atomic by itself, and
        wrapping it would buy a round trip and a lock for nothing. `on conflict do
        update` is what makes a rotation a rotation — a plain insert would fail on the
        second write and leave the new key unapplied.
        """
        name = _checked(provider)
        value = coerce(secret)
        if not value:
            # An empty key is refused rather than stored: forwarded to a provider it
            # comes back as a 401, which reads like a wrong key rather than a
            # missing one.
            raise VaultConfigError(f"refusing to store an empty key for provider {name!r}")
        await self._db.execute(
            _UPSERT, (name, seal(value.reveal().encode(), self._key, name), KEY_VERSION)
        )

    async def delete(self, provider: str) -> bool:
        """Remove the key for `provider`. `True` if there was one.

        `False` rather than an error when there was nothing: deleting a key that is
        not there is the state the caller asked for, and raising would make every
        cleanup script do a read first.
        """
        name = _checked(provider)
        row = await self._db.fetchone(_SELECT, (name,))
        if row is None:
            return False
        await self._db.execute(_DELETE, (name,))
        return True

    async def has(self, provider: str) -> bool:
        """Whether a key is stored for `provider`.

        Exists so a caller can ask the question without handling `None`. `get` already
        answers it, but that is a control-flow branch where the caller wanted a
        boolean.
        """
        return await self.get(provider) is not None

    async def providers(self) -> tuple[str, ...]:
        """Every provider with a stored key, sorted.

        For a readiness body and for an operator's `psql`. Sorted so two calls with the
        same contents produce the same string.
        """
        rows = await self._db.fetchone(_PROVIDERS)
        if not rows:
            return ()
        return tuple(entry["provider"] for entry in rows.get("rows", ()))

    def _plaintext(self, row: Mapping[str, object], provider: str) -> str:
        version = row.get("key_version")
        if version != KEY_VERSION:
            # v1 always writes 1, so a row saying otherwise came from a future build
            # or a hand-edited database. Decrypting it anyway would mean ignoring the
            # one field that says how to interpret the rest of the row.
            raise VaultDecryptError(
                f"the stored value for provider {provider!r} is key version {version!r}; "
                f"this build reads version {KEY_VERSION}"
            )
        return unseal(bytes(row["ciphertext"]), self._key, provider).decode()

    def __repr__(self) -> str:
        """Says the key's length and nothing else.

        A `Vault` appears in tracebacks, in debugger panes and in `repr()` of anything
        holding it. Rendering the key there would put it in every one of those, and the
        length is the only fact about it an operator can act on.
        """
        return f"{type(self).__name__}(key_length={len(self._key)})"


def _checked(provider: str) -> str:
    """Validate a provider name against the table's own constraint.

    Checked here so the error names the value, and checked in Python so a bad name
    never reaches a query. The name is the primary key *and* the AAD, so a name with
    odd characters in it is a row nobody can clean up and a ciphertext that cannot be
    authenticated.
    """
    if not isinstance(provider, str) or not PROVIDER_RE.match(provider):
        raise VaultConfigError(
            f"{provider!r} is not a usable provider name; expected lowercase "
            "snake_case, as the vault_secrets constraint requires"
        )
    return provider


def _main() -> None:  # pragma: no cover - the operator-facing entry point
    """`python -m muse.vault` prints a fresh key.

    A key generated by a library call inside a service is a key that ended up in a
    log. This way it is generated by a command an operator ran, and pasted into a
    secret store by a person.
    """
    print(generate_key())  # the command's entire output


if __name__ == "__main__":  # pragma: no cover
    _main()
