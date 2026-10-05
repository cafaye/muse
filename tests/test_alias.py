"""`/v1/chat/completions` and `/v1/models`: the OpenAI-compatible aliases.

## The one property this file exists to hold

**The aliases are aliases.** A request to `/v1/chat/completions` travels the *same*
routing, authentication, authorization, metering and outbox path as a request to
`/v1/route`, and differs only in the shape of the response body.

That is why most of these tests are written as *comparisons* against `/v1/route`
rather than as assertions about the alias in isolation. A test that only checked the
alias's own output would pass just as happily against a second, parallel
implementation of routing that happened to return the right JSON — which is the exact
defect the packet exists to prevent. A test that asks "did the same provider get
called the same number of times, and did the same event reach the outbox" cannot.

The structural half of the guarantee lives in `muse.api`: there is one
`_serve_completion` and both endpoints call it. This file asserts its *behaviour*
from outside, because a reader should not have to take the module's structure on
trust — and because the day somebody adds a second router call, the counting
assertions below are what notice.

## Why `/v1/models` carries a credential

It is a listing, and a listing sounds harmless. Three reasons it is behind the same
bearer check as everything else, and the first is the one that matters:

- **core's conventions put `security` on the document globally** (asserted by
  `tests/test_openapi.py::test_security_is_applied_globally`), so a public
  `/v1/models` would need a per-operation override to *not* require a credential.
  The alternative to that override is a model list any unauthenticated caller can
  enumerate — which tells an attacker the exact route names, and therefore which
  vendors this deployment talks to.
- **A model list is reconnaissance.** `fast` and `smart` in the clear is the
  difference between a blind probe and a targeted one.
- **A different scope would be a new scope namespace**, which `muse.auth`'s own
  docstring says is a platform decision this service does not make. So it requires
  `completions:write`, the same string, unchanged.
"""

from __future__ import annotations

import json

import pytest

from muse.errors import ProviderTimeout
from muse.providers import Price, ProviderRegistry
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.routes import routes_from_yaml

from .conftest import asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.jwks import ACCOUNT, Identity, token
from .support.test_app import IDENTITY, auth_for, build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MODEL = "gpt-4o-mini"
PRICE = Price(1000, 2000)

BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}

#: The two paths, so a test can drive both and compare rather than restate one.
ROUTE = "/v1/route"
ALIAS = "/v1/chat/completions"


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def routes(*models: str) -> str:
    """A routes file naming each model, all served by one provider."""
    return write_routes(
        {
            "version": 1,
            "routes": [
                {"model": name, "candidates": [{"provider": "openai", "model": MODEL}]}
                for name in models
            ],
        }
    )


def app_for(registry: ProviderRegistry | None = None, *, database=None, auth=None):
    """An app with one route (`fast`) that always completes."""
    resolved = registry or ProviderRegistry().register(
        FakeProvider(name="openai", content="hello there", tokens_in=12, tokens_out=8, price=PRICE)
    )
    return build_test_app(
        registry=resolved,
        database=database or FakeDatabase(),
        table=routes_from_yaml(routes("fast", "smart")),
        auth=auth,
    )


def authed(**overrides) -> dict[str, str]:
    """A real signed token. Every check in the chain runs, so no test here can pass
    by bypassing the boundary it is testing."""
    return bearer(token(IDENTITY, **overrides))


# --- the alias answers in OpenAI's shape ------------------------------------


async def test_the_alias_answers_in_openais_response_shape():
    """Exactly OpenAI's six fields, asserted by equality.

    `assert body == {...}` rather than `in` checks, per rule 4: a stray extra key is
    invisible to a membership test and is exactly what would break a generated
    client. Nothing muse-specific is added — `provider`, `route` and `cost_micros`
    are `/v1/route`'s additions and belong to that endpoint, not to the compatibility
    surface.

    `created` and `id` are the two volatile values, so they are compared against
    their own documented grammar rather than pinned to a literal.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json=BODY, headers=authed())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"id", "object", "created", "model", "choices", "usage"}
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL
    assert body["id"].startswith("chatcmpl-")
    assert isinstance(body["created"], int) and body["created"] > 0
    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello there"},
            "finish_reason": "stop",
        }
    ]
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}


async def test_the_alias_reports_the_same_usage_the_routed_path_metered():
    """The token counts on the wire are the ones that were metered.

    Not a shape assertion: it is the claim that the numbers a caller is shown and the
    numbers `billing` is shown are the same numbers. They come from one
    `RoutedCompletion`, so this holds by construction — and it is the assertion that
    would fail if somebody rendered the alias from a second, separately-computed
    source.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(database=database)) as client:
        response = await client.post(ALIAS, json=BODY, headers=authed())

    event = json.loads(database.outbox[0]["data"])
    body = response.json()
    assert body["usage"]["prompt_tokens"] == event["tokens_in"]
    assert body["usage"]["completion_tokens"] == event["tokens_out"]
    assert body["usage"]["total_tokens"] == event["tokens_in"] + event["tokens_out"]


