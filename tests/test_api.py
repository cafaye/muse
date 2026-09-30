"""`POST /v1/route` — the HTTP surface.

Four things are under test, and the fourth is the one that is easy to skip and
impossible to add later:

1. **The success shape**, asserted by exact equality. Not `in` checks — a stray new
   key in a completion response is a contract change, and the SDKs generated from
   this document will not care.
2. **`application/problem+json` on every non-2xx**, with `code` from core's reserved
   list and `trace_id` matching the `X-Trace-Id` header. A service that invents its
   own error shape is a service that needs a bespoke SDK.
3. **The auth stub.** Header presence and a `Bearer` scheme, nothing more. The
   distinction is stated in the test names: this checks that a request *reaches* the
   endpoint, and a test that asserted the token was validated would be asserting a
   contract that does not exist yet.
4. **Metering happens on the success path, inside the handler.** A completion that
   is returned without a metered event is a completion whose spend is lost, and the
   only place that can be caught is here.

The app is built per test with a container of fakes, so no test opens a socket
(AGENTS.md rule 3) and no test can reach a real provider.
"""

from __future__ import annotations

import pytest

from muse.providers import Price, ProviderRegistry
from muse.providers.credentials import StaticCredentials
from muse.providers.fake import ScriptedProvider
from muse.redaction import Secret
from muse.routes import routes_from_yaml

from .conftest import AUTH_HEADERS, asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.test_app import build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MODEL = "gpt-4o-mini"
PRICE = Price(150, 600)
BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}


def completion(**overrides):
    from muse.providers import Completion

    return Completion(
        **{
            "provider": "openai",
            "model": MODEL,
            "content": "hello there",
            "tokens_in": 12,
            "tokens_out": 8,
            "price": PRICE,
            **overrides,
        }
    )


def app_for(*providers, database: FakeDatabase | None = None, **route_fields):
    """An app wired to fakes.

    `route_fields` go into the one route's `Route`, so a test can state a retry
    policy without rebuilding the table.
    """
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return build_test_app(
        registry=registry,
        database=database or FakeDatabase(),
        table=routes_from_yaml(
            write_routes(
                {
                    "version": 1,
                    "routes": [
                        {
                            "model": "fast",
                            "candidates": [{"provider": p.name, "model": MODEL} for p in providers],
                            **route_fields,
                        }
                    ],
                }
            )
        ),
    )


def serving() -> ScriptedProvider:
    return ScriptedProvider(name="openai", price=PRICE, completions=(completion(),))


# --- the success shape -----------------------------------------------------


async def test_a_routed_request_returns_a_completion() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 200


async def test_the_completion_body_has_exactly_the_documented_shape() -> None:
    """Exact equality. A stray key is a contract change, and every SDK generated from
    `openapi/v1.yaml` will carry it forever."""
    async with asgi_client(app_for(serving())) as client:
        body = (await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)).json()

    assert set(body) == {
        "id",
        "object",
        "created",
        "model",
        "provider",
        "route",
        "choices",
        "usage",
        "cost_micros",
        "trace_id",
    }
    assert body["object"] == "chat.completion"
    assert body["model"] == MODEL
    assert body["provider"] == "openai"
    assert body["route"] == "fast"
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    assert body["cost_micros"] == 7


async def test_the_choice_carries_the_content_and_the_finish_reason() -> None:
    async with asgi_client(app_for(serving())) as client:
        body = (await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)).json()

    assert body["choices"] == [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "hello there"},
            "finish_reason": "stop",
        }
    ]


async def test_a_truncated_completion_reports_its_finish_reason() -> None:
    """`length` means the model was cut off. A caller that cannot tell that from `stop`
    will treat a truncated answer as a complete one."""
    provider = ScriptedProvider(
        name="openai", price=PRICE, completions=(completion(finish_reason="length"),)
    )
    async with asgi_client(app_for(provider)) as client:
        body = (await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)).json()
    assert body["choices"][0]["finish_reason"] == "length"


async def test_the_response_carries_a_trace_id() -> None:
    async with asgi_client(app_for(serving())) as client:
        body = (await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)).json()
    assert body["trace_id"]


