"""The HTTP surface: `POST /v1/route`, the error envelope, auth, and trace ids.

Four things live here and each answers a convention in core's
`docs/openapi-conventions.md`:

- **The error envelope.** Every non-2xx is `application/problem+json` (RFC 9457) with
  `type`, `title`, `status`, `detail`, `instance`, `code` and `trace_id`. A service
  that invents its own error body is a service that needs a bespoke SDK.
- **The status choices.** 404 for a model nothing routes, because that is a
  caller-fixable mistake; 503 for every candidate failing, because muse is up and the
  models are not, and a caller's retry logic branches on exactly that difference;
  422 for a well-formed request that is semantically wrong; 500 only for a bug in
  muse, whose type and message never reach the caller.
- **The bearer check.** Signature verified against identity's published JWKS with the
  algorithm pinned, then issuer, audience, expiry, and every required claim, then the
  capability the operation needs and the tenant the request runs as. A valid signature
  is not an authorization: a correctly signed token carrying no scope is refused.
  `muse.auth` is the whole of it; this module is the HTTP shape around it.
- **`X-Trace-Id` in the header and the body.** Support starts from that id, so a
  success that has one and a 401 that does not is a service you cannot debug at 3am.
  An *incoming* id is echoed only if it looks like an opaque token; anything else is
  replaced, because an attacker-controlled header that reaches a log line is a
  log-injection vector and sanitising is a filter with a bypass.

Metering happens in the handler, on the success path, and a metering failure is a
503 rather than a swallowed exception: the completion has already been paid for, and
returning it without its record loses the spend silently.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from muse.auth import SCOPE, Principal, TokenVerifier, bearer_token, require_scope
from muse.errors import (
    AllCandidatesFailed,
    AuthError,
    InsufficientScope,
    MissingAccount,
    MuseError,
    RouteConfigError,
    RouteNotFound,
    SigningKeysUnavailable,
    Unauthenticated,
)

#: core's error type URIs. Stable, machine-readable, and the last segment is the same
#: slug as `code`.
ERROR_BASE = "https://errors.cafaye.com"

#: The reserved codes from core's openapi conventions, and the title each one gets.
#: A title is a fixed human-readable summary for the code; it may be reworded without
#: a version bump, which is why it lives in one table rather than in each call site.
TITLES = {
    "unauthorized": "Unauthorized",
    "forbidden": "Forbidden",
    "not_found": "Not found",
    "conflict": "Conflict",
    "validation_failed": "Validation failed",
    "rate_limited": "Rate limited",
    "internal": "Internal error",
    "unavailable": "Service unavailable",
}

#: A `trace_id` we are willing to echo: 8-64 characters of a token alphabet. Anything
#: else is replaced with a fresh one. Generated ids are `uuid4().hex` (32), so a
#: caller generating ids as short as eight characters is not disconnected from its
#: own trace.
TRACE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")

TRACE_HEADER = "X-Trace-Id"

#: `temperature` on the wire, and the one place that grammar is written down.
#:
#: Plain decimal in [0, 2]: no exponent, no leading `+`, no `NaN`, and no `.5`
#: without the zero. The range lives in the pattern rather than only in the
#: description because a pattern is what a generated client can enforce, and a
#: bound written only in prose is a sentence rather than a check. It is the same
#: string the OpenAPI document publishes on `RouteRequest.temperature`, which is
#: what makes the two a contract instead of two statements that agree today —
#: `tests/test_openapi.py` asserts the document and this constant are the same
#: pattern.
#:
#: The alternative was an integer with a scale (milli-degrees, so `700` means
#: 0.7), which keeps the wire integral without a string. It is not what the
#: provider takes: LiteLLM declares `temperature: float | None`, so a scale would
#: have to be divided back out on the way to the provider anyway, and every
#: caller would have to know the scale. A decimal string is the Kubernetes advice
#: for a provider-passthrough parameter where exact bytes matter.
TEMPERATURE_PATTERN = r"^(?:0(?:\.[0-9]+)?|1(?:\.[0-9]+)?|2(?:\.0+)?)$"

#: `owned_by` on every entry in `GET /v1/models`.
#:
#: OpenAI's field names the organisation that owns the model, and this service is not
#: an organisation — it is a router in front of vendors it does not own. So the value
#: is muse's own name: the honest answer, and the one a reader can act on.
MODEL_OWNER = "muse"


#: The request body. `extra="forbid"` because core closes request bodies like it
#: closes schemas: a typo'd `max_token` that is silently ignored leaves a caller
#: believing they capped the response when they did not.
class RouteRequestBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str = Field(description="The route to use, as named in config/routes.yaml.")
    messages: list[RouteMessage] = Field(
        min_length=1, description="The conversation, oldest message first."
    )
    max_tokens: int | None = Field(default=None, gt=0)
    # A decimal STRING, because a float on the wire is a value the caller cannot
    # round-trip. It was `float | None` and openapi/v1.yaml said `type: number`;
    # Kubernetes' API conventions refuse that at `api-conventions.md:603` because
    # floats "cannot be reliably round-tripped and have varying precision across
    # languages and architectures". The concrete failure is a JavaScript caller
    # sending `0.1 + 0.2`, which `float64` puts on the wire as
    # `0.30000000000000004` — real digits, all of them forwarded to the provider.
    #
    # `sampling_temperature()` converts, and it is the ONLY place in this
    # repository where this value becomes a float: LiteLLM takes
    # `temperature: float | None`, so a float is unavoidable at that seam and
    # unavoidable exactly once. That is the same single-conversion seam
    # `cost_per_1k_tokens` converts money on, for the same reason.
    temperature: str | None = Field(default=None, pattern=TEMPERATURE_PATTERN)

    def sampling_temperature(self) -> float | None:
        """The caller's decimal string as the float LiteLLM wants, or None.

        The two `ValueError` arms are a BACKSTOP, not the check: pydantic has
        already validated `temperature` against `TEMPERATURE_PATTERN`, which is the
        same grammar the OpenAPI document publishes, so nothing that reaches here
        from the endpoint can trip either arm.

        They exist because this method is also reachable directly — a test, or a
        future caller building the model itself — and because a `Decimal` that
        raises is an unhandled 500 rather than the 422 the contract promises for a
        malformed temperature. Unreachable from `/v1/route` and covered as a unit
        test is the honest way to hold that line: a `pragma: no cover` here would
        hide the day the pattern and this method stopped agreeing, which is the one
        thing that must not happen silently.
        """
        if self.temperature is None:
            return None
        try:
            value = Decimal(self.temperature)
        except InvalidOperation as error:
            raise ValueError(f"temperature {self.temperature!r} is not a decimal number") from error
        if not Decimal(0) <= value <= Decimal(2):
            raise ValueError("temperature must be between 0 and 2 inclusive")
        return float(value)


class RouteMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    role: str
    content: str


class Problem(BaseModel):
    """core's error envelope, exactly as its conventions document it."""

    type: str
    title: str
    status: int
    detail: str
    instance: str
    code: str
    trace_id: str

    def rendered(self) -> dict[str, Any]:
        return self.model_dump()


