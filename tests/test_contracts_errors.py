"""The paths that only fire when something is already wrong.

These are the branches a suite never reaches by accident, which is exactly why they
need tests: an error handler with no test is an error handler that has never run, and
the first time it runs is in production during an incident.

Four of them:

- **The `RouteConfigError` handler.** A misconfiguration reaching a request means the
  boot check did not catch it — a routes file edited under a running container, most
  likely. It must be a `problem+json` body like every other error, not a FastAPI
  default.
- **The generic `MuseError` handler.** Any typed error the endpoint did not explicitly
  map. It must not leak the error's type or message.
- **The generic `Exception` handler.** A bug in muse. The trace id is the handle and
  the log line is where the detail lives, so the body says nothing at all.
- **A provider with neither a key nor a resolver**, and one whose resolver has no
  credential for it. Both are `CredentialUnavailable` rather than an `AttributeError`
  three frames down.
"""

from __future__ import annotations

import base64

import pytest

from muse.errors import CredentialUnavailable, RouteConfigError
from muse.main import Container, Settings, create_app
from muse.metering import Meter
from muse.providers import LiteLLMProvider, ProviderRegistry
from muse.providers.credentials import StaticCredentials, VaultCredentials
from muse.providers.fake import ScriptedProvider
from muse.redaction import Secret
from muse.router import Router
from muse.routes import RetryPolicy, RouteTable, routes_from_yaml
from muse.vault import Vault, load_vault_key

from .conftest import AUTH_HEADERS, asgi_client, write_routes
from .support.fake_database import FakeDatabase

pytestmark = [pytest.mark.anyio, pytest.mark.unit]

PROVIDER = "openai"
MODEL = "gpt-4o-mini"
BODY = {"model": "fast", "messages": [{"role": "user", "content": "hello"}]}
KEY = base64.b64encode(bytes(range(32))).decode()


def app_that_raises(error: Exception):
    """An app whose only route raises `error` from inside the handler.

    A stub provider whose `complete` raises, so the error propagates through the real
    router and the real exception-handler chain rather than being handed to a handler
    directly. That is the path a real failure takes.
    """

    class Exploding(ScriptedProvider):
        async def complete(self, request):  # type: ignore[override]
            raise error

    provider = Exploding(name=PROVIDER)
    registry = ProviderRegistry().register(provider)
    table = routes_from_yaml(
        write_routes(
            {
                "version": 1,
                "routes": [
                    {"model": "fast", "candidates": [{"provider": PROVIDER, "model": MODEL}]}
                ],
            }
        )
    )
    database = FakeDatabase()
    container = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table),
        vault=Vault(database, load_vault_key({"MUSE_VAULT_KEY": KEY})),
        meter=Meter(database),
        credentials=StaticCredentials({}),
    )
    return create_app(container=container)


# --- the error handlers ---------------------------------------------------


async def test_a_misconfiguration_reaching_a_request_is_an_internal_problem() -> None:
    """A `RouteConfigError` at request time means the boot check missed it — most
    likely a routes file edited under a running container. It has to be a
    `problem+json` body like every other error, and it must not carry the config
    detail: a routes file's contents are not the caller's business, and a
    misconfiguration detail can name a file path on the host."""
    async with asgi_client(app_that_raises(RouteConfigError("routes.yaml: unknown key"))) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["code"] == "internal"
    assert "routes.yaml" not in response.text


async def test_an_unmapped_muse_error_is_an_internal_problem() -> None:
    """Any typed error the endpoint did not map explicitly. The type and message stay
    in the log; the caller gets the code and the trace id."""
    from muse.errors import VaultDecryptError

    async with asgi_client(app_that_raises(VaultDecryptError("row for 'openai' is broken"))) as c:
        response = await c.post("/v1/route", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 500
    assert response.json()["code"] == "internal"
    assert "VaultDecryptError" not in response.text
    assert "row for" not in response.text


async def test_a_bug_in_muse_says_nothing_about_itself() -> None:
    """Not the type, not the message, not a hint. The trace id correlates this response
    with the log line that has all of it — and a caller who reads the message learns
    about muse's internals, which is not information they can act on and is information
    an attacker can use."""
    async with asgi_client(app_that_raises(ZeroDivisionError("division by zero"))) as client:
        response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)

    assert response.status_code == 500
    assert response.json() == {
        "type": "https://errors.cafaye.com/internal",
        "title": "Internal error",
        "status": 500,
        "detail": "an unexpected error occurred",
        "instance": "/v1/route",
        "code": "internal",
        "trace_id": response.headers["X-Trace-Id"],
    }


async def test_every_problem_body_carries_a_trace_id_matching_its_header() -> None:
    """core: `trace_id` is always present and always matches the response header.
    A body that disagrees sends support to the wrong log line, which is the one job
    the field has."""
    for error in (RouteConfigError("x"), ZeroDivisionError("y")):
        async with asgi_client(app_that_raises(error)) as client:
            response = await client.post("/v1/route", json=BODY, headers=AUTH_HEADERS)
        assert response.json()["trace_id"] == response.headers["X-Trace-Id"]
        assert response.json()["trace_id"]


# --- a provider with no usable credential ----------------------------------


def test_a_provider_with_neither_a_key_nor_a_resolver_is_refused() -> None:
    """At construction, not on the first call. A provider with no way to get a key
    would otherwise fail in production with a message about a missing constructor
    argument, one request at a time."""
    with pytest.raises(ValueError, match="api_key or a credentials resolver"):
        LiteLLMProvider(name=PROVIDER)