async def test_the_response_echoes_the_trace_id_header() -> None:
    """core's checklist: the trace id is in the header *and* the body, and support
    starts from it. A body that disagrees with the header sends someone to the wrong
    log line."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.headers["X-Trace-Id"] == response.json()["trace_id"]


async def test_an_incoming_trace_id_is_echoed_rather_than_replaced() -> None:
    """`guard` and the SDKs propagate the id across services, so muse has to keep the
    one it was given or a trace stops at the edge."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json=BODY, headers={**AUTH_HEADERS, "X-Trace-Id": "abc123def456"}
        )
    assert response.headers["X-Trace-Id"] == "abc123def456"
    assert response.json()["trace_id"] == "abc123def456"


async def test_a_hostile_trace_id_is_replaced_rather_than_echoed() -> None:
    """An attacker-controlled header that lands in a log line is a log-injection
    vector. Anything that is not a short opaque token is replaced, not sanitised —
    sanitising is a filter with a bypass, and a fresh id has nothing to bypass."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json=BODY, headers={**AUTH_HEADERS, "X-Trace-Id": "a\nb injected"}
        )
    assert response.headers["X-Trace-Id"] != "a\nb injected"
    assert len(response.headers["X-Trace-Id"]) == 32


async def test_a_short_trace_id_is_accepted() -> None:
    """Eight characters is the floor, so a caller generating ids as short as uuid4().hex[:8]
    is not silently disconnected from its own trace."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json=BODY, headers={**AUTH_HEADERS, "X-Trace-Id": "abcdefgh"}
        )
    assert response.headers["X-Trace-Id"] == "abcdefgh"


async def test_the_optional_parameters_reach_the_provider() -> None:
    provider = serving()
    async with asgi_client(app_for(provider)) as client:
        await client.post(
            "/v1/route", json={**BODY, "max_tokens": 64, "temperature": 0.3}, headers=AUTH_HEADERS
        )
    assert provider.requests[0].max_tokens == 64
    assert provider.requests[0].temperature == 0.3


async def test_a_multi_turn_conversation_reaches_the_provider_in_order() -> None:
    """Order is data. Reordered, it produces a confident wrong answer rather than an
    error, so the order is asserted end to end."""
    provider = serving()
    async with asgi_client(app_for(provider)) as client:
        await client.post(
            "/v1/route",
            json={
                "model": "fast",
                "messages": [
                    {"role": "system", "content": "be brief"},
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
            },
            headers=AUTH_HEADERS,
        )
    assert [m.role for m in provider.requests[0].messages] == ["system", "user", "assistant"]


# --- metering on the success path ------------------------------------------


async def test_a_served_completion_is_metered() -> None:
    """The property the whole service exists for. A 200 with no outbox row is spend
    that is never billed, and nothing downstream would notice."""
    database = FakeDatabase()
    async with asgi_client(app_for(serving(), database=database)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 200
    assert len(database.outbox) == 1


async def test_the_metered_event_reports_the_vendor_that_served() -> None:
    """A fallback that fires must be billed to the vendor that ran, or that vendor's
    invoice and the customer's bill disagree and neither side can see the other's
    number."""
    import json

    from muse.errors import ProviderTimeout

    database = FakeDatabase()
    primary = ScriptedProvider(name="openai", price=PRICE, errors=(ProviderTimeout("slow"),))
    fallback = ScriptedProvider(
        name="anthropic",
        price=PRICE,
        completions=(completion(provider="anthropic"),),
    )
    async with asgi_client(app_for(primary, fallback, database=database)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)

    assert response.json()["provider"] == "anthropic"
    assert json.loads(database.outbox[0]["data"])["provider"] == "anthropic"


async def test_a_metering_failure_does_not_return_a_success() -> None:
    """The completion was paid for. Returning it anyway loses the record silently; an
    error makes the loss visible. This is the one place the endpoint could have been
    made convenient, and it must not be.

    `internal`, not `unavailable`: 503 means the models are down and a retry is
    reasonable, while a failed write to our own store is a defect somebody has to look
    at. And no completion content is returned either way — the caller cannot act on a
    completion that was never metered, so handing it over would be worse than useless.
    """
    database = FakeDatabase()
    database.fail_next = RuntimeError("database is down")
    async with asgi_client(app_for(serving(), database=database)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 500
    assert response.json()["code"] == "internal"
    assert "hello there" not in response.text
    assert "database is down" not in response.text


# --- errors ----------------------------------------------------------------


async def test_a_request_without_a_credential_is_unauthorized() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_an_unauthorized_body_is_problem_json() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY)
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "https://errors.cafaye.com/unauthorized"
    assert response.json()["status"] == 401
    assert response.json()["instance"] == "/v1/route"


async def test_a_non_bearer_scheme_is_unauthorized() -> None:
    """`Authorization: Basic ...` is not a cafaye credential. core allows bearer JWTs
    only, and accepting any scheme would make a misconfigured client look
    authenticated."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json=BODY, headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
    assert response.status_code == 401


async def test_a_bearer_scheme_with_no_token_is_unauthorized() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY, headers={"Authorization": "Bearer"})
    assert response.status_code == 401


async def test_an_unauthorized_response_still_carries_a_trace_id() -> None:
    """The 401 is the response an operator is most likely to be looking at. A trace id
    only on success would mean the one error worth grepping for has none."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post("/v1/route", json=BODY)
    assert response.json()["trace_id"] == response.headers["X-Trace-Id"]


async def test_an_unrouted_model_is_not_found() -> None:
    """404, not 503. A request naming a model nothing routes is a caller mistake with
    a caller-fixable answer, and the status code is what tells a client which it is."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json={**BODY, "model": "nonexistent"}, headers=AUTH_HEADERS
        )
    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_an_unknown_role_is_a_validation_failure() -> None:
    """422, and with the field named. The provider would answer 400 with its own
    wording; naming the field is the difference between a one-line fix and a
    round trip to whoever wrote the caller."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"role": "captain", "content": "hi"}]},
            headers=AUTH_HEADERS,
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert "role" in response.json()["detail"]


async def test_an_empty_message_list_is_a_validation_failure() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json={"model": "fast", "messages": []}, headers=AUTH_HEADERS
        )
    assert response.status_code == 422


async def test_a_missing_model_field_is_a_validation_failure() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route",
            json={"messages": [{"role": "user", "content": "hi"}]},
            headers=AUTH_HEADERS,
        )
    assert response.status_code == 422


