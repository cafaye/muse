"""The credentials vault.

Every test here is about one of three things: a key that is encrypted at rest, a
key that is never printed, and a service that refuses to start rather than run
without a key. The third is the one that reads most like a formality and is the
most load-bearing: a vault that boots with a default key is a vault whose keys are
readable by anyone who has read the source.

The properties that are easy to state and expensive to lose:

- **AES-256-GCM, key from `MUSE_VAULT_KEY`, 32 bytes base64.** A wrong length, a
  non-base64 value, and a missing variable are all boot failures. There is no
  default key and no "generate one and warn" path, because both of those leave a
  deployment's credentials readable from its own logs.
- **The provider name is the additional authenticated data.** GCM authenticates its
  AAD, so a ciphertext moved from `openai` to `anthropic` fails to decrypt rather
  than decrypting under the wrong vendor. Without this, a copy-paste in an operator's
  SQL session is an undetected key swap.
- **A nonce per seal, never reused.** The nonce is stored with the ciphertext and
  prepended to it. Reusing one under the same key destroys the confidentiality of
  both messages, so it is drawn from the OS CSPRNG per seal and there is no code
  path that sets it.
- **Plaintext never reaches a log, an exception message, or a `repr`.** Asserted on
  `caplog` and on the error text, not inferred from the absence of a `print`.
"""

from __future__ import annotations

import base64
import logging

import pytest
from cryptography.exceptions import InvalidTag

from muse.errors import VaultConfigError, VaultDecryptError, VaultKeyError
from muse.redaction import Secret
from muse.vault import (
    KEY_LENGTH,
    KEY_VERSION,
    VAULT_KEY_ENV,
    Vault,
    generate_key,
    load_vault_key,
    seal,
    unseal,
)

from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PROVIDER = "openai"
OTHER_PROVIDER = "anthropic"
KEY = base64.b64encode(bytes(range(KEY_LENGTH))).decode()
OTHER_KEY = base64.b64encode(bytes(range(1, KEY_LENGTH + 1))).decode()
API_KEY = "sk-live-51H8xQ2eZvKYlo2C0fJk7Nb9pQrT4wYd"


def env(**overrides: str) -> dict[str, str]:
    """An environment with a valid key unless the test says otherwise."""
    return {VAULT_KEY_ENV: KEY, **overrides}


# --- the key ---------------------------------------------------------------


def test_the_key_is_read_from_the_named_variable() -> None:
    """Named in the variable rather than positional, because the variable name is
    what an operator searches for and a positional argument is what they do not."""
    assert VAULT_KEY_ENV == "MUSE_VAULT_KEY"


def test_a_valid_key_is_decoded_to_thirty_two_bytes() -> None:
    """32 bytes is AES-256's key size exactly. 16 would be AES-128 and 64 is not a
    thing; a key of either length means someone passed the wrong variable."""
    assert len(load_vault_key(env())) == 32
    assert KEY_LENGTH == 32


def test_a_missing_key_refuses_to_boot() -> None:
    """Not a warning, not a generated default. A vault that starts without a key is
    a vault whose keys are readable by anyone who can read this repository."""
    with pytest.raises(VaultKeyError, match=VAULT_KEY_ENV):
        load_vault_key({})


def test_an_empty_key_refuses_to_boot() -> None:
    """An empty variable is the most common deploy mistake, and it is indistinguishable
    from a missing one unless it is treated as missing."""
    with pytest.raises(VaultKeyError):
        load_vault_key({VAULT_KEY_ENV: ""})


def test_a_key_that_is_not_base64_refuses_to_boot() -> None:
    with pytest.raises(VaultKeyError, match="base64"):
        load_vault_key({VAULT_KEY_ENV: "not base64 at all!!"})


def test_a_key_of_the_wrong_length_refuses_to_boot() -> None:
    """16 bytes is AES-128 and 31 is a truncated paste. Both would otherwise produce
    a vault whose strength nobody chose."""
    short = base64.b64encode(bytes(16)).decode()
    with pytest.raises(VaultKeyError, match="32"):
        load_vault_key({VAULT_KEY_ENV: short})


