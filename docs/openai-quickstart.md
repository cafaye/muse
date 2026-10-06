# A stock OpenAI client against muse

The reader's path to `POST /v1/chat/completions` and `GET /v1/models`, from a running
stack to a completion, with every command spelled out. Nothing here needs the source.

Three things a client needs to be told and this repository did not say anywhere: **the
base URL**, **where the token comes from**, and **what to put in `model`**. That is the
whole of what this document is, plus the two fields on this surface that look like
mistakes and are not.

`POST /v1/route` is the same machinery in muse's own shape and is documented in the
[README](../README.md#endpoints). This page is about the OpenAI-compatible aliases.

---

## 1. The base URL

```
http://localhost:8000/v1
```

`8000` is muse's own published port (`docker-compose.yml`, and uvicorn's default if you
run it outside compose). `/v1` is part of the base URL because OpenAI's clients append
`/chat/completions` and `/models` themselves — the same reason `api_base` for OpenAI
ends in `/v1`. A client pointed at `http://localhost:8000` asks for
`/chat/completions`, which is a 404 here.

## 2. A model name you can actually ask for

Ask for a **route name**, not a vendor model id. The ones that exist are whatever
`config/routes.yaml` says, and the committed file has two:

| `model` | Served by | For |
| --- | --- | --- |
| `fast` | `openai/gpt-4o-mini`, falling back to `anthropic/claude-haiku-4-5` | classification, extraction, routing |
| `smart` | `anthropic/claude-sonnet-4-5`, falling back to `openai/gpt-4o` | reasoning, long output |

`gpt-4o-mini` is a 404 here. That is not an oversight and not an accident of
configuration: `/v1/models` deliberately lists route names, because a caller who could
learn a vendor's model id from the model list could pin a request to that vendor, and
the fallback chain — the reason this service exists — stops being possible. You never
learn which vendor answered.

Ask the service rather than this table, which is the point of `GET /v1/models`:

```sh
curl -s http://localhost:8000/v1/models -H "Authorization: Bearer $MUSE_TOKEN"
```

```json
{"object":"list","data":[
  {"id":"fast","object":"model","created":0,"owned_by":"muse"},
  {"id":"smart","object":"model","created":0,"owned_by":"muse"}
]}
```

`created` is `0` on every entry, always. muse has no model-creation timestamp and
inventing one would be a lie in a machine-readable field; some clients display it and
none may act on it. `owned_by` is `muse` because OpenAI's field names the organisation
that owns the model, and this service is a router in front of vendors it does not own.

## 3. The token, and the honest gap in this section

**A local run cannot get one from `identity` today, and a quickstart that pretended
otherwise would waste your afternoon.**

What muse requires, precisely (`src/muse/auth.py`):

- an **RS256** JWT — `alg: none`, HS256 and ES256 are all refused, and the algorithm is
  pinned in the verifier's call rather than read out of the token;
- verified against the key set at `MUSE_JWKS_URL`, defaulting to
  `{MUSE_IDENTITY_ISSUER}/.well-known/jwks.json`;
- `iss` = your `MUSE_IDENTITY_ISSUER` (default `https://identity.cafaye.com`), `aud` =
  `MUSE_IDENTITY_AUDIENCE` (default `muse`), and `sub`, `iat`, `jti`, `exp`, `nbf`
  present and correct;
- an **`account_id`** — no fallback to `sub`, because muse meters per tenant and an
  unattributable spend is a request nobody can charge for;
- **`completions:write`**, in `scopes` or `scope`. Absent means the empty set, never
  everything: a correctly signed token with no scope is a **403**, not an allow.

Why you cannot get one from a local `identity`:

- **its scope vocabulary does not contain `completions:write`.** `identity`'s
  `AllScopes()` is eight names — `accounts:read`, `accounts:write`, `accounts:delete`,
  `oidc_clients:write`, `audit_log:read`, `account_invitations:write`,
  `sessions:write`, `users:disable` — and a mint naming anything else is a 422
  `unsupported`. `muse`'s scope is muse's own surface, and adding a scope to another
  service's vocabulary is a platform decision, not a self-hoster's.
- **its scoped API keys are opaque, not JWTs.** `identity` mints `cafaye_`-prefixed
  random strings for `POST /v1/accounts/:id/api-keys` and answers introspection for
  them; there is no signature for muse to verify.