async def test_every_candidate_failing_is_unavailable() -> None:
    """503, not 500. muse is up; the models are not. The distinction is what a
    caller's retry logic branches on."""
    from muse.errors import ProviderTimeout

    provider = ScriptedProvider(name="openai", price=PRICE, errors=(ProviderTimeout("down"),))
    async with asgi_client(app_for(provider)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


async def test_the_unavailable_detail_names_the_providers_that_were_tried() -> None:
    """ "it failed" is not actionable. "openai timed out" is the first line of the
    investigation, and the router already knows it."""
    from muse.errors import ProviderTimeout

    provider = ScriptedProvider(name="openai", price=PRICE, errors=(ProviderTimeout("slow"),))
    async with asgi_client(app_for(provider)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert "openai" in response.json()["detail"]


async def test_an_unavailable_detail_never_contains_a_credential() -> None:
    """The failure chain reaches a response body. A provider that echoes the key it
    rejected would otherwise put a live credential in front of whoever reads it."""
    from muse.errors import ProviderAuthError

    provider = ScriptedProvider(
        name="openai",
        price=PRICE,
        errors=(ProviderAuthError("Incorrect API key provided: sk-live-SECRET"),),
    )
    app = build_test_app(
        registry=ProviderRegistry().register(provider),
        database=FakeDatabase(),
        table=routes_from_yaml(
            write_routes(
                {
                    "version": 1,
                    "routes": [
                        {"model": "fast", "candidates": [{"provider": "openai", "model": MODEL}]}
                    ],
                }
            )
        ),
        credentials=StaticCredentials({"openai": Secret("sk-live-SECRET")}),
        scrub_secrets=(Secret("sk-live-SECRET"),),
    )
    async with asgi_client(app) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert "sk-live-SECRET" not in response.text


async def test_an_unexpected_error_is_an_internal_problem() -> None:
    """A bug in muse must not leak its type or its message to a caller. The trace id
    is the handle; the log line is where the detail lives."""
    provider = serving()
    provider.name = "openai"

    async def boom(request):
        raise ZeroDivisionError("a bug in muse")

    provider.complete = boom
    async with asgi_client(app_for(provider)) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 500
    assert response.json()["code"] == "internal"
    assert "ZeroDivisionError" not in response.text
    assert "a bug in muse" not in response.text


# --- the endpoint's own surface --------------------------------------------


async def test_the_path_is_versioned() -> None:
    """core: every path is prefixed, and `/v1` is never mutated in place."""
    async with asgi_client(app_for(serving())) as client:
        assert (await client.post("/route", json=BODY, headers=AUTH_HEADERS)).status_code == 404


async def test_get_is_not_allowed() -> None:
    """The completion is not idempotent — it costs money — so a GET that a browser or
    a crawler could make is not something this surface offers."""
    async with asgi_client(app_for(serving())) as client:
        assert (await client.get("/v1/route", headers=AUTH_HEADERS)).status_code == 405


async def test_a_malformed_body_is_a_validation_failure() -> None:
    """422, not 400, and the reason is core's own table: `validation_failed` is a
    documented 422, so a 400 carrying that code would be a code whose status
    disagrees with the reserved list. There is no reserved 400 code, and inventing one
    is the thing the list exists to prevent. `detail` says the body did not parse, so
    the caller knows which of the two it was."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route",
            content=b"{not json",
            headers={**AUTH_HEADERS, "Content-Type": "application/json"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"


async def test_a_message_with_no_role_is_a_validation_failure() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"content": "hi"}]},
            headers=AUTH_HEADERS,
        )
    assert response.status_code == 422


async def test_a_message_with_no_content_is_a_validation_failure() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route",
            json={"model": "fast", "messages": [{"role": "user"}]},
            headers=AUTH_HEADERS,
        )
    assert response.status_code == 422


async def test_a_negative_max_tokens_is_a_validation_failure() -> None:
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json={**BODY, "max_tokens": -5}, headers=AUTH_HEADERS
        )
    assert response.status_code == 422


async def test_an_unknown_body_field_is_rejected() -> None:
    """core closes request bodies like it closes schemas. A typo'd `max_token` that is
    ignored leaves a caller believing they capped the response when they did not."""
    async with asgi_client(app_for(serving())) as client:
        response = await client.post(
            "/v1/route", json={**BODY, "max_token": 64}, headers=AUTH_HEADERS
        )
    assert response.status_code == 422


async def test_the_probe_endpoints_are_untouched() -> None:
    """Adding a route must not disturb liveness. If `/healthz` ever consults a
    dependency, a database outage restarts every container."""
    async with asgi_client(app_for(serving())) as client:
        assert (await client.get("/healthz")).json() == {"status": "ok"}


async def test_readiness_reports_the_database_when_one_is_configured() -> None:
    """The `db` slot was reserved for exactly this. A readiness body that says
    `skipped` while the vault is wired to a database is a body nobody trusts."""
    async with asgi_client(app_for(serving())) as client:
        body = (await client.get("/readyz")).json()
    assert body["checks"]["db"] == "ok"


async def test_readiness_reports_a_failing_database() -> None:
    """`degraded`, not `unavailable`. muse can still answer `/v1/route` for a route
    whose provider needs no database read, so a vault outage is a degraded service
    rather than a down one — and the distinction is what an orchestrator acts on."""
    database = FakeDatabase()
    database.fail_next = RuntimeError("connection refused")

    async with asgi_client(app_for(serving(), database=database)) as client:
        body = (await client.get("/readyz")).json()

    assert body["status"] == "degraded"
    assert body["checks"]["db"] == "error"


async def test_two_apps_do_not_share_a_container() -> None:
    """The isolation guarantee at the level it now matters. Two apps in one process
    must not see each other's providers or databases, or a test's registration leaks
    into the next one and the suite becomes order-dependent — the exact failure
    `tests/test_app_factory.py` exists to catch, now with state behind it."""
    first, second = app_for(serving()), app_for(serving())
    assert first.state.container is not second.state.container
    assert first.state.container.database is not second.state.container.database

    async with asgi_client(first) as ca, asgi_client(second) as cb:
        ra = await ca.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
        rb = await cb.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
    assert ra.status_code == rb.status_code == 200
    assert len(first.state.container.database.outbox) == 1
    assert len(second.state.container.database.outbox) == 1