def test_a_key_with_trailing_whitespace_is_accepted() -> None:
    """`MUSE_VAULT_KEY=$(cat keyfile)` leaves a newline, and a key file is the normal
    way to supply one. Rejecting that trains operators to strip it by hand, which is
    how a key ends up in a shell history."""
    assert load_vault_key({VAULT_KEY_ENV: f"  {KEY}\n"}) == load_vault_key(env())


def test_a_key_error_never_contains_the_key() -> None:
    """A key that is the wrong length is still a live credential in someone's paste
    buffer, and a boot error is the one message guaranteed to be read and forwarded."""
    wrong = base64.b64encode(b"a-far-too-short-key-for-aes-256").decode()
    with pytest.raises(VaultKeyError) as excinfo:
        load_vault_key({VAULT_KEY_ENV: wrong})
    assert wrong not in str(excinfo.value)


def test_a_generated_key_is_thirty_two_bytes_and_base64() -> None:
    """So an operator can mint one. Generation is offered as a *command*, never as a
    fallback: a key muse invented for itself is a key nobody wrote down."""
    generated = generate_key()
    assert len(base64.b64decode(generated)) == 32
    assert base64.b64encode(base64.b64decode(generated)).decode() == generated


def test_two_generated_keys_differ() -> None:
    assert generate_key() != generate_key()


# --- sealing and unsealing -------------------------------------------------


def test_a_sealed_value_does_not_contain_the_plaintext() -> None:
    """The property the whole table exists for. Asserted on the bytes, because a
    base64 of the plaintext would be just as bad."""
    sealed = seal(b"sk-live-secret", load_vault_key(env()), PROVIDER)
    assert b"sk-live-secret" not in sealed


def test_a_sealed_value_round_trips() -> None:
    key = load_vault_key(env())
    assert unseal(seal(b"sk-live-secret", key, PROVIDER), key, PROVIDER) == b"sk-live-secret"


def test_the_nonce_is_stored_with_the_ciphertext() -> None:
    """Prepended, so a row is self-contained: a ciphertext whose nonce lives
    somewhere else is not decryptable at all, which is the failure mode to prefer over
    one that silently decrypts to the wrong thing."""
    key = load_vault_key(env())
    first, second = seal(b"same", key, PROVIDER), seal(b"same", key, PROVIDER)
    assert first != second, "two seals of the same plaintext must differ"
    # GCM output is ciphertext + a 16-byte tag, plus a 12-byte nonce.
    assert len(first) == 12 + len(b"same") + 16


def test_a_seal_is_not_deterministic() -> None:
    """A fresh nonce per seal. Reusing one under the same key destroys the
    confidentiality of both messages, so this is asserted rather than trusted."""
    key = load_vault_key(env())
    assert seal(b"same", key, PROVIDER) != seal(b"same", key, PROVIDER)


def test_empty_plaintext_round_trips() -> None:
    """An empty key is rejected by the resolver, but `seal` is also used for values
    that are not credentials, and it should not have a surprising edge."""
    key = load_vault_key(env())
    assert unseal(seal(b"", key, PROVIDER), key, PROVIDER) == b""


def test_a_ciphertext_moved_to_another_provider_does_not_decrypt() -> None:
    """The reason the provider name is the AAD. A copy-paste in an operator's SQL
    session moves a key to the wrong vendor; GCM authenticating the AAD turns that
    into a loud failure instead of a request billed to the wrong account."""
    key = load_vault_key(env())
    sealed = seal(b"sk-live-secret", key, PROVIDER)
    with pytest.raises(VaultDecryptError, match="authentication"):
        unseal(sealed, key, OTHER_PROVIDER)


def test_the_wrong_key_does_not_decrypt() -> None:
    key, other = load_vault_key(env()), load_vault_key({VAULT_KEY_ENV: OTHER_KEY})
    with pytest.raises(VaultDecryptError):
        unseal(seal(b"sk-live-secret", key, PROVIDER), other, PROVIDER)


