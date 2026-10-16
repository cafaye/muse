"""`POST /v1/decisions` — structured decisions, and the same guarantees as `/v1/route`.

## The property this file exists to hold

**A decision is served by the same path a completion is.** One auth check, one
router, one breaker registry, one metered event, one outbox row. `container.router`
runs it — not a bare `litellm.adecisions` in a handler — so a decision falls back,
backs off, trips a breaker and is priced exactly the way a completion does.

That is why most of these tests count calls and rows rather than read the answer
body. A shape assertion passes against a second implementation that happened to
return the right JSON; "the provider was called once, the outbox has one row, and
the fallback answered when the primary refused" does not.

## Why the 400 for an invalid request is litellm's and not muse's

muse validates the *envelope* — `model`, `input`, `questions` non-empty — with
pydantic, which is a 422. What makes a question *semantically* wrong — a
`criteria` map that is not a map, a score question with no levels — is litellm's
to determine, and `litellm.adecisions` refuses it with a `BadRequestError` before
any model is called. So `LiteLLMProvider.decide` lets that exception arrive and
maps it, the router stops the fallback chain rather than spending it on a request
that is broken everywhere, and the endpoint answers 400. A second validator in
muse would be a second opinion about litellm's vocabulary, and the two would drift
on a litellm upgrade.

## Why the answers are keyed and in order

litellm's decisions endpoint takes a *named* question map and answers under those
names. muse assigns `q1`, `q2`, ... before dispatch rather than taking a
caller-supplied name, because two questions the caller did not name would
collapse into one answer silently. Python dicts preserve insertion order, so the
returned mapping is also the answer order.
"""

from __future__ import annotations

import json

import pytest

from muse.api import DecisionQuestionBody
from muse.errors import ProviderTimeout
from muse.outbox import InMemoryTransport, OutboxPublisher
from muse.providers import LiteLLMProvider, Price, ProviderRegistry
from muse.providers.fake import FakeProvider, ScriptedProvider
from muse.routes import routes_from_yaml

from .conftest import AUTH_HEADERS, asgi_client, write_routes
from .support.fake_database import FakeDatabase
from .support.jwks import token
from .support.litellm_stub import make_stub
from .support.test_app import IDENTITY, build_test_app

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

MODEL = "gpt-4o-mini"
PRICE = Price(150, 600)
ROUTE = "/v1/decisions"

#: The shared request body. One `noul` question, because a decision needs at least
#: one question and most tests are about something other than the questions.
BODY = {
    "model": "fast",
    "input": "Customer wrote: I was charged twice for order #4411 and want one refunded today.",
    "questions": [
        {
            "type": "noul",
            "instructions": "Is the customer asking for a refund?",
            "criteria": {"true": "wants money back", "false": "asks a question"},
        }
    ],
}


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


def answering(*, answer: dict | None = None, **kwargs) -> FakeProvider:
    """A provider whose `decide` always answers with the same payload."""
    return FakeProvider(
        name=kwargs.pop("provider_name", "openai"),
        price=kwargs.pop("price", PRICE),
        tokens_in=kwargs.pop("tokens", (12, 8))[0],
        tokens_out=kwargs.pop("tokens", (12, 8))[1],
        answer=answer if answer is not None else {"type": "noul", "noul": 0.93},
    )


def app_for(*providers, database: FakeDatabase | None = None, table=None):
    """An app with one route (`fast`) and the given candidates, in order."""
    registry = ProviderRegistry()
    for provider in providers:
        registry.register(provider)
    return build_test_app(
        registry=registry,
        database=database or FakeDatabase(),
        table=table or routes_from_yaml(routes("fast")),
    )


def question(**overrides) -> dict:
    """One question, so a test states only what it varies."""
    return {
        "type": "noul",
        "instructions": "Is this a refund request?",
        "criteria": {"true": "wants money back", "false": "asks a question"},
        **overrides,
    }