def problem(
    request: Request,
    code: str,
    status: int,
    detail: str,
    *,
    fields: list[dict[str, str]] | None = None,
) -> JSONResponse:
    """A `problem+json` response.

    `detail` is the only free-form field and it is still scrubbed by the caller: it
    often carries provider text, and provider text is third-party text that can echo
    the key it rejected.
    """
    body = Problem(
        type=f"{ERROR_BASE}/{code}",
        title=TITLES[code],
        status=status,
        detail=detail,
        instance=request.url.path,
        code=code,
        trace_id=trace_id_for(request),
    )
    payload = body.rendered()
    if fields:
        payload["errors"] = fields
    return JSONResponse(
        status_code=status,
        content=payload,
        media_type="application/problem+json",
        headers={TRACE_HEADER: body.trace_id},
    )


def trace_id_for(request: Request) -> str:
    return getattr(request.state, "trace_id", uuid.uuid4().hex)


def new_trace_id(incoming: str | None) -> str:
    """Echo `incoming` if it looks like an opaque token, else mint a fresh id.

    Replacing rather than sanitising, deliberately: a sanitiser is a filter with a
    bypass, and a fresh id has nothing to bypass. The cost is that a caller sending a
    malformed id loses their correlation — which is the correct outcome, because an id
    nobody can trust is worse than one nobody has.
    """
    if incoming and TRACE_ID_RE.match(incoming):
        return incoming
    return uuid.uuid4().hex