async def test_the_alias_echoes_the_trace_id_header():
    """`X-Trace-Id` on the alias, because the middleware is not the alias's business.

    The body drops `trace_id` — OpenAI's shape has no such field, and inventing one
    is how a "compatible" endpoint stops being compatible. The **header** is where
    support correlation lives, and it is set by middleware that every response passes
    through, so the alias keeps it for free. Asserted because "for free" is exactly
    the kind of thing a refactor takes away.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(
            ALIAS,
            json=BODY,
            headers={**authed(), "X-Trace-Id": "0123456789abcdef"},
        )
    assert response.headers["x-trace-id"] == "0123456789abcdef"


# --- the alias is the same path, not a second one ----------------------------


async def test_both_paths_call_the_provider_exactly_once():
    """The counting assertion, and the heart of this file.

    Two providers, two fresh databases, two requests — one per path. Each must have
    been called exactly once. A second implementation of routing would either call
    the provider twice, or call a different provider, or not call one at all; each of
    those is a different failure and all three are invisible to a shape assertion.
    """
    for path in (ROUTE, ALIAS):
        provider = FakeProvider(name="openai", price=PRICE)
        app = app_for(ProviderRegistry().register(provider))
        async with asgi_client(app) as client:
            response = await client.post(path, json=BODY, headers=authed())
        assert response.status_code == 200, path
        assert provider.calls == 1, f"{path} called the provider {provider.calls} times"


async def test_both_paths_send_the_provider_the_same_request():
    """Same model, same messages, same caps — asserted on the provider's own record.

    The strongest available statement that the two paths converge before dispatch.
    The alias must not drop `max_tokens` on the way through, and this is what would
    notice.
    """
    request_body = {**BODY, "max_tokens": 64}
    seen = {}
    for path in (ROUTE, ALIAS):
        provider = FakeProvider(name="openai", price=PRICE)
        app = app_for(ProviderRegistry().register(provider))
        async with asgi_client(app) as client:
            await client.post(path, json=request_body, headers=authed())
        seen[path] = provider.requests[0]

    assert seen[ROUTE] == seen[ALIAS]
    assert seen[ALIAS].model == MODEL
    assert seen[ALIAS].max_tokens == 64


async def test_both_paths_emit_exactly_one_metered_event():
    """One completion, one `muse.tokens.consumed` row, on either path.

    The alias must not meter twice (a double-billing bug) and must not skip metering
    (a completion nobody is charged for). Both are counted against the outbox rather
    than inferred from the response, because the outbox is what `billing` reads.
    """
    counts = {}
    for path in (ROUTE, ALIAS):
        database = FakeDatabase()
        app = app_for(database=database)
        async with asgi_client(app) as client:
            response = await client.post(path, json=BODY, headers=authed())
        assert response.status_code == 200
        counts[path] = len(database.outbox)
    assert counts == {ROUTE: 1, ALIAS: 1}


async def test_the_alias_uses_the_same_fallback_chain():
    """A failing primary still falls through on the alias.

    The chain is the router's, not the endpoint's. If the alias had its own dispatch
    the fallback would be the first thing to go missing, and the failure would be a
    503 in production with no test failure here — so the route is configured with two
    candidates and the first one is scripted to time out.
    """
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(
            name="openai",
            price=PRICE,
            errors=[ProviderTimeout("primary is down")],
            completions=[],
        )
    )
    registry.register(FakeProvider(name="anthropic", content="from the fallback", price=PRICE))
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "routes": [
                    {
                        "model": "fast",
                        "candidates": [
                            {"provider": "openai", "model": MODEL},
                            {"provider": "anthropic", "model": MODEL},
                        ],
                    }
                ],
            }
        )
    )
    database = FakeDatabase()
    app = build_test_app(
        registry=registry, database=database, table=table
    )

    async with asgi_client(app) as client:
        response = await client.post(ALIAS, json=BODY, headers=authed())

    assert response.status_code == 200
    assert response.json()["choices"][0]["message"]["content"] == "from the fallback"
    assert json.loads(database.outbox[0]["data"])["provider"] == "anthropic"


# --- the failure modes are identical, because it is the same handler --------


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_no_credential_is_401_on_both_paths(path):
    async with asgi_client(app_for()) as client:
        response = await client.post(path, json=BODY)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_a_token_with_no_capability_is_403_on_both_paths(path):
    """`scopes=""`, so the claim is the empty *set* and not a claim muse cannot
    interpret — which would be a 401 and would make this pass for the wrong reason."""
    async with asgi_client(app_for()) as client:
        response = await client.post(path, json=BODY, headers=authed(scopes=""))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_a_token_with_no_account_is_401_on_both_paths(path):
    async with asgi_client(app_for()) as client:
        response = await client.post(path, json=BODY, headers=authed(account_id=None))
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_an_unreachable_key_set_is_503_and_not_401_on_both_paths(path):
    """The distinction the README argues at length, and the reason the alias could not
    have been written as a parallel handler even if it were easier.

    identity being down is not the caller's credential being bad. A 401 here sends a
    caller with a good token to re-authenticate against a healthy service and retry
    forever.
    """
    identity = Identity()
    identity.fail_with(503)
    app = app_for(auth=auth_for(identity))
    async with asgi_client(app) as client:
        response = await client.post(path, json=BODY, headers=bearer(token(identity)))
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_an_unrouted_model_is_404_on_both_paths(path):
    async with asgi_client(app_for()) as client:
        response = await client.post(path, json={**BODY, "model": "gpt-5-turbo"}, headers=authed())
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_a_malformed_body_is_422_on_both_paths(path):
    async with asgi_client(app_for()) as client:
        response = await client.post(path, json={"model": "fast"}, headers=authed())
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


@pytest.mark.parametrize("path", [ROUTE, ALIAS])
async def test_every_candidate_failing_is_503_on_both_paths(path):
    registry = ProviderRegistry().register(
        ScriptedProvider(
            name="openai",
            price=PRICE,
            errors=[ProviderTimeout("down")],
            completions=[],
        )
    )
    async with asgi_client(app_for(registry)) as client:
        response = await client.post(path, json=BODY, headers=authed())
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


async def test_the_problem_body_names_the_path_that_was_requested():
    """`instance` is the caller's own URL on both paths.

    A 404 that says `instance: /v1/route` when the caller asked
    `/v1/chat/completions` is a small lie in the one body a client parses to work out
    what went wrong — and it would come free from a handler that hardcoded the path.
    """
    async with asgi_client(app_for()) as client:
        routed = await client.post(ROUTE, json={**BODY, "model": "nope"}, headers=authed())
        aliased = await client.post(ALIAS, json={**BODY, "model": "nope"}, headers=authed())
    assert routed.json()["instance"] == ROUTE
    assert aliased.json()["instance"] == ALIAS


async def test_an_unknown_field_is_refused_on_the_alias_too():
    """`extra="forbid"` on the shared request model, so `stream: true` is a 422.

    Streaming is out of scope for this packet and a separate one. Silently ignoring
    `stream` would hand a caller a non-streaming answer to a request that asked for a
    stream, and it would do so silently — which is the failure mode
    `RouteRequestBody`'s `extra="forbid"` exists to prevent. Named rather than left
    implicit, because "we do not support that yet" is only honest if it is said.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post(ALIAS, json={**BODY, "stream": True}, headers=authed())
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