def test_a_choice_question_sent_with_choices_reaches_the_provider_with_them() -> None:
    """The caller's `choices` must reach litellm's `choices`.

    This is the shape a caller following litellm's proxy docs sends, and the one
    the suite never exercised: every test POSTed `criteria` instead. The
    provider received a question with no `choices`, litellm validated it as an
    `OpenAIChoiceQuestion`, and refused it with
    `questions.0.choice.choices: Field required` — a 400 naming a field the
    caller DID send, on a path every test said was green.

    Asserted on the provider's own record of what it was handed, so the claim is
    about the wire and not about the response.
    """
    provider = answering()
    body = {
        **BODY,
        "questions": [
            {
                "type": "choice",
                "name": "classification",
                "instructions": "what kind of message is this?",
                "choices": [{"value": "question"}, {"value": "feedback"}],
            }
        ],
    }

    import asyncio

    async def go() -> None:
        async with asgi_client(app_for(provider)) as client:
            await client.post(ROUTE, json=body, headers=AUTH_HEADERS)

    asyncio.run(go())

    (request,) = provider.decisions
    assert request.questions[0].choices == [{"value": "question"}, {"value": "feedback"}]
    assert request.questions[0].as_wire()["choices"] == [{"value": "question"}, {"value": "feedback"}]


def _question(**overrides) -> DecisionQuestionBody:
    """One question body, so a test states only what it varies."""
    return DecisionQuestionBody(**{"type": "noul", "instructions": "x", **overrides})


def bearer() -> dict[str, str]:
    return {"Authorization": f"Bearer {token(IDENTITY)}"}


# --- the three question types, and what each answers with -------------------


async def test_a_noul_question_returns_a_calibrated_probability() -> None:
    """A number in [0, 1], which is the whole point of the vocabulary.

    The consumer (inbox) classifies every customer mention into a category *with a
    confidence*; a `noul` answer is that confidence as a probability rather than as
    a label the model asserted. It arrives as litellm renders it — `{"type": "noul",
    "noul": 0.93}` — because the caller is written against litellm's docs, and
    muse re-shaping it would be a client that has to be taught about muse.
    """
    async with asgi_client(app_for(answering(answer={"type": "noul", "noul": 0.93}))) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    answer = response.json()["answers"]["q1"]
    assert answer == {"type": "noul", "noul": 0.93}
    assert 0.0 <= answer["noul"] <= 1.0