async def require_bearer(request: Request, scope: str = SCOPE) -> Principal:
    """The verified caller, or raise.

    Header shape, then signature, then claims, then capability, then tenancy — in that
    order, because each step is cheaper than the next and refusing early is what keeps a
    malformed token from costing a key fetch.

    `scope` is the capability the *operation* needs and is a parameter rather than a
    constant so a future operation states its own rather than inheriting this one's.
    """
    verifier: TokenVerifier = request.app.state.container.auth
    token = bearer_token(request.headers.get("Authorization"))
    if token is None:
        raise Unauthenticated("a bearer credential is required")

    principal = await verifier.verify(token)
    require_scope(principal, scope)
    return principal


#: The status and reserved code each authentication failure renders as.
#:
#: A table rather than a chain of `except` clauses, so the mapping is one readable fact
#: and adding an auth failure is adding a row. The exact type is the key, not the class
#: hierarchy — the same discipline as `muse.errors.is_retryable` and `muse.errortype`,
#: and for the same reason: a new `AuthError` subclass must be a *decision* about its
#: status rather than inheriting one by accident.
#:
#: `SigningKeysUnavailable` is 503 because identity being down is not the caller's
#: credential being bad; reporting it as a 401 sends the caller to re-authenticate
#: against a healthy service and then retry forever.
_AUTH_STATUS: dict[type[AuthError], tuple[str, int]] = {
    SigningKeysUnavailable: ("unavailable", 503),
    MissingAccount: ("unauthorized", 401),
    Unauthenticated: ("unauthorized", 401),
    InsufficientScope: ("forbidden", 403),
}

#: What an `AuthError` with no row above renders as. **Refusal, not admission**: a new
#: failure that nobody has classified yet answers 401 rather than 200, because the whole
#: point of the check is that an unclassified refusal cannot become a served request.
_UNCLASSIFIED_AUTH = ("unauthorized", 401)


def auth_problem(request: Request, error: AuthError) -> JSONResponse:
    """The problem envelope for an auth failure, with the right status for its kind."""
    code, status = _AUTH_STATUS.get(type(error), _UNCLASSIFIED_AUTH)
    return problem(request, code, status, str(error))


@dataclass(frozen=True, slots=True)
class RouteResponseBody(BaseModel):
    """The success body. Asserted by exact equality in the suite, so a field added
    here is a contract change and shows up as a failing test rather than as a new key
    in every SDK generated from `openapi/v1.yaml`."""

    id: str
    object: str
    created: int
    model: str
    provider: str
    route: str
    choices: list[dict[str, Any]]
    usage: dict[str, int]
    cost_micros: int
    trace_id: str


class ChatCompletionBody(BaseModel):
    """OpenAI's `chat.completion` object, and only that.

    Six fields, chosen because a generated OpenAI client validates against them and
    because every one of them is already on `ServedCompletion`. The three muse-specific
    fields on `RouteResponseBody` — `provider`, `route`, `cost_micros` — are absent on
    purpose: this is the compatibility surface, and a caller who wants them is a caller
    who should be calling `/v1/route`. `trace_id` is absent for the same reason and its
    loss is covered by the `X-Trace-Id` header, which middleware sets on every response.
    """

    id: str
    object: str
    created: int
    model: str
    choices: list[dict[str, Any]]
    usage: dict[str, int]


