-- 00002_vault_secrets.sql — the credentials vault.
--
-- One row per provider, one API key per row, encrypted at rest with AES-256-GCM
-- under the key in MUSE_VAULT_KEY. The column list is deliberately short, and the
-- shortest part is the point: there is no plaintext column, no `updated_by`, no
-- `metadata` jsonb that a future change could put a key in. The only way a key
-- reaches this table is through `muse.vault.Vault`, which encrypts first.
--
-- `ciphertext` is the nonce, the ciphertext, and the GCM tag concatenated, as
-- `bytea`. Storing them together is what makes a decryption self-contained: a row
-- that has been copied to another table is still decryptable, and a row whose
-- nonce was separated from it is not decryptable at all, which is the failure
-- mode to prefer.
--
-- `key_version` exists so a key rotation can re-encrypt in place without a
-- migration or a second column. v1 always writes 1.

create table if not exists vault_secrets (
    -- The provider this key belongs to. The primary key, because a provider has
    -- exactly one key and the ciphertext is bound to this value as AES-GCM
    -- additional authenticated data — moving a row to another provider's name
    -- makes it undecryptable rather than silently usable.
    provider    text        primary key,

    ciphertext  bytea       not null,

    key_version int         not null default 1,

    created_at  timestamptz not null default now(),
    updated_at  timestamptz not null default now(),

    constraint vault_secrets_provider_format check (
        provider ~ '^[a-z][a-z0-9]*(_[a-z0-9]+)*$'
    ),
    constraint vault_secrets_key_version_positive check (key_version >= 1)
);

-- The provider name is the whole lookup key, so the primary key index is the
-- index. No secondary index is added here on purpose: an index over a table with
-- one row per provider is a second thing to keep correct and nothing to make
-- faster.