- its OIDC access tokens *are* signed, but their `aud` is the client that asked for the
  token, and its scope set is `openid email profile accounts publish`.

So the credential a self-hoster can actually get today is one they mint themselves,
against a key set they publish, and point muse at. That is a **development** arrangement:
it is the documented escape hatch `MUSE_JWKS_URL` exists for, and it is not a
production credential path. When `identity` learns `completions:write` this section
becomes "mint a token from identity" and nothing else changes — which is why it is
written as the requirements rather than as a recipe for this one workaround.

### 3a. Mint a local token

Run from a muse checkout, so `cryptography` and `joserfc` are already installed. It
generates an RSA key, writes the public half as a JWKS, and prints one token:

```sh
mkdir -p .dev-identity/.well-known

uv run python - <<'PY'
import base64, json, time, uuid, pathlib
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from joserfc import jwt
from joserfc.jwk import RSAKey

KID, PORT = "muse-local-dev", 8901
out = pathlib.Path(".dev-identity")
(out / ".well-known").mkdir(parents=True, exist_ok=True)

key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
pem = key.private_bytes(serialization.Encoding.PEM,
                       serialization.PrivateFormat.PKCS8,
                       serialization.NoEncryption())
(out / "signing-key.pem").write_bytes(pem)

def b64(n):                       # base64url, no padding — RFC 7515
    return base64.urlsafe_b64encode(n.to_bytes((n.bit_length() + 7) // 8, "big")).rstrip(b"=").decode()

pub = key.public_key().public_numbers()
(out / ".well-known" / "jwks.json").write_text(json.dumps({"keys": [{
    "kty": "RSA", "use": "sig", "alg": "RS256", "kid": KID, "n": b64(pub.n), "e": b64(pub.e),
}]}, indent=2) + "\n")

now = int(time.time())
claims = {
    "iss": f"http://localhost:{PORT}", "aud": "muse", "sub": "local-developer",
    "iat": now, "nbf": now, "exp": now + 3600, "jti": uuid.uuid4().hex,
    "account_id": "00000000-0000-0000-0000-000000000001",
    # Both names, byte-identical. identity mints both for exactly this reason, and
    # muse REFUSES a token carrying both where they disagree rather than picking one.
    "scopes": "completions:write", "scope": "completions:write",
}
print(jwt.encode({"alg": "RS256", "kid": KID}, claims, RSAKey.import_key(pem),
                 algorithms=["RS256"]))
PY
```

Both scope claim names matter. `core/docs/openapi-conventions.md` says `scopes`;
`gateway` reads a space-separated `scope`; identity mints both byte-identical so the
choice can be made without breaking an issued token. A token carrying both where they
**disagree** is a 401 — not merged, not preferred, not unioned, because a token whose
two authorisation claims contradict each other is one muse does not understand.

### 3b. Publish the key set and point muse at it

Serve the JWKS over HTTP, on the path muse fetches:

```sh
uv run python -m http.server 8901 --bind 127.0.0.1 --directory .dev-identity
```

(`/.well-known/jwks.json` is a path, so the file has to be inside a `.well-known`
directory at the server root — which is why it is written there above.)

Then run muse with the issuer and the key set pointed at it, and with a vault key and a
database:

```sh
export MUSE_VAULT_KEY=$(uv run python -m muse.vault)
export MUSE_IDENTITY_ISSUER=http://localhost:8901
export MUSE_JWKS_URL=http://127.0.0.1:8901/.well-known/jwks.json
export MUSE_IDENTITY_AUDIENCE=muse
export MUSE_DATABASE_URL='postgres://muse:cafaye@localhost:15500/muse'   # kit's cluster

uv run uvicorn --factory muse.main:create_app --port 8000
```

`MUSE_VAULT_KEY` has no default and muse refuses to start without one — a vault that
boots with a fallback key is a vault whose keys are readable by anyone who has read
this repository. `python -m muse.vault` prints one.

Check it came up before spending anything on a completion:

```sh
curl -s localhost:8000/healthz   # {"status":"ok"} — consults nothing, on purpose
curl -s localhost:8000/readyz    # {"status":"ok","checks":{"db":"ok"}}
```

`/healthz` never touches a dependency; that is deliberate, because a liveness probe that
consults one turns a database outage into a restart loop. `/readyz` is the one that
reports `db`.

If `identity` is unreachable, every completion is a **503**, not a 401 — muse is up and
something it depends on is not, and a 401 would send you off to re-authenticate against
a perfectly good token.