class ModelEntry(BaseModel):
    """One entry in OpenAI's model list."""

    id: str
    object: str
    created: int
    owned_by: str


class ModelList(BaseModel):
    """OpenAI's list envelope."""

    object: str
    data: list[ModelEntry]


#: A served completion, before any response shape is chosen.
#:
#: One type for both endpoints because there is one served completion: the alias and
#: `/v1/route` render the same object differently, and a second type here would be an
#: invitation to compute the alias's numbers independently — which is exactly the bug
#: this packet is about. The token counts and the cost on this object are the ones that
#: were metered.
@dataclass(frozen=True, slots=True)
class ServedCompletion:
    id: str
    created: int
    model: str
    provider: str
    route: str
    content: str
    finish: str
    tokens_in: int
    tokens_out: int
    cost_micros: int
    trace_id: str

    @property
    def choices(self) -> list[dict[str, Any]]:
        """The single choice, in OpenAI's shape.

        A property rather than a field because it is derived from `content` and
        `finish` and must not be stored twice: two copies of one choice is two things
        to keep in agreement.
        """
        return [
            {
                "index": 0,
                "message": {"role": "assistant", "content": self.content},
                "finish_reason": self.finish,
            }
        ]

    @property
    def usage(self) -> dict[str, int]:
        """`total_tokens` computed here rather than taken from a provider.

        Same rule as `RouteResponseBody.usage`: a provider that reports its two counts
        inconsistently must not be able to make the three disagree. Computed on both
        paths from the same object, so the alias cannot disagree with what was metered.
        """
        return {
            "prompt_tokens": self.tokens_in,
            "completion_tokens": self.tokens_out,
            "total_tokens": self.tokens_in + self.tokens_out,
        }


async def serve_completion(request: Request) -> ServedCompletion | JSONResponse:
    """Authenticate, validate, route, meter — the whole path both endpoints share.

    **This is the alias's reason for existing.** `POST /v1/chat/completions` calls this
    function and renders the result in OpenAI's shape; `POST /v1/route` calls it and
    renders it in muse's. There is no second routing call, no second metering call, and
    no second auth check, because there is one function that does all three and both
    endpoints delegate to it.

    The order is load-bearing and is `/v1/route`'s existing order, unchanged:

    1. **Authenticate, before the body is read and before any provider is contacted.**
       An unauthenticated request must not be able to make muse spend money, and it
       must not be able to learn whether a model name routes at all.
    2. **Validate**, so a typo'd field is a 422 naming the field rather than a bill for
       a request nobody meant.
    3. **Route**, through `container.router` — the one router, with the one fallback
       chain and the one breaker registry.
    4. **Meter, before the response is built and not inside a `try`.** The completion has
       already been paid for; returning it without its record loses the spend silently,
       and a 503 makes the loss visible. This is why metering happens *here* rather
       than in either endpoint: metering on the alias's own path would be the second
       call this packet is trying not to write.

    A `JSONResponse` return is a `problem+json` failure — every one of them produced by
    the same helpers `/v1/route` uses, so the failure modes are identical by
    construction rather than by agreement between two pieces of code.
    """
    try:
        await require_bearer(request)
    except AuthError as error:
        return auth_problem(request, error)

    try:
        body = RouteRequestBody.model_validate(await request.json())
    except ValidationError as error:
        return problem(request, "validation_failed", 422, _describe(error), fields=_fields(error))
    except ValueError:
        return problem(request, "validation_failed", 422, "the request body is not valid JSON")

    container = request.app.state.container
    try:
        result = await container.router.route(
            body.model,
            [(message.role, message.content) for message in body.messages],
            max_tokens=body.max_tokens,
            temperature=body.sampling_temperature(),
            secrets=container.credentials_held(),
        )
    except RouteNotFound as error:
        return problem(
            request, "not_found", 404, f"no route is configured for model {error.model!r}"
        )
    except ValueError as error:
        # A bad role or an empty message, raised by the router before any provider
        # was called — so naming it here costs the caller nothing.
        return problem(request, "validation_failed", 422, str(error))
    except AllCandidatesFailed as error:
        return problem(request, "unavailable", 503, _chain(error))

    await container.meter.record(result)

    completion = result.completion
    return ServedCompletion(
        id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
        created=int(time.time()),
        model=completion.model,
        provider=completion.provider,
        route=result.route,
        content=completion.content,
        finish=completion.finish,
        tokens_in=completion.tokens_in,
        tokens_out=completion.tokens_out,
        cost_micros=result.cost_micros,
        trace_id=trace_id_for(request),
    )


