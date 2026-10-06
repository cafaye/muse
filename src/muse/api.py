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

import json
import logging
import re
import time
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi import APIRouter, FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StrictBool, ValidationError
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
from muse.providers import Completion, StreamChunk
from muse.router import RoutedCompletion, SettledStream, StreamResult

#: This module's logger. Named and used rather than `logging.getLogger(__name__)`
#: at each call site so a rule about what may be logged (no token, no prompt, no
#: credential — AGENTS.md rules 16 and 22) has one place to be enforced and one line
#: to grep. Everything that goes in is an error *class*, a route name and a trace id.
logger = logging.getLogger("muse.api")

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
    #: Added in 1.3.0, and **strictly** a boolean: `"true"`, `1` and `"yes"` are 422s
    #: naming the field rather than quietly streaming.
    #:
    #: `StrictBool` rather than `bool` because pydantic's lax mode coerces all three of
    #: those to `True` and all of `"false"`, `0` and `"no"` to `False`. That is the
    #: precise hazard this repository already refused once for `temperature` (1.1.1):
    #: a caller who wrote the wrong thing gets a 200 and no indication of it. Here the
    #: coercion is worse than a lost rounding — a caller sending `stream: "false"`
    #: because their serialiser stringifies booleans would silently get a **stream**,
    #: which their client may not be reading at all.
    stream: StrictBool = Field(default=False)
    stream_options: StreamOptions | None = Field(default=None)

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


class StreamOptions(BaseModel):
    """`stream_options`, as OpenAI defines it.

    One field, and one boolean. `include_usage` is the only thing OpenAI's
    `stream_options` carries today, and a closed schema that grew a second field
    without a use for it would be a field every generated client has to be taught
    about.
    """

    model_config = ConfigDict(extra="forbid")

    include_usage: StrictBool = Field(
        default=False,
        description=(
            "Include a final chunk carrying token usage. Off by default, because the "
            "default is what a caller who did not ask for a bill wants."
        ),
    )


class CompletionChunkBody(BaseModel):
    """One `chat.completion.chunk`, exactly as OpenAI's SSE frames carry it.

    Seven fields, all of which a generated OpenAI client reads and none of which muse
    adds. `cost_micros`, `provider` and `route` are absent for the same reason they
    are absent from `ChatCompletionBody`: this is the compatibility surface, and a
    stream is not a licence to grow it. `/v1/route` renders the same completion with
    them.
    """

    id: str
    object: str
    created: int
    model: str
    choices: list[dict[str, Any]]
    #: `None` rather than absent on every chunk but the last. OpenAI omits the key on
    #: the chunks that have no usage, and a client that does `chunk.usage.total_tokens`
    #: without checking would otherwise raise on every chunk but the last.
    usage: dict[str, int] | None = None


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


#: The SSE frame terminator OpenAI sends and every OpenAI client library stops on.
#: Not a `data:` frame like the others: a client reading `data:` lines parses each as
#: JSON, and `[DONE]` is not JSON — so the frame is deliberately unparseable and is how
#: the client knows to stop.
SSE_DONE = "data: [DONE]\n\n"


def sse_frame(payload: dict[str, Any] | str) -> str:
    """One SSE frame.

    `data: `, the JSON, then a blank line — the three parts of an SSE event, per the
    wire format. The trailing newline pair is the frame separator and omitting it makes
    a client buffer the next event, so a stream that "works" but never flushes is the
    failure this line prevents.

    A bare string is passed through unquoted, which is how `SSE_DONE` is sent: the
    terminator must not become `"[DONE]"` because a client compares the payload text,
    not a parsed value.
    """
    body = payload if isinstance(payload, str) else json.dumps(payload, separators=(",", ":"))
    return f"data: {body}\n\n"