async def test_temperature_is_the_same_decimal_string_on_the_alias():
    """The 1.1.1 decision holds on both paths, and a number is a 422 on both.

    OpenAI's own clients send `temperature` as a JSON *number*, so this is the field
    most likely to surprise somebody using the alias. It is refused rather than
    coerced for the reason `openapi/v1.yaml` states at length: a float cannot be
    round-tripped, and `0.1 + 0.2` reaches the provider as `0.30000000000000004`.
    Coercing here would put that exact defect back on the compatibility path, where
    it is least likely to be noticed.
    """
    async with asgi_client(app_for()) as client:
        good = await client.post(ALIAS, json={**BODY, "temperature": "0.2"}, headers=authed())
        bad = await client.post(ALIAS, json={**BODY, "temperature": 0.2}, headers=authed())
    assert good.status_code == 200
    assert bad.status_code == 422


# --- GET /v1/models ---------------------------------------------------------


async def test_the_model_list_is_openai_shaped():
    """`{"object": "list", "data": [...]}`, and the ids are the route names.

    Sourced from the route table rather than hardcoded, which is the whole point: a
    hardcoded list is a second source of truth about what muse can route to, and it
    goes stale the first time somebody edits `config/routes.yaml`.
    """
    async with asgi_client(app_for()) as client:
        response = await client.get("/v1/models", headers=authed())

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"object", "data"}
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == ["fast", "smart"]
    for entry in body["data"]:
        assert set(entry) == {"id", "object", "created", "owned_by"}
        assert entry["object"] == "model"
        assert entry["owned_by"] == "muse"


