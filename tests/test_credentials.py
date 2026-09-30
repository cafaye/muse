"""`VaultCredentials` — the production path from a provider name to a key.

The vault itself is tested in `test_vault.py`; this covers the adapter between the
vault and a provider, which is where the two interesting properties live:

- **A missing credential is `CredentialUnavailable`, not a decrypt error.** The
  provider adapter maps those two differently — one ends the request, the other is
  permanent — so collapsing them would turn "we have not onboarded this vendor yet"
  into something that reads like a corrupt row.
- **Nothing here caches.** A resolver that cached a key would make a rotated key
  invisible until a deploy, which is the whole reason a vault exists.

The provider-side behaviour that depends on this — that a resolved key is used for the
call and scrubbed out of the error — is asserted in `test_providers.py`.
"""

from __future__ import annotations

import base64

import pytest

from muse.errors import CredentialUnavailable
from muse.providers.credentials import StaticCredentials, VaultCredentials
from muse.redaction import Secret
from muse.vault import Vault, load_vault_key

from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PROVIDER = "openai"
KEY = base64.b64encode(bytes(range(32))).decode()
API_KEY = "sk-live-51H8xQ2eZvKYlo2C0fJk7Nb9pQrT4wYd"


def vault_with() -> Vault:
    return Vault(FakeDatabase(), load_vault_key({"MUSE_VAULT_KEY": KEY}))


async def test_a_stored_key_is_returned() -> None:
    vault = vault_with()
    await vault.put(PROVIDER, Secret(API_KEY))
    assert (await VaultCredentials(vault).credential_for(PROVIDER)).reveal() == API_KEY


async def test_a_missing_key_is_reported_as_unavailable() -> None:
    """`CredentialUnavailable`, and the message names the provider. A decrypt error
    here would tell an operator their row is corrupt when the truth is that they have
    not onboarded this vendor yet — two completely different pieces of work."""
    with pytest.raises(CredentialUnavailable, match=PROVIDER):
        await VaultCredentials(vault_with()).credential_for(PROVIDER)


async def test_a_rotated_key_is_visible_on_the_next_read() -> None:
    """The property that makes the vault worth having over an env var. Caching here
    would mean a rotation needs a deploy, and "did you restart the pods" is the wrong
    answer to "is the new key live yet"."""
    vault = vault_with()
    resolver = VaultCredentials(vault)
    await vault.put(PROVIDER, Secret("sk-old"))

    assert (await resolver.credential_for(PROVIDER)).reveal() == "sk-old"
    await vault.put(PROVIDER, Secret("sk-new"))
    assert (await resolver.credential_for(PROVIDER)).reveal() == "sk-new"


async def test_the_resolver_satisfies_the_credential_resolver_protocol() -> None:
    """The interface the provider is typed against. A duck-typed check rather than an
    `isinstance`, so a renamed method fails here instead of at the first call."""
    from muse.providers.credentials import CredentialResolver

    assert isinstance(VaultCredentials(vault_with()), CredentialResolver)
    assert isinstance(StaticCredentials({}), CredentialResolver)


async def test_the_resolver_does_not_print_itself() -> None:
    """It holds the vault, and the vault holds the key. A repr that reached through to
    it would put a credential into a debugger pane — and `Vault.__repr__` already
    reports only the key's length, so nothing here should undo that."""
    assert "key_length" not in repr(VaultCredentials(vault_with()))


async def test_a_static_resolver_satisfies_the_same_protocol() -> None:
    """Two implementations, one interface. A test that exercised only the vault one
    would not notice the static one drifting out of shape."""
    from muse.providers.credentials import CredentialResolver

    assert isinstance(StaticCredentials({PROVIDER: "sk-test"}), CredentialResolver)


async def test_a_vault_backed_resolver_and_a_static_one_agree() -> None:
    """The production wiring and the test wiring are interchangeable, which is the
    only reason a test using `StaticCredentials` says anything about production."""
    vault = vault_with()
    await vault.put(PROVIDER, Secret(API_KEY))
    from_vault = await VaultCredentials(vault).credential_for(PROVIDER)
    from_config = await StaticCredentials({PROVIDER: Secret(API_KEY)}).credential_for(PROVIDER)
    assert from_vault == from_config
