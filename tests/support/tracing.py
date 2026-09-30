"""A tracer that keeps its spans in memory, so a test can assert on them.

Not a mock. This is the real OpenTelemetry SDK with its real `InMemorySpanExporter`
swapped in for the OTLP one, so what a test reads is the payload an operator's
collector would receive — attribute names, attribute values, and all. Asserting on a
hand-built dict instead would prove that the *test's* idea of the payload is
correct, which is not the property under test.

The exporter is in-memory precisely so nothing leaves the process: a test that
dialled a collector would be a network call in a suite whose rule is no socket
(AGENTS.md rule 3), and a suite that phones an observability backend is a suite
that phones an observability backend.
"""

from __future__ import annotations

from typing import Any

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from muse.telemetry import Telemetry


def recording_telemetry() -> tuple[Telemetry, InMemorySpanExporter]:
    """A `Telemetry` whose spans land in the returned exporter.

    Returns both, because a test that cannot read the exporter cannot assert the
    redaction rule, and the redaction rule is the point of the module.
    """
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    return Telemetry(provider.get_tracer("muse.test")), exporter


def payloads(exporter: InMemorySpanExporter) -> list[dict[str, Any]]:
    """Every finished span as `{"name": ..., "attributes": {...}}`.

    Deliberately not the SDK's own `to_json`: this is the minimum a security
    assertion needs, and hand-building it means the assertion reads as a list of
    facts rather than as a round trip through a serialiser.
    """
    return [
        {"name": span.name, "attributes": dict(span.attributes or {})}
        for span in exporter.get_finished_spans()
    ]


def names(exporter: InMemorySpanExporter) -> list[str]:
    """Just the span names, in the order they finished."""
    return [span.name for span in exporter.get_finished_spans()]


def by_name(exporter: InMemorySpanExporter, name: str) -> list[dict[str, Any]]:
    """The payload of every span called `name`."""
    return [span for span in payloads(exporter) if span["name"] == name]


def rendered(exporter: InMemorySpanExporter) -> str:
    """The whole export rendered as one string.

    This is the string the canary test searches. Every attribute of every span, so
    a leak through a *value* is caught even when the leak is not through a key the
    allowlist was written to catch — which is the failure mode a key-by-key
    assertion cannot see.
    """
    return repr(payloads(exporter))