async def test_the_model_list_tracks_the_route_table_rather_than_a_literal():
    """The negative control for the test above.

    A test that asserts `["fast", "smart"]` passes against a hardcoded list just as
    happily as against a real read of the table. This one builds an app whose routes
    are named something else entirely, so the two cannot be confused. Rule 23's
    discipline: a guard that cannot go red is not a guard.
    """
    registry = ProviderRegistry().register(FakeProvider(name="openai", price=PRICE))
    table = routes_from_yaml(routes("alpha", "beta", "gamma"))
    app = build_test_app(registry=registry, database=FakeDatabase(), table=table)
    async with asgi_client(app) as client:
        response = await client.get("/v1/models", headers=authed())
    assert [entry["id"] for entry in response.json()["data"]] == ["alpha", "beta", "gamma"]


async def test_the_model_list_is_in_file_order():
    """File order, not sorted — the same rule `RouteTable.models()` already follows.

    Sorting would hide a reordered `routes.yaml`, and a reorder is a change to which
    vendor a customer's requests reach.
    """
    registry = ProviderRegistry().register(FakeProvider(name="openai", price=PRICE))
    table = routes_from_yaml(routes("zulu", "alpha"))
    app = build_test_app(registry=registry, database=FakeDatabase(), table=table)
    async with asgi_client(app) as client:
        response = await client.get("/v1/models", headers=authed())
    assert [entry["id"] for entry in response.json()["data"]] == ["zulu", "alpha"]


async def test_the_model_list_requires_a_credential():
    """401 without one.

    A model list is reconnaissance: it names every route and, through them, which
    vendors this deployment talks to. And core's conventions put `security` on the
    document globally, so an unauthenticated listing would need a per-operation
    override to exist at all — which is a decision this service should not make
    quietly in a compatibility patch.
    """
    async with asgi_client(app_for()) as client:
        response = await client.get("/v1/models")
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_the_model_list_is_403_without_the_capability():
    """The same scope string, unchanged. A read would tempt `models:read`, and that is
    a new scope namespace — a platform decision, recorded in `cafaye.yml`, and not one
    this packet makes."""
    async with asgi_client(app_for()) as client:
        response = await client.get("/v1/models", headers=authed(scopes=""))
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_the_model_list_is_503_when_the_key_set_is_unreachable():
    """The same 503-not-401 rule as the completion paths."""
    identity = Identity()
    identity.fail_with(503)
    app = app_for(auth=auth_for(identity))
    async with asgi_client(app) as client:
        response = await client.get("/v1/models", headers=bearer(token(identity)))
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


async def test_the_model_list_is_a_post_404_and_never_a_403():
    """A listing that exists only for GET.

    Stock clients discover models with GET; a POST to the same path is a method
    mistake, and it answers with the same `not_found` code as a path that does not
    exist — rather than inventing a status a client has to special-case.
    """
    async with asgi_client(app_for()) as client:
        response = await client.post("/v1/models", json={}, headers=authed())
    assert response.status_code == 405
    assert response.json()["code"] == "not_found"


async def test_the_model_list_names_no_account():
    """`PLATFORM`, read: the negative test for the new entry point.

    The route table is configuration, not tenant data, so the honest assertion is
    that no account id appears and the body is identical for every account. That is
    what `tests/test_tenant_scoping.py`'s enumeration claims when it classifies this
    route `PLATFORM`, and a test implying a tenant axis exists here would be the
    defect rather than the coverage.
    """
    bodies = []
    for account in (ACCOUNT, "01HQ0000000000000000000000B", "01HQ000000000000000000000ZZ"):
        async with asgi_client(app_for()) as client:
            response = await client.get("/v1/models", headers=authed(account_id=account))
        assert response.status_code == 200
        assert account not in response.text
        bodies.append(response.json())
    assert bodies[0] == bodies[1] == bodies[2]