---

## 4. The worked example

Every command below was run against a muse built from this repository, serving a
stubbed OpenAI endpoint. The requests, the headers, the paths and the responses are
real output; the completion text is the stub's.

### 4a. `curl`, which needs nothing installed

```sh
MUSE_TOKEN=$(cat .dev-identity/token)

curl -s localhost:8000/v1/chat/completions \
  -H "Authorization: Bearer $MUSE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}]}'
```

```json
{"id":"chatcmpl-0cc3eeaf45254b7f8112243d","object":"chat.completion",
 "created":1791244107,"model":"gpt-4o-mini",
 "choices":[{"index":0,"message":{"role":"assistant","content":"stub heard: hello"},
             "finish_reason":"stop"}],
 "usage":{"prompt_tokens":9,"completion_tokens":7,"total_tokens":16}}
```

Six fields, and nothing else. Add `-i` and you will also get `X-Trace-Id: …`, which is
on **every** response this service sends — that is where support correlation lives.

### 4b. The official OpenAI Python SDK

```sh
uv run --with openai python - <<'PY'
import os
from openai import OpenAI

client = OpenAI(base_url="http://localhost:8000/v1", api_key=os.environ["MUSE_TOKEN"])

print([m.id for m in client.models.list().data])          # ['fast', 'smart']

completion = client.chat.completions.create(
    model="fast",
    messages=[{"role": "user", "content": "hello"}],
)
print(completion.choices[0].message.content)
print(completion.usage.total_tokens)
PY
```

Two arguments and nothing else: `base_url` and `api_key`. The SDK's `api_key` is just
the bearer token, and `base_url` is `http://localhost:8000/v1` — the `/v1` included,
because the SDK appends `/chat/completions` to it.

Real output from that script against this repository:

```
['fast', 'smart']
id: chatcmpl-835152b583a14b288d95c426
model: gpt-4o-mini
content: stub heard: hello
usage: 9 7 16
```

### 4c. Node

Identical shape, for the official Node SDK:

```js
import OpenAI from "openai";

const client = new OpenAI({
  baseURL: "http://localhost:8000/v1", // /v1 included, same reason
  apiKey: process.env.MUSE_TOKEN,
});

const { data } = await client.models.list();
console.log(data.map((m) => m.id)); // [ 'fast', 'smart' ]

const completion = await client.chat.completions.create({
  model: "fast",
  messages: [{ role: "user", content: "hello" }],
});
console.log(completion.choices[0].message.content);
```

---

## 5. The four things that will surprise you

Each of these is a refusal rather than an oversight, and each names itself in the error.

**`temperature` must be a decimal *string*, not a number.** OpenAI's own clients send
that field as a JSON number, so this is the field most likely to surprise you, and it
is a **422 naming `temperature`** on this alias exactly as on `/v1/route`. `"0.7"` works;
`0.7` does not. A float cannot be round-tripped — a JavaScript caller sending `0.1 + 0.2`
puts `0.30000000000000004` on the wire, every digit of which would be forwarded to the
provider — and this service does not coerce it, because coercing on the compatibility
path would put that defect back exactly where it is least likely to be noticed.

**No streaming.** A body carrying `stream` is a **422 naming `stream`**, not a
silently non-streaming answer. A caller who asked for a stream and received a whole
response has no way to tell what happened. Set `stream=False` or leave it unset.

**Unknown fields are refused, not ignored.** The request body is closed
(`extra="forbid"`), so a typo'd `max_token` is a 422 rather than a request that quietly
did not cap anything. Send `model`, `messages`, and optionally `max_tokens` and
`temperature`.

**`provider`, `route`, `cost_micros` and `trace_id` are absent from the response.** They
are on `/v1/route` and they are deliberately not here: this is the compatibility surface,
and a compatibility surface that grows its own extras is one every caller has to be
taught about — which is the opposite of what an alias is for. The generated clients this
path exists for are the strictest readers of a response shape. **`X-Trace-Id` still
carries the trace id**, so support correlation survives. If you want the muse-specific
fields, call `/v1/route` — it is the same routing, the same fallback chain and the same
metered event, in muse's shape:

```sh
curl -s localhost:8000/v1/route \
  -H "Authorization: Bearer $MUSE_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"model":"fast","messages":[{"role":"user","content":"hello"}]}'
```