def test_a_tampered_ciphertext_does_not_decrypt() -> None:
    """GCM's authentication tag is the reason. A bit-flipped ciphertext is either
    detected or it is not encryption."""
    key = load_vault_key(env())
    sealed = bytearray(seal(b"sk-live-secret", key, PROVIDER))
    sealed[-1] ^= 0x01
    with pytest.raises(VaultDecryptError):
        unseal(bytes(sealed), key, PROVIDER)


def test_a_truncated_ciphertext_does_not_decrypt() -> None:
    key = load_vault_key(env())
    with pytest.raises(VaultDecryptError):
        unseal(seal(b"sk-live-secret", key, PROVIDER)[:10], key, PROVIDER)


def test_a_ciphertext_that_is_too_short_for_a_nonce_does_not_decrypt() -> None:
    """A row written by something that is not this vault. Reported as a decrypt
    failure rather than an index error, because the operator's question is "is my key
    wrong or is my data wrong" and the answer here is "the data"."""
    with pytest.raises(VaultDecryptError, match="too short"):
        unseal(b"tiny", load_vault_key(env()), PROVIDER)


def test_a_decrypt_error_never_contains_the_ciphertext() -> None:
    """Ciphertext is not secret in the way plaintext is, but it is the one value that
    identifies *which* row is broken, and dumping it into an exception puts a
    fingerprint of the key's usage in every log it passes through."""
    key = load_vault_key(env())
    sealed = seal(b"sk-live-secret", key, PROVIDER)
    with pytest.raises(VaultDecryptError) as excinfo:
        unseal(sealed, base64.b64decode(OTHER_KEY), PROVIDER)
    assert sealed.hex() not in str(excinfo.value)


# --- the store -------------------------------------------------------------


async def test_a_stored_key_is_not_stored_in_the_clear() -> None:
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))

    await vault.put(PROVIDER, Secret(API_KEY))

    assert API_KEY.encode() not in db.vault[PROVIDER]["ciphertext"]


async def test_a_stored_key_reads_back_as_the_same_value() -> None:
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))

    await vault.put(PROVIDER, Secret(API_KEY))
    recovered = await vault.get(PROVIDER)

    assert recovered.reveal() == API_KEY


async def test_a_recovered_key_does_not_print_itself() -> None:
    """The value is handed back as a `Secret`, not a `str`, so a caller that logs it
    gets the marker. Returning a bare string here would undo every guarantee
    `muse.redaction` provides, at the one place the value is born."""
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    await vault.put(PROVIDER, Secret(API_KEY))

    recovered = await vault.get(PROVIDER)

    assert isinstance(recovered, Secret)
    assert API_KEY not in repr(recovered)
    assert API_KEY not in str(recovered)


async def test_storing_the_same_provider_twice_replaces_the_key() -> None:
    """Key rotation. The row is keyed by provider, so a second write is an update —
    and an insert that failed would leave a rotation silently not applied."""
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))

    await vault.put(PROVIDER, Secret("sk-old"))
    await vault.put(PROVIDER, Secret("sk-new"))

    assert len(db.vault) == 1
    assert (await vault.get(PROVIDER)).reveal() == "sk-new"


async def test_replacing_a_key_keeps_the_original_created_at() -> None:
    """`on conflict do update` must not touch `created_at`; it is when the row entered
    the vault, and an update that moved it would make "when did this provider join"
    unanswerable."""
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))

    await vault.put(PROVIDER, Secret("sk-old"))
    created = db.vault[PROVIDER]["created_at"]
    await vault.put(PROVIDER, Secret("sk-new"))

    assert db.vault[PROVIDER]["created_at"] == created
    assert db.vault[PROVIDER]["updated_at"] != created


async def test_an_unknown_provider_has_no_key() -> None:
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    assert await vault.get(PROVIDER) is None


