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
    temperature: float | None = None


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
        # Authentication before the body is read, and before any provider is contacted:
        # an unauthenticated request must not be able to make muse spend money, and it
        # must not be able to learn whether a model name routes at all.
        try:
            await require_bearer(request)
        except AuthError as error:
            return auth_problem(request, error)

        try:
            body = RouteRequestBody.model_validate(await request.json())
        except ValidationError as error:
            return problem(
                request, "validation_failed", 422, _describe(error), fields=_fields(error)
            )
        except ValueError:
            return problem(request, "validation_failed", 422, "the request body is not valid JSON")

        container = request.app.state.container
        try:
            result = await container.router.route(
                body.model,
                [(message.role, message.content) for message in body.messages],
                max_tokens=body.max_tokens,
                temperature=body.temperature,
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

        # Metered before the response is built, and not inside a try. The completion
        # has already been paid for; returning it without its record loses the spend
        # silently, and a 503 makes the loss visible.
        await container.meter.record(result)

        completion = result.completion
        response = RouteResponseBody(
            id=f"chatcmpl-{uuid.uuid4().hex[:24]}",
            object="chat.completion",
            created=int(time.time()),
            model=completion.model,
            provider=completion.provider,
            route=result.route,
            choices=[
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": completion.content},
                    "finish_reason": completion.finish,
                }
            ],
            usage={
                "prompt_tokens": completion.tokens_in,
                "completion_tokens": completion.tokens_out,
                "total_tokens": completion.tokens_in + completion.tokens_out,
            },
            cost_micros=result.cost_micros,
            trace_id=trace_id_for(request),
        )
        return JSONResponse(
            content=response.model_dump(),
            headers={TRACE_HEADER: response.trace_id},
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
    "TRACE_HEADER",
    "Problem",
    "RouteRequestBody",
    "RouteResponseBody",
    "auth_problem",
    "build_router",
    "new_trace_id",
    "problem",
    "register_error_handlers",
    "require_bearer",
    "trace_id_for",
]