```json
{"id":"chatcmpl-ebc691f07f1e46209fa6f068","object":"chat.completion",
 "created":1791244400,"model":"gpt-4o-mini","provider":"openai","route":"fast",
 "choices":[{"index":0,"message":{"role":"assistant","content":"stub heard: hello"},
             "finish_reason":"stop"}],
 "usage":{"prompt_tokens":9,"completion_tokens":7,"total_tokens":16},
 "cost_micros":6,"trace_id":"f35948c4d0414241bdbe0923b7d4d331"}
```

### Statuses worth recognising

Every non-2xx is `application/problem+json` — the same envelope, the same codes, on all
three endpoints.

| Code | Status | When |
| --- | --- | --- |
| `unauthorized` | 401 | the credential is absent, malformed, unverified, expired, or carries no `account_id` |
| `forbidden` | 403 | it verified and lacks `completions:write` |
| `not_found` | 404 | no route for that `model` — including `gpt-4o-mini` |
| `validation_failed` | 422 | not a valid route request: a bad field, a numeric `temperature`, a `stream` |
| `unavailable` | 503 | every candidate provider failed, **or** the key set is unreachable |
| `internal` | 500 | a bug in muse; `trace_id` is the handle |

A 503 is the one worth retrying: it means muse is up and something it depends on is not.

### If every completion is a 503

Check the issuer **first**, because a 503 is indistinguishable from a routing fault at
the response and the routing is the thing you will go looking at. In
`docker-compose.yml` the line is commented out:

```yaml
      # MUSE_IDENTITY_ISSUER: http://identity:8080
```

Unset, muse uses the production default `https://identity.cafaye.com`, which is not
reachable from a laptop — so the key-set fetch fails and the answer is a 503 naming
nothing helpful. Commented out rather than defaulted to the compose network on purpose:
an issuer that does not exist would fail every request at the fetch, and a *wrong*
issuer that does exist is a worse failure. Set it when your identity is running.

Then the credential. `503` also means every candidate provider failed, and the `detail`
field names which and why — `CredentialUnavailable` is a deploy mistake (no key in the
vault, §6) and `ProviderAuthError` is a rejected key. Read `detail` before touching
anything else; it is written for this.

## 6. Before the first request: a provider credential

A route needs a key for the vendor that serves it, and muse reads it from the encrypted
vault — one row per provider. There is no admin API in v1; this is a psql session:

```sh
MUSE_VAULT_KEY=$MUSE_VAULT_KEY uv run python - <<'PY'
import asyncio, os
from muse.db import PsycopgDatabase
from muse.redaction import Secret
from muse.vault import Vault, load_vault_key

async def main():
    db = await PsycopgDatabase.open(os.environ["MUSE_DATABASE_URL"])
    vault = Vault(db, load_vault_key())
    await vault.put("openai", Secret(input("openai key: ")))

asyncio.run(main())
PY
```

Without a stored key a route answers 503 naming `CredentialUnavailable` for every
candidate — a deploy mistake, reported as one, rather than a 401 from the vendor that
reads like a wrong key.

The tables come from this repository's two migrations, applied once against the
database kit's init script provisioned:

```sh
psql "$MUSE_DATABASE_URL" -f migrations/00001_outbox_events.sql
psql "$MUSE_DATABASE_URL" -f migrations/00002_vault_secrets.sql
```

## 7. What this page has been checked against

- every request and response above is **real output** from a muse built from this
  repository, started with the commands in §3b, against a stubbed OpenAI endpoint and a
  real Postgres;
- the `curl` examples are the ones in this file, run unchanged;
- the Python SDK run used the official `openai` package (`uv run --with openai`), not a
  hand-written stand-in for it;
- the two surprising fields are asserted in `tests/test_alias.py`
  (`temperature` as a number, `stream` refused) and the six-field response shape in
  `test_the_alias_answers_in_openais_response_shape`, which compares by equality so a
  stray key cannot slip through;
- `tests/test_quickstart.py` holds this document to the code: every path it names is in
  `openapi/v1.yaml`, every model name it tells a reader to ask for is in
  `config/routes.yaml`, and the scope and issuer it names are the constants in
  `src/muse/auth.py`. A quickstart that documents an endpoint which does not exist is
  the failure this guards.

**Not verified here:** that `identity` can mint a token this service accepts. It cannot,
today — §3 says why, from `identity`'s own scope vocabulary and its own token format.
That gap is `identity`'s to close and it is recorded in this repository's `cafaye.yml`.