async def test_has_reports_whether_a_key_is_stored() -> None:
    """`get` returning `None` already answers it, so this exists to say so without
    making a caller handle `None` — the difference between a readability question
    and a control-flow one."""
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    assert await vault.has(PROVIDER) is False
    await vault.put(PROVIDER, Secret(API_KEY))
    assert await vault.has(PROVIDER) is True


async def test_a_deleted_key_is_gone() -> None:
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))
    await vault.put(PROVIDER, Secret(API_KEY))

    assert await vault.delete(PROVIDER) is True
    assert await vault.get(PROVIDER) is None


async def test_deleting_a_provider_with_no_key_reports_nothing_to_do() -> None:
    """`False` rather than an error: deleting a key that is not there is the state the
    caller asked for, and an error would make cleanup scripts need a read first."""
    assert await Vault(FakeDatabase(), load_vault_key(env())).delete(PROVIDER) is False


async def test_providers_are_listed_sorted() -> None:
    """For a readiness body or an operator's `psql`. Sorted so two calls with the same
    contents produce the same string."""
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    await vault.put("openai", Secret("a"))
    await vault.put("anthropic", Secret("b"))
    await vault.put("fake", Secret("c"))
    assert await vault.providers() == ("anthropic", "fake", "openai")


async def test_listing_providers_on_an_empty_vault_is_empty() -> None:
    assert await Vault(FakeDatabase(), load_vault_key(env())).providers() == ()


async def test_a_write_is_one_statement() -> None:
    """A single statement is atomic by itself, so a single-statement write needs no
    transaction — and wrapping it in one would be a round trip bought for nothing."""
    db = FakeDatabase()
    await Vault(db, load_vault_key(env())).put(PROVIDER, Secret(API_KEY))
    assert db.commits == 0
    assert len(db.statements) == 1


async def test_the_write_names_the_columns_it_sets() -> None:
    """The whole column list, asserted, because `on conflict do update` that forgets
    a column silently keeps a stale value."""
    db = FakeDatabase()
    await Vault(db, load_vault_key(env())).put(PROVIDER, Secret(API_KEY))
    sql = db.statements[0].sql.lower()
    for column in ("ciphertext", "key_version", "updated_at"):
        assert column in sql
    assert "on conflict" in sql


async def test_the_key_version_is_written() -> None:
    """Written now so a later rotation can re-encrypt in place without a migration
    or a second column — the version a row was sealed under is part of the row."""
    db = FakeDatabase()
    await Vault(db, load_vault_key(env())).put(PROVIDER, Secret(API_KEY))
    assert db.vault[PROVIDER]["key_version"] == KEY_VERSION


async def test_a_row_sealed_under_another_version_is_refused() -> None:
    """v1 always writes 1, so a row saying 2 came from a future build or a
    hand-edited database. Decrypting it anyway would mean ignoring the one field that
    says how to interpret the rest."""
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))
    await vault.put(PROVIDER, Secret(API_KEY))
    db.vault[PROVIDER]["key_version"] = 2

    with pytest.raises(VaultDecryptError, match="version"):
        await vault.get(PROVIDER)


async def test_a_provider_name_is_validated_on_write() -> None:
    """The name is the primary key and the AAD. A name with a quote in it is a
    parameter today, and a constraint today, and a stored row nobody can delete
    cleanly tomorrow."""
    with pytest.raises(VaultConfigError, match="provider"):
        await Vault(FakeDatabase(), load_vault_key(env())).put("open ai", Secret("x"))


async def test_a_provider_name_is_validated_on_read() -> None:
    with pytest.raises(VaultConfigError):
        await Vault(FakeDatabase(), load_vault_key(env())).get("")


async def test_a_provider_name_is_validated_on_delete() -> None:
    with pytest.raises(VaultConfigError):
        await Vault(FakeDatabase(), load_vault_key(env())).delete("OPENAI")


async def test_an_empty_key_is_refused_on_write() -> None:
    """Storing an empty key produces a 401 from the provider that reads like a wrong
    key rather than a missing one, which sends the operator to rotate a key that does
    not exist."""
    with pytest.raises(VaultConfigError, match="empty"):
        await Vault(FakeDatabase(), load_vault_key(env())).put(PROVIDER, Secret(""))


