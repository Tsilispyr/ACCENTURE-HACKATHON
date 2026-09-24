"""Azure Application Insights and Azure Monitor, over OpenTelemetry.

Handout section 11 and the Definition of Done ask for traces of the agent
workflow, LLM calls, latency, tokens, MCP and specialist interactions,
exceptions and evaluation results - exported to App Insights.

WHAT THIS FILE IS NOT. It is not a second tracing backend fighting the first.
Langfuse stays: it is where an engineer reads a single run's prompts and
outputs while building, bound once in `pipeline/graph.py`. App Insights is
where an operator watches a fleet - latency distributions, failure rates,
exceptions over time. They answer different questions, and both are env-var
gated, so a machine with neither configured runs normally and silently.

THE ONE RULE HERE: never let telemetry break the thing it observes. Every entry
point catches broadly and degrades to a no-op. A dead exporter must cost a
trace, never a request.

    from agentcore import observability
    observability.configure()          # idempotent, safe to call anywhere

Set APPLICATIONINSIGHTS_CONNECTION_STRING to turn it on.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any, Iterator

# Importing for the side effect: llm.py holds the only load_dotenv() in the
# codebase, and the connection string arrives in .env like everything else.
# Without this, configure() reads an environment that has not been loaded yet.
import agentcore.llm  # noqa: F401

CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"

_configured = False
_enabled = False


def enabled() -> bool:
    """Is Azure Monitor actually exporting? Reported by /healthz.

    A silent integration is worse than a broken one: tracing failures return
    empty rather than raise, so a dead exporter can hide for a long time behind
    a green health check.
    """
    return _enabled


def configure(service_name: str | None = None) -> bool:
    """Wire Azure Monitor if a connection string is set. Idempotent.

    Returns True when telemetry is live, False when it is off or unavailable -
    both of which are ordinary states, not errors.
    """
    global _configured, _enabled

    if _configured:
        return _enabled
    _configured = True

    connection = os.getenv(CONNECTION_STRING, "").strip()
    if not connection:
        return False

    try:
        from azure.monitor.opentelemetry import configure_azure_monitor

        configure_azure_monitor(
            connection_string=connection,
            # The nine pipeline stages are already the span names, so a trace
            # reads s1_intake -> ... -> s9_guard_out without any mapping.
            logger_name="agentcore",
        )
        os.environ.setdefault(
            "OTEL_SERVICE_NAME",
            service_name or os.getenv("OTEL_SERVICE_NAME") or "hackathon2",
        )
        _enabled = True
    except Exception:  # noqa: BLE001 - telemetry must never break the request
        _enabled = False

    return _enabled


def instrument_fastapi(app: Any) -> None:
    """Trace HTTP requests, if telemetry is on. Safe to call unconditionally."""
    if not _enabled:
        return
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(app)
    except Exception:  # noqa: BLE001
        pass


def tracer() -> Any | None:
    if not _enabled:
        return None
    try:
        from opentelemetry import trace

        return trace.get_tracer("agentcore")
    except Exception:  # noqa: BLE001
        return None


@contextmanager
def span(name: str, **attributes: Any) -> Iterator[Any]:
    """One span, with the request and actor already attached.

    A no-op context manager when telemetry is off, so call sites never branch.
    """
    try:
        handle = tracer()
    except Exception:  # noqa: BLE001 - `tracer` guards itself, but this wraps
        handle = None  # every stage of the graph, so it gets belt and braces

    if handle is None:
        yield None
        return

    try:
        from agentcore.world import current_actor, current_request_id

        with handle.start_as_current_span(name) as active:
            # Correlation comes free: world.py already carries these per request.
            for key, value in (
                ("request.id", current_request_id()),
                ("actor.id", getattr(current_actor(), "id", "")),
                ("actor.scope", getattr(current_actor(), "scope", "")),
                *attributes.items(),
            ):
                if value not in (None, ""):
                    active.set_attribute(key, value)
            yield active
    except Exception:  # noqa: BLE001
        yield None


def record_audit(active: Any, events: list[dict[str, Any]]) -> None:
    """Attach audit events to a span.

    They are span EVENTS rather than spans: `audit_event` records what happened
    and carries no timings, so there is no start/end pair to turn into a span.
    Pretending otherwise would invent durations.
    """
    if active is None:
        return
    try:
        for event in events:
            active.add_event(
                f"{event.get('stage', '')}.{event.get('event', '')}",
                {
                    key: (value if isinstance(value, (str, int, float, bool)) else str(value))
                    for key, value in event.items()
                    if key not in ("stage", "event")
                },
            )
    except Exception:  # noqa: BLE001
        pass


def record_metrics(name: str, values: dict[str, float], **attributes: Any) -> None:
    """Publish evaluation results as telemetry.

    Section 11 asks for evaluation results in Azure Monitor, not only in the
    ledger. The ledger stays the record of truth; this is so a gate regression
    is visible on the same dashboard as a latency spike.
    """
    if not _enabled:
        return
    try:
        from opentelemetry import metrics as otel_metrics

        meter = otel_metrics.get_meter("agentcore.evaluation")
        for key, value in values.items():
            meter.create_gauge(f"{name}.{key}").set(float(value), attributes or None)
    except Exception:  # noqa: BLE001
        pass
