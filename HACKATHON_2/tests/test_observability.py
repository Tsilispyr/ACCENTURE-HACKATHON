"""Telemetry must never break the thing it observes.

That is the whole contract of this module. Azure Monitor is off on a laptop,
off in CI, and off for anyone who has not pasted a connection string - so the
no-op path is the one that runs almost always, and it has to be genuinely
invisible rather than merely quiet.

The failure mode being guarded against is specific: an exporter that cannot
reach its endpoint, a library version that moved a symbol, a malformed
connection string. Any of those raising inside a graph node would turn a
working assessment into a failed one for the sake of a trace nobody was
reading.
"""

from __future__ import annotations

import pytest

from agentcore import observability

pytestmark = pytest.mark.workflow


@pytest.fixture(autouse=True)
def reset():
    """configure() is idempotent by design, so tests must clear its latch."""
    observability._configured = False
    observability._enabled = False
    yield
    observability._configured = False
    observability._enabled = False


def test_no_connection_string_means_telemetry_is_off(monkeypatch):
    monkeypatch.delenv(observability.CONNECTION_STRING, raising=False)
    assert observability.configure() is False
    assert observability.enabled() is False


def test_a_blank_connection_string_is_off_not_broken(monkeypatch):
    """A .env shipped with the key present and empty is the NORMAL state."""
    monkeypatch.setenv(observability.CONNECTION_STRING, "   ")
    assert observability.configure() is False


def test_configure_is_idempotent(monkeypatch):
    monkeypatch.delenv(observability.CONNECTION_STRING, raising=False)
    assert observability.configure() == observability.configure()


def test_a_broken_exporter_does_not_raise(monkeypatch):
    """The load-bearing test: a dead endpoint costs a trace, never a request."""
    monkeypatch.setenv(observability.CONNECTION_STRING, "InstrumentationKey=nonsense")

    def explode(**_):
        raise RuntimeError("exporter unreachable")

    monkeypatch.setattr(
        "azure.monitor.opentelemetry.configure_azure_monitor", explode, raising=False
    )
    assert observability.configure() is False
    assert observability.enabled() is False


def test_span_is_a_no_op_when_telemetry_is_off(monkeypatch):
    monkeypatch.delenv(observability.CONNECTION_STRING, raising=False)
    observability.configure()
    with observability.span("s1_intake", extra="value") as active:
        assert active is None


def test_span_yields_even_if_the_tracer_explodes(monkeypatch):
    """A caller must always get its body run, whatever the tracer does."""
    monkeypatch.setattr(observability, "_enabled", True)
    monkeypatch.setattr(observability, "tracer", lambda: (_ for _ in ()).throw(RuntimeError()))
    ran = False
    with observability.span("s6_act"):
        ran = True
    assert ran


def test_recording_audit_events_on_no_span_is_harmless():
    observability.record_audit(None, [{"stage": "s1_intake", "event": "request_parsed"}])


def test_recording_metrics_while_off_is_harmless(monkeypatch):
    monkeypatch.delenv(observability.CONNECTION_STRING, raising=False)
    observability.configure()
    observability.record_metrics("agent", {"pass_rate": 0.58})


def test_instrumenting_an_app_while_off_is_harmless(monkeypatch):
    monkeypatch.delenv(observability.CONNECTION_STRING, raising=False)
    observability.configure()
    observability.instrument_fastapi(object())


# ----------------------------------------------------------- the pipeline ---


def test_every_stage_runs_through_a_span_wrapper():
    """Span names must equal node names, with no mapping table to drift."""
    from agentcore.pipeline.graph import build_graph

    nodes = set(build_graph().nodes)
    expected = {
        "s1_intake", "s2_guard_in", "s3_ground", "s4_plan", "s5_gate",
        "s6_act", "s7_replan", "s8_compose", "s9_guard_out",
    }
    assert expected <= nodes


def test_the_graph_still_runs_with_telemetry_off(llm, store, domain, actor):
    """The wrapper sits on every node, so this is the regression that matters."""
    from agentcore.contracts import Request
    from agentcore.pipeline.graph import build_app
    from agentcore.pipeline.s2_guard_in import InjectionVerdict
    from agentcore.pipeline.s3_ground import Sufficiency
    from agentcore.pipeline.s4_plan import Draft, DraftStep
    from agentcore.pipeline.s7_replan import Replan
    from domains.deterministic import DeterministicReport

    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True, missing=""))
    llm.queue(Draft, Draft(steps=[DraftStep(description="Check it")]))
    llm.queue(Replan, Replan(done=True, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary="ok", resolved=True))

    state = build_app().invoke(
        {"request": Request(id="r1", raw_text="When is escalation?", actor=actor),
         "actor": actor},
        {"configurable": {"thread_id": "obs-1"}},
    )
    assert state["answer"] is not None


def test_healthz_reports_whether_azure_monitor_is_live():
    """A silent integration is worse than a broken one."""
    from fastapi.testclient import TestClient

    from agentcore.api.service import app

    body = TestClient(app).get("/healthz").json()
    assert "azure_monitor_enabled" in body
    assert "tracing_enabled" in body, "the two are reported separately, on purpose"