async def stream_completion(request: Request, body: RouteRequestBody) -> Response:
    """Authenticate, validate, route, stream, meter — the streamed half of the shared path.

    **This is the second reason `serve_completion` exists.** Before 1.3.0 a streamed
    request would have needed its own auth check, its own validation and its own router
    call, and the alias invariant — that a request reaches a provider on the terms
    `/v1/route` would have refused — would have been true only of non-streaming
    requests. A caller who discovered they could reach a provider by sending
    `stream: true` would have found the weaker path, which is the exact shape of bug
    this repository keeps guarding against.

    The order is `serve_completion`'s order, unchanged and for its reasons:

    1. **Authenticate, before the body is read and before any provider is contacted.**
       `require_bearer` is called here, at the top, exactly as it is on the
       non-streaming path — so an unauthenticated streaming request cannot make muse
       spend money and cannot learn whether a model name routes at all.
    2. **Validate**, before the headers are sent. This is why validation happens here
       rather than inside the generator: a `StreamingResponse` has already sent its
       `200` and `text/event-stream` by the time its body runs, so a 422 raised later
       could only be an SSE frame, and a caller reading problem+json would get a
       stream instead.
    3. **Open the stream**, by asking the router. A failure here is still a normal
       `problem+json` status — 404, 422, 503 — because no bytes have been sent yet.
    4. **Meter, after the walk.** Inside the generator, because the tokens only exist
       once the stream has been read to the end. The completion is what was metered and
       the bytes are what was sent, so a metering failure cannot produce a response
       that disagrees with the bill.

    **A failure after the headers is an SSE error frame, in OpenAI's shape.** An SSE
    response cannot carry a 503 once it has begun, so the only honest thing on the wire
    is a `data:` frame carrying the error envelope's own fields, and then `[DONE]`, so
    the client closes the stream the way it would on a success. It is the same
    envelope object `problem()` builds, not a second error shape — a caller that can
    parse one failure from this service can parse the other.
    """
    container = request.app.state.container
    # The bearer check, exactly as `serve_completion` performs it and with the same
    # vocabulary. **There is no `except AuthError` here on purpose.** `serve_completion`
    # has already run this check and taken its failure branch before it decides to call
    # this function, so an `AuthError` here would be unreachable — and an unreachable
    # error arm is worse than no arm: it reads as "this function refuses an
    # unauthenticated caller", which is exactly the guarantee a reader of this signature
    # is looking for, while in fact the guarantee lives one frame up. Making the check
    # unconditional and letting an unexpected `AuthError` propagate to the global
    # handler keeps the code honest about where the boundary is.
    await require_bearer(request)

    served_id = f"chatcmpl-{uuid.uuid4().hex[:24]}"
    created = int(time.time())
    trace = trace_id_for(request)
    messages = [(message.role, message.content) for message in body.messages]
    secrets = container.credentials_held()

    try:
        stream = container.router.stream(
            body.model,
            messages,
            max_tokens=body.max_tokens,
            temperature=body.sampling_temperature(),
            secrets=secrets,
        )
        # Opened eagerly, and only to the first chunk, so that every failure the chain
        # can still answer with a status is answered with one. Everything after this is
        # past the point of no return, and goes on the wire as an error frame.
        first = await anext(stream)
    except RouteNotFound as error:
        return problem(
            request, "not_found", 404, f"no route is configured for model {error.model!r}"
        )
    except ValueError as error:
        return problem(request, "validation_failed", 422, str(error))
    except AllCandidatesFailed as error:
        return problem(request, "unavailable", 503, _chain(error))

    include_usage = bool(body.stream_options and body.stream_options.include_usage)

    async def body_stream() -> AsyncIterator[str]:
        async for result in _prepend(first, stream):
            if result.settled is not None:
                if result.settled.error is not None:
                    # Recorded before the frame goes out, and for the same reason the
                    # success path meters before it returns: this is a fact about work
                    # that was done and not billed, and it must not depend on a client
                    # reading the stream to the end.
                    await _record_unmetered(result.settled, body, trace)
                    # **A failure after the headers, in OpenAI's shape.** The response
                    # is already `200 text/event-stream`, so a 503 can no longer be sent
                    # and pretending otherwise would leave the client waiting. The error
                    # goes on the wire as a `data:` frame carrying this service's own
                    # envelope — `error`, `message`, `type`, `param`, `code` — followed
                    # by `[DONE]` so the client ends the stream the way it ends a
                    # success rather than hanging on a connection nobody will close.
                    #
                    # The same envelope object `problem()` builds, so a caller that can
                    # parse one failure from this service can parse this one. A second,
                    # stream-only error shape would be a client that has to special-case
                    # muse exactly where it is least able to.
                    #
                    # `unavailable` rather than `internal`: the request reached a vendor
                    # and the vendor is why it failed, which is the 503 `/v1/route` would
                    # have returned had it not started.
                    yield sse_frame(_error_frame(result.settled.error, trace))
                    yield SSE_DONE
                    return
                await _meter_stream(container, result.settled, body, trace)
                if include_usage and result.settled.completion is not None:
                    yield sse_frame(
                        _chunk_body(
                            served_id,
                            created,
                            result.settled.completion.model,
                            [],
                            result.settled.completion,
                        )
                    )
                yield SSE_DONE
                return
            yield sse_frame(
                _chunk_body(served_id, created, body.model, [delta_of(result.chunk)], usage=None)
            )

    return StreamingResponse(
        body_stream(),
        media_type="text/event-stream",
        headers={TRACE_HEADER: trace, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _error_frame(error: BaseException, trace: str) -> dict[str, Any]:
    """A settled-stream failure as an SSE frame, in OpenAI's error shape.

    Seven keys, and they are OpenAI's: a client library raises from the `error` object
    rather than inspecting the frame, so a frame without `error.message` and
    `error.type` produces an unhelpful message on the client side however good the
    text is.

    `detail` is the scrubbed provider text the router produced — `muse.router._scrub`
    runs on every failure that reaches a caller, so a vendor echoing the key it
    rejected cannot reach the wire here either.
    """
    return {
        "error": {
            "message": str(error),
            "type": "unavailable",
            "param": None,
            "code": "upstream_error",
        },
        "trace_id": trace,
    }


async def _prepend(
    first: StreamResult, rest: AsyncIterator[StreamResult]
) -> AsyncIterator[StreamResult]:
    """The already-pulled first result, then the rest.

    The first chunk was pulled before the response existed so that a status could still
    be returned; it must not be lost for that. A one-line generator rather than a
    `chain`, because the two are not the same thing when the first value may be the only
    one and a caller abandons the iterator.
    """
    yield first
    async for result in rest:
        yield result


async def _record_unmetered(settled: SettledStream, body: RouteRequestBody, trace: str) -> None:
    """Log that a stream finished without producing a billable completion.

    **Silently recording nothing is not an option.** The *decision* not to meter an
    incomplete stream is defensible — a completion that did not happen is not a
    completion to bill — but being unable to tell afterwards that it happened is not, and
    an outbox row's absence is not queryable. One line turns "no row" into a fact an
    operator can count.

    Only the error's **class**, the route name and the trace id. Never the message,
    which is provider text and may echo a credential; never the prompt or the partial
    content, because a log line is retained and searchable by anyone with log access
    (rules 16 and 22). `test_a_failed_stream_is_recorded_...` asserts the absences with
    canaries rather than trusting the argument above.
    """
    logger.warning(
        "muse.stream.not_metered",
        extra={
            "reason": "stream_did_not_complete",
            "error_class": type(settled.error).__name__ if settled.error else None,
            "model": body.model,
            "trace_id": trace,
        },
    )


async def _meter_stream(
    container: Any,
    settled: SettledStream,
    body: RouteRequestBody,
    trace: str,
) -> None:
    """Meter a settled stream, or record why it was not metered.

    **A stream that did not complete is not metered**, and the error is logged rather
    than raised: the bytes are already on the wire, so raising here would produce a
    traceback on a response the caller has finished reading, and the completion being
    absent is not a bug in muse — it is the outcome the router already reported to the
    caller as an error frame. Silently recording nothing is what this refuses to do, so
    the refusal is recorded: the event that a completion was dropped is emitted rather
    than swallowed.

    **A metering failure on a completed stream propagates**, exactly as it does on the
    non-streaming path. The completion has been paid for; losing its record silently
    loses the spend, and here the caller cannot even be told, because the bytes are
    already gone. That asymmetry is the reason metering is not fire-and-forget here.
    """
    if settled.completion is None:
        logger.warning(
            "muse.stream.not_metered",
            extra={
                "reason": "stream_did_not_complete",
                "error_class": type(settled.error).__name__ if settled.error else None,
                "model": body.model,
                "trace_id": trace,
            },
        )
        return
    completion = settled.completion
    await container.meter.record(
        RoutedCompletion(
            completion=completion,
            route=settled.route or body.model,
            model=completion.model,
            candidates_tried=1,
            attempts=1,
        )
    )


def delta_of(chunk: StreamChunk | None) -> dict[str, Any]:
    """One chunk as OpenAI's `choices[0]`.

    **`index` and `delta` are the wrapper, and omitting them is not a simplification.**
    A client reads `chunk.choices[0].delta.content` to assemble the message and
    `chunk.choices[0].index` to know *which* choice it is; sending the delta fields
    directly in `choices[0]` produces frames that parse and assemble to an empty string
    on a stock client. That is why the frames are asserted by exact equality against
    OpenAI's shape rather than "the text is somewhere in there".

    `index` is always 0, as it is in `RouteResponseBody.choices`: `n > 1` is not
    supported by this version, and a client that saw a non-zero index would look for a
    second choice.

    `content` is omitted from the delta when the chunk carries none — the role-only
    opener and the finish-only closer. A `"content": null` key is legal but redundant,
    and omitting it is what OpenAI itself does.
    """
    if chunk is None:  # pragma: no cover - callers pass a chunk or settle instead
        return {"index": 0, "delta": {}}
    delta: dict[str, Any] = {}
    if chunk.role is not None:
        delta["role"] = chunk.role
    if chunk.content is not None:
        delta["content"] = chunk.content
    if chunk.finish_reason is not None:
        delta["finish_reason"] = chunk.finish_reason
    return {"index": 0, "delta": delta}


def _chunk_body(
    completion_id: str,
    created: int,
    model: str,
    choices: list[dict[str, Any]],
    usage: Completion | None,
) -> dict[str, Any]:
    """One chunk's JSON, in OpenAI's shape.

    Built by hand rather than through `CompletionChunkBody` because this is on the hot
    path of every frame of every stream, and the pydantic round trip per token-chunk is
    a cost with no benefit — the shape is fixed by this function and asserted by exact
    equality in the suite. `object` is `chat.completion.chunk`, which is what tells a
    client these frames are not whole completions.
    """
    body: dict[str, Any] = {
        "id": completion_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model,
        "choices": choices,
    }
    if usage is not None:
        body["usage"] = {
            "prompt_tokens": usage.tokens_in,
            "completion_tokens": usage.tokens_out,
            "total_tokens": usage.tokens_in + usage.tokens_out,
        }
    return body


async def serve_completion(request: Request) -> ServedCompletion | JSONResponse | Response:
    """Authenticate, validate, route, meter — the whole path both endpoints share.

    **This is the alias's reason for existing.** `POST /v1/chat/completions` calls this
    function and renders the result in OpenAI's shape; `POST /v1/route` calls it and
    renders it in muse's. There is no second routing call, no second metering call, and
    no second auth check, because there is one function that does all three and both
    endpoints delegate to it.

    Since 1.3.0 it also owns the streamed half: a body carrying `stream: true`
    dispatches to `stream_completion`, which does the same four steps in the same order
    and is reached from here rather than from either endpoint. The choice is made HERE,
    after the body has been validated and before any provider is contacted, so both
    endpoints agree on what a streamed request is by construction rather than by two
    handlers each having remembered.

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
    construction rather than by agreement between two pieces of code. A
    `StreamingResponse` return is a `stream: true` request, and is the only third shape
    this can return.
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

    if body.stream:
        # The authenticated principal has been established above and the body is
        # validated; `stream_completion` re-runs the bearer check rather than being
        # handed a principal, because a function that can be called with a principal
        # and a function that cannot tell whether there was one are different
        # functions, and the second is the one that cannot be called wrongly.
        return await stream_completion(request, body)

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
            "completion emits a `muse.tokens.consumed` event.\n\n"
            "**With `stream: true`** this returns `text/event-stream`: `data:` frames of "
            "`chat.completion.chunk`, terminated by `data: [DONE]`. The fallback chain "
            "runs until the first chunk and not after — once a caller holds bytes the "
            "next candidate cannot be used without splicing two answers into one body. "
            "A failure after the first chunk arrives as a `data:` frame carrying this "
            "service's `problem+json` envelope, followed by `[DONE]`."
        ),
    )
    async def route_completion(request: Request) -> Response:
        served = await serve_completion(request)
        if isinstance(served, JSONResponse | StreamingResponse):
            # `problem+json` failure, or a stream already built by the shared path.
            #
            # **`StreamingResponse` is checked here as carefully as `JSONResponse`
            # is.** `serve_completion` can now return either, so a handler that only
            # knew about the old one would take a `StreamingResponse` as if it were a
            # served completion and read `.id` off it — a 500 on every streamed request
            # from this endpoint, and the kind of failure that looks like a broken
            # router rather than a missed isinstance.
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
            "client reaches this service without knowing muse exists.\n\n"
            "**`stream: true` is supported** and behaves exactly as it does on "
            "`/v1/route`, because both go through one function. `stream_options."
            "{include_usage: true}` adds a final chunk carrying token usage; without it "
            "the usage is metered but not returned, which is what OpenAI's own default "
            "of `false` means."
        ),
    )
    async def create_chat_completion(request: Request) -> Response:
        served = await serve_completion(request)
        if isinstance(served, JSONResponse | StreamingResponse):
            # Both shapes the shared path can return without a completion to render —
            # see `route_completion` for why the second one is checked rather than
            # assumed away.
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