def test_a_provider_rejects_an_unsupported_vendor_before_anything_else() -> None:
    """The vendor check comes first because a name that is not a vendor makes every
    later line meaningless — including the credential check, whose message would name
    a vendor that does not exist."""
    with pytest.raises(ValueError, match="not a supported vendor"):
        LiteLLMProvider(name="bedrock")


async def test_a_resolver_with_no_credential_for_this_vendor_is_unavailable() -> None:
    """The adapter turns `CredentialUnavailable` into "no key", and `complete` turns
    that into `CredentialUnavailable` again. The round trip matters: the provider must
    report the *same* error the resolver did, not a new one, or a caller cannot tell
    "not onboarded" from "the row is corrupt"."""
    from muse.providers import CompletionRequest, Message

    provider = LiteLLMProvider(name=PROVIDER, credentials=StaticCredentials({}), litellm=object())
    with pytest.raises(CredentialUnavailable, match=PROVIDER):
        await provider.complete(
            CompletionRequest(model=MODEL, messages=(Message(role="user", content="hi"),))
        )


async def test_a_vault_with_no_key_for_this_vendor_is_unavailable() -> None:
    """The production path, end to end: a vault that has never been written to. The
    health probe reports it too, so a readiness body and a request agree."""
    from muse.providers import CompletionRequest, Message

    vault = Vault(FakeDatabase(), load_vault_key({"MUSE_VAULT_KEY": KEY}))
    provider = LiteLLMProvider(name=PROVIDER, credentials=VaultCredentials(vault), litellm=object())

    health = await provider.health()
    assert health.healthy is False
    assert PROVIDER in health.detail

    with pytest.raises(CredentialUnavailable):
        await provider.complete(
            CompletionRequest(model=MODEL, messages=(Message(role="user", content="hi"),))
        )


async def test_a_key_written_to_the_vault_is_used_on_the_next_call() -> None:
    """The whole reason the resolver is per-call: a key that is onboarded after boot
    works without a restart, and a key that is rotated works without a deploy. The
    key reaches litellm, which is what makes it a credential and not a lookup."""
    from muse.providers import CompletionRequest, Message

    captured: dict[str, object] = {}

    class Stub:
        async def acompletion(self, **kwargs):
            captured.update(kwargs)
            return {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            }

        def get_model_info(self, model):
            return {"input_cost_per_token": 1e-07, "output_cost_per_token": 2e-07}

    vault = Vault(FakeDatabase(), load_vault_key({"MUSE_VAULT_KEY": KEY}))
    provider = LiteLLMProvider(name=PROVIDER, credentials=VaultCredentials(vault), litellm=Stub())
    await vault.put(PROVIDER, Secret("sk-live-ROTATED"))

    await provider.complete(
        CompletionRequest(model=MODEL, messages=(Message(role="user", content="hi"),))
    )

    assert captured["api_key"] == "sk-live-ROTATED"


async def test_the_lifespan_builds_a_container_when_none_was_supplied(
    tmp_path, monkeypatch
) -> None:
    """The production path, through the app: a lifespan event with no container
    supplied assembles one from the environment.

    Driven with the real `async with app.router.lifespan_context(app)` rather than
    through an HTTP request, because `httpx.ASGITransport` does not run lifespan
    events — and the lifespan is where the *only* place `build_container` is called
    lives. Without this, the line that connects the environment to a running service
    would be the one line in this packet with no test.

    No database URL is set, so this assembles a real vault, a real registry with both
    vendor adapters, a real router and a real meter — over the refusing stand-in for
    postgres. The routes file is a temporary one so the committed file's contents are
    not a precondition of this test.
    """
    monkeypatch.setenv("MUSE_VAULT_KEY", KEY)
    routes = tmp_path / "routes.yaml"
    routes.write_text(
        write_routes(
            {"version": 1, "routes": [{"model": "fast", "candidates": [{"provider": "openai"}]}]}
        ),
        encoding="utf-8",
    )
    app = create_app(settings=Settings(env="test", routes_path=routes))
    async with app.router.lifespan_context(app):
        container = app.state.container
    assert container is not None
    assert container.registry.names() == ("anthropic", "openai")
    assert container.routes.models() == ("fast",)
    assert container.router.routes is container.routes
    assert isinstance(container.vault, Vault)
    assert isinstance(container.credentials, VaultCredentials)
    assert isinstance(container.meter, Meter)
    assert container.settings.env == "test"


async def test_a_supplied_container_survives_the_lifespan_untouched(tmp_path, monkeypatch) -> None:
    """The other branch of the same `if`. A container handed to `create_app` is not
    rebuilt from the environment during startup — otherwise a test's fakes would be
    replaced by real adapters the moment anything ran a lifespan, which is a failure
    that only appears in a suite that happens to use one."""
    monkeypatch.delenv("MUSE_VAULT_KEY", raising=False)
    database = FakeDatabase()
    registry = ProviderRegistry()
    table = RouteTable(version=1, defaults=RetryPolicy(), routes=())
    supplied = Container(
        settings=Settings(env="test"),
        database=database,
        registry=registry,
        routes=table,
        router=Router(registry, table),
        vault=Vault(database, load_vault_key({"MUSE_VAULT_KEY": KEY})),
        meter=Meter(database),
        credentials=StaticCredentials({}),
    )
    app = create_app(container=supplied)
    async with app.router.lifespan_context(app):
        assert app.state.container is supplied


def test_a_table_with_no_routes_is_a_valid_empty_router() -> None:
    """Used by the readiness fixture. Stated here because an empty table is a real
    state a caller can construct, and `find` returning `None` for everything is the
    correct answer rather than an error."""
    table = RouteTable(version=1, defaults=RetryPolicy(), routes=())
    assert table.find("anything") is None
    assert table.models() == ()