async def test_a_bare_string_key_is_accepted() -> None:
    """Wrapped on the way in, so a caller reading from a config file does not have to
    remember to wrap it and then log it."""
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    await vault.put(PROVIDER, API_KEY)
    assert (await vault.get(PROVIDER)).reveal() == API_KEY


# --- the plaintext must not leak -------------------------------------------


async def test_nothing_about_a_write_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The end-to-end version of the rule. `Secret` makes an accidental `%s` safe, and
    this proves there is no second, less careful path through the vault."""
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))

    with caplog.at_level(logging.DEBUG, logger="muse"):
        await vault.put(PROVIDER, Secret(API_KEY))
        await vault.get(PROVIDER)
        await vault.providers()

    assert API_KEY not in caplog.text
    assert caplog.text == ""


async def test_the_vault_does_not_repr_its_key() -> None:
    """A `Vault` in a traceback frame or a debugger pane must not render the key it
    holds. The key itself is bytes, so the repr shows its length and nothing else."""
    vault = Vault(FakeDatabase(), load_vault_key(env()))
    assert KEY not in repr(vault)


async def test_a_failed_read_does_not_include_the_plaintext_or_the_ciphertext() -> None:
    """The error an operator sees names the provider and nothing else about the value:
    the provider is what they can act on, and the value is what they must not copy
    into a ticket."""
    db = FakeDatabase()
    key = load_vault_key(env())
    await Vault(db, key).put(PROVIDER, Secret(API_KEY))
    db.vault[PROVIDER]["key_version"] = 99

    with pytest.raises(VaultDecryptError) as excinfo:
        await Vault(db, key).get(PROVIDER)

    assert PROVIDER in str(excinfo.value)
    assert API_KEY not in str(excinfo.value)
    assert db.vault[PROVIDER]["ciphertext"].hex() not in str(excinfo.value)


async def test_two_providers_keys_are_independent() -> None:
    """The AAD doing its job in the shape production uses: two rows in one table, one
    key each, neither readable as the other."""
    db = FakeDatabase()
    vault = Vault(db, load_vault_key(env()))
    await vault.put(PROVIDER, Secret("sk-openai"))
    await vault.put(OTHER_PROVIDER, Secret("sk-anthropic"))

    assert (await vault.get(PROVIDER)).reveal() == "sk-openai"
    assert (await vault.get(OTHER_PROVIDER)).reveal() == "sk-anthropic"


def test_the_gcm_cipher_is_aes_256() -> None:
    """Pinned so a refactor to a different cipher — or a key length `cryptography`
    accepts but nobody chose — fails here rather than in a vault nobody can read.

    Decrypted through the primitive API rather than `AESGCM`, so this asserts the
    bytes on the wire are the format claimed and not merely round-trip through the
    same helper that wrote them.
    """
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    key = load_vault_key(env())
    sealed = seal(b"x", key, PROVIDER)
    nonce, body, tag = sealed[:12], sealed[12:-16], sealed[-16:]
    decipher = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
    decipher.authenticate_additional_data(PROVIDER.encode())
    assert decipher.update(body) + decipher.finalize() == b"x"
    assert len(key) == 32


def test_a_decrypt_failure_is_translated_from_the_tags_own_error() -> None:
    """The translation is asserted through its cause, not by letting `InvalidTag`
    escape. That is the actual contract: a tampered row must surface as a muse error,
    and if `cryptography` ever raised something else the `except` would stop matching
    and a tamper would become a 500 instead of a decrypt failure."""
    key = load_vault_key(env())
    sealed = bytearray(seal(b"x", key, PROVIDER))
    sealed[-1] ^= 0xFF

    with pytest.raises(VaultDecryptError) as excinfo:
        unseal(bytes(sealed), key, PROVIDER)

    assert isinstance(excinfo.value.__cause__, InvalidTag)
