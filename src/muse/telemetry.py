"""W3C trace context, and the allowlist that decides what a span may carry.

PLAN §7 adopts W3C `traceparent` and OpenTelemetry for every service, with traces
landing in the platform collector. This module is the whole of that for muse, and it
is deliberately two things rather than one:

- **`TraceParent` and `parse_traceparent`** — the wire format. Parse it, continue it,
  or refuse it. A malformed header starts a new trace and never a 400: correlation
  metadata is an observability affordance, and an affordance that can take a
  customer's request down is a denial-of-service vector aimed at our own probe.
- **`ALLOWED_SPAN_ATTRIBUTES` and `record`** — the redaction boundary for telemetry.

The second is the part that needs arguing for, because it looks like over-caution
until you picture the alternative. A tracing backend is a searchable, retained,
widely-readable store. `muse.redaction` exists because prompts, completions and API
keys are text that must not end up in one. Adding spans creates a new destination for
exactly that text, and the realistic way it leaks is not a malicious actor — it is a
well-meaning engineer six months from now adding `span.set_attribute("prompt", ...)`
because it would be useful for debugging, in a service whose prompts are other
customers' data.

So the control is an **allowlist at one choke point**, not discipline at each call
site. `record()` is the only way an attribute reaches a span, and anything it does
not recognise does not land. `ALLOWED_SPAN_ATTRIBUTES` is asserted on directly in
`tests/test_telemetry.py`, including a test that no name in it contains `prompt`,
`content`, `message`, `body`, `header` or `query`.

Two things are deliberately *not* on the list:

- **`error.message`.** A vendor's error text is third-party text, and a
  content-policy rejection quotes the offending content back to you. `error.type` —
  the exception's class name — answers "what kind of failure" with no content in it.
- **Anything the caller sent.** The model name a client asked for is caller text, so
  it is recorded only after `RouteTable.find` has matched it to a configured route.
  Before that point the request produced a status and an error type, which is all an
  unknown model deserves.

The values are gated as well as the names. A `Secret` is refused on its *type*, not
by comparing its value — a filter with a bypass is the thing `redaction.py` argues
against, and refusing the type means there is no code path from the vault to a span.
Non-scalars are refused for the same reason: an object with a `__str__` is a value
that can be a list of the caller's prompts.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import Tracer, TracerProvider
from opentelemetry.trace import NoOpTracerProvider, Span, get_current_span, set_span_in_context

from muse.errors import ConfigError
from muse.redaction import Secret

#: The W3C trace-context header. Lowercase on purpose: HTTP header names are
#: case-insensitive and this is the spelling the spec publishes.
TRACEPARENT_HEADER = "traceparent"

#: The only version this build knows how to *interpret*. A higher version is still
#: parsed (see `parse_traceparent`) — the spec says to continue a well-formed trace
#: rather than discard it — but `ff` is forbidden outright because the spec reserves
#: it to mean "this is not a traceparent".
FORBIDDEN_VERSION = 0xFF

_TRACEPARENT_RE = re.compile(r"^([0-9a-f]{2})-([0-9a-f]{32})-([0-9a-f]{16})-([0-9a-f]{2})$")

#: The all-zero ids the spec reserves. Accepting them correlates every such request
#: in the system into a single trace, which is a denial of service against the
#: tracing backend rather than a cosmetic bug — so they are refused, not normalised.
_ZERO_TRACE_ID = "0" * 32
_ZERO_SPAN_ID = "0" * 16

#: The longest string any single attribute may hold.
#:
#: Defence in depth *behind* the allowlist, not a substitute for it. The canary test
#: uses a short canary precisely so this cannot be what makes it pass: a truncated
#: leak is still a leak, and a redaction rule proved by a length limit is a
#: redaction rule with a hole in it.
MAX_ATTRIBUTE_LENGTH = 256

#: The complete set of attribute names muse may put on a span.
#:
#: Read this list as the security control it is. Every name is a fact about *how* a
#: request was served — which vendor, which model, how many tokens, how long, what
#: class of failure — and none of them is a fact about *what the caller said*.
#:
#: muse's own attributes are underscored and set as keyword arguments; the
#: OpenTelemetry semantic conventions keep their dots and are set through the mapping
#: form. Two conventions rather than one, because a dotted name cannot be a Python
#: keyword argument, and rewriting the conventions to fit would make these attributes
#: unrecognisable next to every other OpenTelemetry tool in the ecosystem.
ALLOWED_SPAN_ATTRIBUTES: frozenset[str] = frozenset(
    {
        # Semantic conventions, and only the ones that cannot carry content. Never
        # `url.query`, never `http.request.header`, never the request body.
        "http.request.method",
        "http.response.status_code",
        "url.path",
        # Correlation. Set by the middleware from the span's own context, so it is a
        # generated or upstream id — never a fragment of either.
        "muse_trace_id",
        "muse_parent_span_id",
        # The routing decision.
        "muse_route",
        "muse_model",
        "muse_provider",
        "muse_candidate_index",
        "muse_candidates_tried",
        "muse_attempts",
        # The provider call.
        "muse_tokens_in",
        "muse_tokens_out",
        "muse_cost_micros",
        "muse_latency_ms",
        # Resilience.
        "muse_breaker_state",
        "muse_retry_attempt",
        "muse_retry_backoff_ms",
        "muse_retry_budget_exhausted",
        "muse_retry_indeterminate",
        # Errors: the exception's *class name* and nothing else. The message is
        # third-party text and a content-policy rejection quotes the offending
        # content back, so `error.message` is precisely the attribute that could
        # carry a prompt. It is not on this list and must not be added.
        "error.type",
    }
)


@dataclass(frozen=True, slots=True)
class TraceParent:
    """A parsed `traceparent`.

    Frozen because one of these is shared by the request span, the route span and
    every provider span of a single in-flight request.
    """

    trace_id: str
    span_id: str
    flags: int = 1
    version: int = 0

    @property
    def sampled(self) -> bool:
        """The caller's sampling decision, carried rather than upgraded.

        Defaulting this to sampled would let a service that happens to be tracing
        override a caller who asked not to be. The decision is theirs.
        """
        return bool(self.flags & 0x01)

    def header(self) -> str:
        """The wire form. Round-trips: parse then format is the identity.

        A caller that greps its own access log for the trace id it sent has to find
        that id on the vendor's side, or propagating it achieves nothing.
        """
        return f"{self.version:02x}-{self.trace_id}-{self.span_id}-{self.flags:02x}"


def parse_traceparent(value: str | None) -> TraceParent | None:
    """Parse a `traceparent`, or return `None`.

    `None` covers both "no header" and "unusable header", and the two are told apart
    by the middleware recording which it was — but the *action* is the same for both:
    start a new trace. A caller whose header is broken loses correlation, which is
    the correct outcome, because an id nobody can trust is worse than one nobody has.

    No exception, ever. This runs on the request path of every inbound call, before
    anything has validated anything.
    """
    if not value:
        return None
    match = _TRACEPARENT_RE.match(value)
    if match is None:
        return None
    version = int(match[1], 16)
    trace_id, span_id, flags = match[2], match[3], int(match[4], 16)
    if version == FORBIDDEN_VERSION:
        return None
    if trace_id == _ZERO_TRACE_ID or span_id == _ZERO_SPAN_ID:
        return None
    return TraceParent(trace_id=trace_id, span_id=span_id, flags=flags, version=version)


def record(span: Span, attributes: Mapping[str, object] | None = None, **kwargs: object) -> None:
    """Put attributes on a span, if they are allowed to be there.

    The only path from a value to a span. Four filters, in order of how much they
    protect against:

    1. **Unknown name** — dropped. This is the one that catches a future `muse.prompt`.
    2. **`Secret`** — dropped on its type. Never compared by value.
    3. **Non-scalar** — dropped. An object with a `__str__` can be anything.
    4. **Over-long string** — truncated to `MAX_ATTRIBUTE_LENGTH`.

    Accepts a mapping or keywords so a call site reads as the attributes it means to
    set, rather than assembling a dict first.
    """
    for name, value in {**(attributes or {}), **kwargs}.items():
        if name not in ALLOWED_SPAN_ATTRIBUTES:
            continue
        if isinstance(value, Secret) or not isinstance(value, (str, int, float, bool)):
            continue
        span.set_attribute(name, value[:MAX_ATTRIBUTE_LENGTH] if isinstance(value, str) else value)


def outbound_traceparent() -> str | None:
    """The `traceparent` to send on an outbound call, or `None`.

    Read from the ambient OpenTelemetry context rather than threaded through every
    provider's signature: the adapter should not need to know that tracing exists,
    and a `traceparent` argument on `Provider.complete` is one more thing every
    future provider has to remember to honour.

    `None` when nothing is tracing, so an unconfigured deployment sends no header
    rather than a fabricated trace id that would drop muse's spans into whatever
    trace the vendor assigned.
    """
    context = get_current_span().get_span_context()
    if not context.is_valid:
        return None
    return f"00-{context.trace_id:032x}-{context.span_id:016x}-{int(context.trace_flags):02x}"


@dataclass(frozen=True, slots=True)
class Telemetry:
    """The tracer, and the only way this service creates a span.

    Frozen: one per app, read by every concurrent request. A mutable tracer handle is
    a config change half the fleet sees and half does not — the same reason
    `muse.main.Container` is frozen (AGENTS.md rule 1).
    """

    tracer: Tracer

    @classmethod
    def noop(cls) -> Telemetry:
        """Tracing off: spans are created and discarded, and no trace is propagated.

        Uses the API's `NoOpTracerProvider` rather than a real `TracerProvider` with
        no span processors. That is not a stylistic choice — a real provider with no
        processors still produces *recording* spans with perfectly valid trace
        contexts, so muse would send a `traceparent` on every outbound call in a
        deployment that had deliberately configured no tracing at all. Propagating a
        trace nobody is collecting is how "we propagate the caller's trace" quietly
        becomes untrue.

        The no-op provider's spans carry an invalid context, which is also what stops
        the middleware echoing an all-zero `traceparent` — a header value the spec
        reserves and this module refuses to parse on the way in.
        """
        return cls(NoOpTracerProvider().get_tracer("muse"))

    @contextmanager
    def span(self, name: str, **attributes: object) -> Iterator[Span]:
        """Start a span, record the attributes that are allowed, and yield it.

        A context manager rather than a decorator because the span has to be
        *current* for the duration of the body — that is what makes the provider
        span a child of the route span and what lets `outbound_traceparent` see it
        from inside the adapter.
        """
        with self.tracer.start_as_current_span(name) as started:
            record(started, attributes)
            yield started


def parent_context(parsed: TraceParent) -> object:
    """An OpenTelemetry context continuing `parsed`, as a non-recording span.

    A `NonRecordingSpan` is the SDK's own representation of "someone upstream is
    already tracing this; take their ids". Using it rather than inventing a fresh
    context is what makes muse's span a *child* of the caller's rather than a
    separate trace that happens to share a number.
    """
    from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, TraceState

    return set_span_in_context(
        NonRecordingSpan(
            SpanContext(
                trace_id=int(parsed.trace_id, 16),
                span_id=int(parsed.span_id, 16),
                is_remote=True,
                trace_flags=TraceFlags(parsed.flags),
                trace_state=TraceState(),
            )
        )
    )


def build_provider(*, endpoint: str | None = None, service_name: str = "muse") -> TracerProvider:
    """The tracer provider for this configuration.

    With no `endpoint` — the default, and the case in development and in the whole
    test suite — this is a provider with **no span processor**, so spans are created,
    nested and closed correctly and then discarded. That is deliberate in both
    directions: the suite stays hermetic and no-socket (AGENTS.md rule 3), and a
    default install phones nobody. A collector address is something a deployment
    supplies, never something the code guesses.

    The OTLP exporter is an *optional extra* (`muse[otel]`), imported only when an
    endpoint is configured. As a hard dependency it would pull grpcio and protobuf
    into every install to serve a path most deployments never reach. When the
    variable is set and the extra is absent this raises a `ConfigError` naming both
    halves of the fix, rather than silently exporting nowhere — a configured
    collector that is silently absent is an outage nobody is paged for.
    """
    provider = TracerProvider(resource=_resource(service_name))
    if endpoint is None:
        return provider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider.add_span_processor(BatchSpanProcessor(_otlp_exporter(endpoint)))
    return provider


def _resource(service_name: str) -> Resource:
    return Resource.create(
        {
            "service.name": service_name,
            # The SDK's own defaults are misleading in a service that has a name of
            # its own: they read `unknown_service:python`, which sorts badly in every
            # collector's service list.
            "service.namespace": "cafaye",
        }
    )


def _otlp_exporter(endpoint: str) -> object:
    """The OTLP HTTP exporter, or a boot failure that names the fix."""
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    except ImportError as error:
        raise ConfigError(
            "MUSE_OTEL_EXPORTER_OTLP_ENDPOINT is set but the OTLP exporter is not "
            "installed; install muse[otel] to export traces, or unset the variable "
            "to export nowhere"
        ) from error
    return OTLPSpanExporter(endpoint=endpoint)