def build_router() -> APIRouter:
    """The versioned surface. A separate router so the probes and the API can be
    mounted, tested and reasoned about independently."""
    api = APIRouter(prefix="/v1", tags=["llm"])

    @api.post(
        "/route",
        response_model=RouteResponseBody,
        status_code=200,
        summary="Route a chat completion",
        description=(
            "Serves a completion from the first candidate that succeeds for the named "
            "route. The caller never learns which vendor answered. Every served "
            "completion emits a `muse.tokens.consumed` event."
        ),
    )
    async def route_completion(request: Request) -> Response:
        served = await serve_completion(request)
        if isinstance(served, JSONResponse):
            return served

        response = RouteResponseBody(
            id=served.id,
            object="chat.completion",
            created=served.created,
            model=served.model,
            provider=served.provider,
            route=served.route,
            choices=served.choices,
            usage=served.usage,
            cost_micros=served.cost_micros,
            trace_id=served.trace_id,
        )
        return JSONResponse(
            content=response.model_dump(),
            headers={TRACE_HEADER: response.trace_id},
        )

    @api.post(
        "/chat/completions",
        response_model=ChatCompletionBody,
        status_code=200,
        summary="Create a chat completion (OpenAI-compatible alias for /v1/route)",
        description=(
            "OpenAI's path and response shape, served by the same routing, "
            "authentication, authorization, metering and outbox path as `POST "
            "/v1/route`. It is an alias, not a second implementation: a stock OpenAI "
            "client reaches this service without knowing muse exists. **Streaming is "
            "not supported** — a request carrying `stream` is a 422 naming the field, "
            "not a silently non-streaming answer."
        ),
    )
    async def create_chat_completion(request: Request) -> Response:
        served = await serve_completion(request)
        if isinstance(served, JSONResponse):
            return served

        # OpenAI's six fields and nothing else. `provider`, `route` and `cost_micros`
        # are muse's additions and belong to `/v1/route`; a compatibility surface that
        # grew its own extras is one a generated client has to be taught about, which
        # is the opposite of what an alias is for. `X-Trace-Id` still carries the trace
        # id — it is set by middleware every response passes through, so support
        # correlation survives the body dropping the field.
        response = ChatCompletionBody(
            id=served.id,
            object="chat.completion",
            created=served.created,
            model=served.model,
            choices=served.choices,
            usage=served.usage,
        )
        return JSONResponse(
            content=response.model_dump(),
            headers={TRACE_HEADER: served.trace_id},
        )

    @api.get(
        "/models",
        response_model=ModelList,
        status_code=200,
        summary="List the models this service can route to",
        description=(
            "OpenAI's model list, sourced from this service's own route table rather "
            "than a hardcoded list — so it cannot disagree with what muse can actually "
            "route. Requires a credential and the same `completions:write` capability "
            "as every other operation here; a model list names every route and, through "
            "them, every vendor this deployment talks to."
        ),
    )
    async def list_models(request: Request) -> Response:
        # The same check, the same scope, and therefore the same 401 / 403 / 503
        # distinctions. A separate weaker check here would be a parallel auth policy,
        # which is the class of thing `muse.auth`'s docstring argues against.
        try:
            await require_bearer(request)
        except AuthError as error:
            return auth_problem(request, error)

        table = request.app.state.container.routes
        response = ModelList(
            object="list",
            data=[
                ModelEntry(id=name, object="model", created=0, owned_by=MODEL_OWNER)
                for name in table.models()
            ],
        )
        return JSONResponse(
            content=response.model_dump(),
            headers={TRACE_HEADER: trace_id_for(request)},
        )

    return api


