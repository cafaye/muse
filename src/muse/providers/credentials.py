"""Where a provider's API key comes from.

An interface with two implementations and one production wiring, kept in its own
module so the *shape* the router depends on is stated once rather than being implied
by the vault's own signature. `muse.providers` re-exports both, because a caller that
only wants to hand a router a dict of keys should not have to know which module the
protocol lives in.

The production resolver is `VaultCredentials`, which reads through the vault on every
request. That is deliberate: a resolver that cached at boot would mean a rotated key
needs a restart to take effect, and "we restarted the pods" is the answer to a
question an operator should never have to ask.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from muse.errors import CredentialUnavailable
from muse.redaction import Secret, coerce


@runtime_checkable
class CredentialResolver(Protocol):
    """Where a provider's API key comes from."""

    async def credential_for(self, provider: str) -> Secret:
        """The key for `provider`, or raise `CredentialUnavailable`.

        Async because the production implementation reads the database. A sync
        interface would force the vault's read onto a thread, or force a cache, and
        both are worse than awaiting.
        """
        ...


class StaticCredentials:
    """Keys from a mapping. For configuration files and tests.

    Not the production path: every value is in the process's memory as a plain
    string, which is the thing the vault exists to avoid. It exists so a route can be
    exercised without a database, and so the interface the router depends on is
    explicit rather than inferred.
    """

    def __init__(self, keys: dict[str, Secret | str]) -> None:
        self._keys = dict(keys)

    async def credential_for(self, provider: str) -> Secret:
        try:
            secret = coerce(self._keys[provider])
        except KeyError:
            raise CredentialUnavailable(
                f"no credential is configured for provider {provider!r}"
            ) from None
        if not secret:
            # An empty key in an env file is a real deploy mistake, and forwarding it
            # produces a 401 that reads like a *wrong* key rather than a missing one.
            raise CredentialUnavailable(f"the credential for provider {provider!r} is empty")
        return secret


class VaultCredentials:
    """Keys from the vault, read per request.

    The read happens on every call rather than being cached at construction, so a
    rotated key takes effect on the next request. A cache would need invalidation, and
    a cache that cannot be invalidated from outside the process is a key that needs a
    deploy to rotate — which is the failure this whole module exists to prevent.
    """

    def __init__(self, vault) -> None:  # noqa: ANN001 - Vault, avoiding an import cycle
        self._vault = vault

    async def credential_for(self, provider: str) -> Secret:
        secret = await self._vault.get(provider)
        if secret is None:
            raise CredentialUnavailable(
                f"no credential is stored for provider {provider!r}"
            )
        return secret

    def __repr__(self) -> str:
        return f"{type(self).__name__}()"