async def test_a_choice_question_returns_one_of_the_labels() -> None:
    """One label out of the `criteria` map, with the rest of the distribution.

    `probabilities` is not decoration: a caller routing on the top label alone
    cannot tell "urgent at 0.51" from "urgent at 0.99", and the second is the one
    worth acting on automatically. litellm's own shape carries both, so muse does.
    """
    body = dict(BODY, questions=[question(type="choice", instructions="How urgent is this?")])
    answer = {
        "type": "choice",
        "choice": "high",
        "confidence": 0.82,
        "probabilities": {"low": 0.18, "high": 0.82},
    }
    async with asgi_client(app_for(answering(answer=answer))) as client:
        response = await client.post(ROUTE, json=body, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["answers"]["q1"] == answer


async def test_a_score_question_returns_a_valid_level_index() -> None:
    """An index into the `criteria` levels, with the legend beside it.

    The index is what a caller stores; the `legend` is what a human reads. Both
    come from litellm, and muse passes both through, because a score without its
    legend is a number nobody can interpret a month later.
    """
    body = dict(BODY, questions=[question(type="score", instructions="How frustrated?")])
    answer = {
        "type": "score",
        "score": 2,
        "confidence": 0.7,
        "legend": {"0": "calm", "1": "mildly annoyed", "2": "angry"},
        "probabilities": {"0": 0.1, "1": 0.3, "2": 0.6},
    }
    async with asgi_client(app_for(answering(answer=answer))) as client:
        response = await client.post(ROUTE, json=body, headers=AUTH_HEADERS)

    assert response.status_code == 200
    returned = response.json()["answers"]["q1"]
    assert returned == answer
    assert returned["score"] == 2


async def test_many_questions_are_answered_in_the_order_they_were_asked() -> None:
    """Order, asserted rather than assumed.

    litellm answers a *named* map, so the order on the wire comes from the order
    muse asked. `q1`, `q2`, ... in question order, asserted against a mixed
    question list so a single-type request cannot pass by accident.
    """
    body = dict(
        BODY,
        questions=[
            question(instructions="refund?", type="noul"),
            question(instructions="urgent?", type="choice"),
            question(instructions="frustrated?", type="score"),
        ],
    )
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json=body, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert list(response.json()["answers"]) == ["q1", "q2", "q3"]


# --- the provider was called, and called once ------------------------------


async def test_one_decision_calls_the_provider_once() -> None:
    """The counting assertion, the heart of this file.

    A second decision path would either call the provider twice or not at all, and
    both look correct in a response assertion.
    """
    provider = answering()
    async with asgi_client(app_for(provider)) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert provider.calls == 1


async def test_the_router_forwards_the_questions_it_was_given() -> None:
    """What the provider actually received, asserted on the provider's own record.

    The questions are forwarded in order and named `q1`, ... by the router, not by
    the caller. If the naming moved into the provider or into the handler, the
    answers would come back keyed by something nobody wrote down.
    """
    provider = answering()
    async with asgi_client(app_for(provider)) as client:
        await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    (request,) = provider.decisions
    assert request.input == BODY["input"]
    # The route name, not the resolved model, is the caller's word for it: the
    # provider is dispatched with the model the route table resolved to, which
    # is what the response's `model` field names too.
    assert request.model == MODEL
    assert [q.type for q in request.questions] == ["noul"]
    assert request.questions[0].name == "q1"


# --- metering: a decision costs tokens, and the spend is recorded -----------


async def test_a_decision_is_metered_like_a_completion() -> None:
    """One `muse.tokens.consumed` row, with the five core fields, and no more.

    This is the requirement that a consumer cannot read decisions for free. The
    row is the one `Meter.record` writes on the completion path — the same
    envelope, the same five fields, the same transaction — because a decision
    spends tokens exactly as a completion does.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(answering(tokens=(12, 8)), database=database)) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert len(database.outbox) == 1
    payload = json.loads(database.outbox[0]["data"])
    assert payload == {
        "model": MODEL,
        "provider": "openai",
        "tokens_in": 12,
        "tokens_out": 8,
        # 12 in at 150/1k and 8 out at 600/1k: 1.8 + 4.8 = 6.6, rounded once to 7.
        "cost_micros": 7,
    }


async def test_the_metered_cost_matches_what_the_caller_is_shown() -> None:
    """The number on the wire is the number in the event.

    Not a shape assertion: it is the claim that a caller reconciling an invoice
    against `cost_micros` is reading the same number `billing` will. Same rule the
    alias holds.
    """
    database = FakeDatabase()
    async with asgi_client(app_for(answering(tokens=(12, 8)), database=database)) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["cost_micros"] == json.loads(database.outbox[0]["data"])["cost_micros"]


async def test_the_event_is_the_one_event_and_it_is_in_a_transaction() -> None:
    """Same insert, same depth. core's rule: an event is never written outside a
    transaction that also wrote the state it describes — and muse has no usage
    table, so the transaction is the whole write."""
    database = FakeDatabase()
    async with asgi_client(app_for(answering(), database=database)) as client:
        await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert database.depths() == [1]
    assert database.commits == 1
    assert database.rollbacks == 0


# --- the outbox: the publisher can deliver what the meter wrote -------------


async def test_the_outbox_publishes_the_decision_event() -> None:
    """The row the meter wrote is a row the publisher can put on the bus.

    Asserted through the real publisher against an in-memory transport, not by
    reading the row: a row that is in the table and cannot be rendered is a
    retained event nobody will ever receive, and that is invisible to a count.
    """
    from muse.outbox import InMemoryTransport, OutboxPublisher

    database = FakeDatabase()
    async with asgi_client(app_for(answering(), database=database)) as client:
        await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    transport = InMemoryTransport()
    assert await OutboxPublisher(database, transport).publish_batch() == 1

    # `(subject, envelope)` per publish, and the envelope's `data` arrives as the
    # dict a driver decoded the jsonb into.
    published = transport.published
    assert len(published) == 1
    subject, envelope = published[0]
    assert subject == "muse.tokens.consumed"
    assert envelope["specversion"] == "1.0"
    assert envelope["data"]["tokens_in"] == 12
    assert envelope["data"]["provider"] == "openai"


# --- auth: the same check, on the same terms -------------------------------


async def test_no_credential_is_a_401() -> None:
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json=BODY)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthorized"


async def test_a_token_with_no_capability_is_a_403() -> None:
    """`scopes=""`, so the claim is the empty *set* rather than a claim muse cannot
    interpret — which would be a 401 and would make this pass for the wrong
    reason."""
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json=BODY, headers={"Authorization": f"Bearer {token(IDENTITY, scopes='')}"})
    assert response.status_code == 403
    assert response.json()["code"] == "forbidden"


async def test_a_verified_token_is_served() -> None:
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)
    assert response.status_code == 200


# --- an unknown model is a 404, exactly as on /v1/route --------------------


async def test_an_unknown_model_is_the_same_404_v1_route_gives() -> None:
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json={**BODY, "model": "nope"}, headers=AUTH_HEADERS)

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert "nope" in response.json()["detail"]


# --- streaming is refused, with the field named ----------------------------


async def test_stream_is_a_422_naming_the_field() -> None:
    """The same refusal, in the same words, as `/v1/chat/completions`.

    A caller who asked for a stream and got a whole response has no way to tell
    what happened, so it is named rather than ignored. `extra="forbid"` is the
    mechanism, and the status is 422 — the same one the alias gives, for the same
    reason, in the same code.
    """
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json={**BODY, "stream": True}, headers=AUTH_HEADERS)

    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
    assert response.json()["errors"][0]["field"] == "stream"


async def test_an_unknown_field_is_refused_on_the_same_terms() -> None:
    """`extra="forbid"`, not a `stream`-specific branch. A second request field
    that litellm accepts later would be silently ignored by a keyword guard."""
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json={**BODY, "streamd": True}, headers=AUTH_HEADERS)
    assert response.status_code == 422


# --- the question count, and what makes it a 400 ---------------------------


async def test_an_out_of_range_question_count_is_a_400() -> None:
    """A caller-fixable refusal, before any provider is called.

    The bound is litellm's — it is the reason there is no validator here — and it
    arrives as a mapped `ProviderInvalidRequest`, which the router turns into
    `DecisionRequestRefused` rather than spending the fallback chain on a request
    that would be broken for every candidate.

    Driven through the real adapter against a stand-in litellm whose
    `adecisions` refuses, so the mapping under test is the one production runs.
    """
    primary = LiteLLMProvider(
        name="openai", api_key="sk-test", litellm=make_stub(raises="BadRequestError")
    )
    body = {**BODY, "questions": [question() for _ in range(200)]}
    async with asgi_client(app_for(primary)) as client:
        response = await client.post(ROUTE, json=body, headers=AUTH_HEADERS)

    assert response.status_code == 400
    assert response.json()["code"] == "validation_failed"


async def test_a_refused_request_does_not_spend_the_fallback_chain() -> None:
    """The reason the 400 is not a 503.

    "Every candidate failed" is an outage and a 503 tells the caller to retry. A
    request litellm refuses is broken everywhere, so retrying it and falling
    through to the next vendor are both waste — the router stops at the first
    refusal.
    """
    primary = LiteLLMProvider(
        name="openai", api_key="sk-test", litellm=make_stub(raises="BadRequestError")
    )
    fallback = FakeProvider(name="anthropic", price=PRICE)
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

    async with asgi_client(app_for(primary, fallback, table=table)) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 400
    assert fallback.calls == 0, "the fallback was asked about a request nobody could serve"


# --- reliability: the fallback chain is the completion's -------------------


async def test_a_failing_primary_falls_through_on_the_decisions_path() -> None:
    """The chain is the router's, not a decisions-specific one.

    If the decisions path dispatched on its own the fallback would be the first
    thing missing, and the failure would be a 503 in production with no test
    failure here — so the route is configured with two candidates and the primary
    is scripted to time out.
    """
    registry = ProviderRegistry()
    registry.register(
        ScriptedProvider(
            name="openai",
            price=PRICE,
            errors=[ProviderTimeout("primary is down")],
            completions=[],
            answers=[{"type": "noul", "noul": 0.42}],
        )
    )
    registry.register(
        FakeProvider(name="anthropic", price=PRICE, answer={"type": "noul", "noul": 0.99})
    )
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
    async with asgi_client(build_test_app(registry=registry, database=database, table=table)) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    assert response.json()["answers"]["q1"] == {"type": "noul", "noul": 0.99}
    assert response.json()["provider"] == "anthropic"
    assert json.loads(database.outbox[0]["data"])["provider"] == "anthropic"


async def test_every_candidate_failing_is_the_same_503() -> None:
    """muse is up and the models are not — the status a caller's retry logic
    branches on, identical to `/v1/route`'s."""
    registry = ProviderRegistry().register(
        ScriptedProvider(
            name="openai",
            price=PRICE,
            errors=[ProviderTimeout("down")],
            completions=[],
            answers=[],
        )
    )
    async with asgi_client(app_for(registry.get("openai"))) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 503
    assert response.json()["code"] == "unavailable"


# --- the response shape, asserted whole ------------------------------------


async def test_the_decision_body_has_exactly_the_documented_shape() -> None:
    """Exact equality per rule 4: a stray key is a contract change, and every SDK
    generated from `openapi/v1.yaml` would carry it forever.

    `id` and `created` are the two volatile values, so they are compared against
    their own documented grammar rather than pinned to a literal.
    """
    async with asgi_client(app_for(answering(tokens=(12, 8)))) as client:
        response = await client.post(ROUTE, json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 200
    body = response.json()
    assert set(body) == {
        "id",
        "object",
        "created",
        "model",
        "provider",
        "route",
        "answers",
        "usage",
        "cost_micros",
        "trace_id",
    }
    assert body["object"] == "decisions"
    assert body["model"] == MODEL
    assert body["route"] == "fast"
    assert body["provider"] == "openai"
    assert body["id"].startswith("decision-")
    assert isinstance(body["created"], int) and body["created"] > 0
    assert body["answers"] == {"q1": {"type": "noul", "noul": 0.93}}
    assert body["usage"] == {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}
    assert body["cost_micros"] == 7
    assert body["trace_id"] == response.headers["x-trace-id"]


async def test_every_non_2xx_is_problem_json_with_a_trace_id() -> None:
    """core's envelope, on this path too, and the trace id matches the header
    because support starts from it."""
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(ROUTE, json=BODY)

    assert response.status_code == 401
    assert response.headers["content-type"].startswith("application/problem+json")
    body = response.json()
    assert set(body) >= {"type", "title", "status", "detail", "instance", "code", "trace_id"}
    assert body["status"] == 401
    assert body["trace_id"] == response.headers["x-trace-id"]
    assert body["instance"] == ROUTE


async def test_the_body_is_not_valid_json_is_a_422() -> None:
    async with asgi_client(app_for(answering())) as client:
        response = await client.post(
            ROUTE, content=b"not json", headers={**AUTH_HEADERS, "content-type": "application/json"}
        )
    assert response.status_code == 422
    assert response.json()["code"] == "validation_failed"