#: A Starlette status mapped onto a reserved code. Core's list has no 405, so the
#: method error borrows `not_found`: both answer "you asked for something that is not
#: here", and inventing a code for it would be a client that has to special-case muse.
_STATUS_CODES = {
    400: "validation_failed",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "not_found",
    409: "conflict",
    422: "validation_failed",
    429: "rate_limited",
    500: "internal",
    503: "unavailable",
}


def _code_for_status(status: int) -> str:
    return _STATUS_CODES.get(status, "internal")


def _chain(error: AllCandidatesFailed) -> str:
    """The failure chain as one sentence.

    "it failed" is not actionable; "openai timed out, anthropic rejected the
    credential" is the first line of the investigation, and the router already knows
    all of it. The details were scrubbed by the router before they got here.
    """
    parts = [
        f"{failure.provider}/{failure.model}: {failure.error.__name__}"
        + (f" (after {failure.attempts} attempts)" if failure.attempts > 1 else "")
        for failure in error.failures
    ]
    return f"every candidate for model {error.model!r} failed — " + "; ".join(parts)


def _describe(error: ValidationError) -> str:
    """A validation failure as one sentence naming the first offending field.

    The provider would answer 400 in its own words; naming the field is the difference
    between a one-line fix and a round trip to whoever wrote the caller.
    """
    first = error.errors()[0]
    where = ".".join(str(part) for part in first["loc"]) or "the body"
    return f"{where}: {first['msg']}"


def _fields(error: ValidationError) -> list[dict[str, str]]:
    """core's `errors[]`, present only on a 422."""
    return [
        {"field": ".".join(str(part) for part in item["loc"]), "code": item["type"]}
        for item in error.errors()
    ]


def register_error_handlers(app: FastAPI) -> None:
    """Map every error onto the envelope: HTTP errors, muse's typed errors, and
    everything else.

    The `HTTPException` handler is the one that is easy to miss. Starlette's built-in
    404 and 405 bodies are `{"detail": "..."}` as `application/json`, and core says
    *every* non-2xx is `application/problem+json` — so without this a client that can
    parse our errors cannot parse the two it is most likely to hit while integrating.
    """

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return problem(
            request,
            _code_for_status(error.status_code),
            error.status_code,
            str(error.detail),
        )

    @app.exception_handler(RouteConfigError)
    async def _config_error(request: Request, error: RouteConfigError) -> JSONResponse:
        return problem(request, "internal", 500, "muse is misconfigured")

    @app.exception_handler(MuseError)
    async def _muse_error(request: Request, error: MuseError) -> JSONResponse:
        return problem(request, "internal", 500, "muse failed to handle the request")

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> JSONResponse:
        return problem(request, "internal", 500, "an unexpected error occurred")


__all__ = [
    "MODEL_OWNER",
    "TRACE_HEADER",
    "ChatCompletionBody",
    "ModelEntry",
    "ModelList",
    "Problem",
    "RouteRequestBody",
    "RouteResponseBody",
    "ServedCompletion",
    "auth_problem",
    "build_router",
    "new_trace_id",
    "problem",
    "register_error_handlers",
    "require_bearer",
    "serve_completion",
    "trace_id_for",
]
