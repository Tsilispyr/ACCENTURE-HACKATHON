"""Observability is three layers and only one of them always works.

The always-on layer is the LangGraph audit trail. It is ordinary application
state: no account, no container, no network, nothing to be unreachable. These
tests pin the two things that make it trustworthy - that the hosted layer
cannot half-enable itself, and that a trace reads in the order things actually
happened.
"""

from __future__ import annotations

import pytest

from agentcore import tracing


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    """configure() is idempotent by design, which tests must undo.

    The module caches its decision so a repeated call is free. Without this the
    first test to run would fix the answer for every test after it.
    """
    monkeypatch.setattr(tracing, "_configured", False)
    monkeypatch.delenv(tracing.PUBLIC_KEY, raising=False)
    monkeypatch.delenv(tracing.SECRET_KEY, raising=False)
    yield


def test_one_key_alone_does_not_enable_tracing(monkeypatch):
    """Langfuse needs BOTH keys, and a half-filled .env is the common case.

    Someone pastes the public key, gets distracted, and the handler would be
    constructed against an endpoint it cannot authenticate to. Reporting
    enabled=True there would be worse than reporting False, because /healthz
    is the thing people trust to tell them tracing is live.
    """
    monkeypatch.setenv(tracing.PUBLIC_KEY, "pk-not-a-real-key")

    assert tracing.configure() is False
    assert tracing.enabled() is False


def test_both_keys_enable_it(monkeypatch):
    """Credentials arriving is the whole trigger. No second thing to remember."""
    monkeypatch.setenv(tracing.PUBLIC_KEY, "pk-not-a-real-key")
    monkeypatch.setenv(tracing.SECRET_KEY, "sk-not-a-real-key")

    assert tracing.configure() is True
    assert tracing.enabled() is True


def test_status_always_names_the_layer_that_works(monkeypatch):
    """A banner that says nothing when the SaaS is off would be misleading.

    Tracing is never entirely off here, which is the point of the local layer.
    """
    tracing.configure()
    assert "LangGraph" in tracing.status()
    assert "Langfuse" not in tracing.status()

    monkeypatch.setenv(tracing.PUBLIC_KEY, "pk-not-a-real-key")
    monkeypatch.setenv(tracing.SECRET_KEY, "sk-not-a-real-key")
    assert "LangGraph" in tracing.status()
    assert "Langfuse" in tracing.status()


def test_a_re_entered_stage_is_headed_again():
    """The replan loop has to be visible, or the trace is actively misleading.

    Heading a stage only on FIRST sight files a replan's events under whichever
    stage ran in between - so a run that bounced off the gate and re-planned
    would read as though the gate did the re-planning. That is the exact
    reading this tool exists to make obvious.
    """
    audit = [
        {"stage": "s4_plan", "event": "planned"},
        {"stage": "s5_gate", "event": "interrupt"},
        {"stage": "s4_plan", "event": "replanned"},
    ]

    text = tracing.render(audit)
    lines = [line.strip() for line in text.splitlines() if line.strip()]

    assert lines.index("replanned") > lines.index("s4_plan   (visit 2)")
    assert text.count("s4_plan") == 2


def test_a_stage_total_is_shown_once_not_per_visit():
    """Repeating the total under each visit would read as a per-visit figure."""
    audit = [
        {"stage": "s4_plan", "event": "planned"},
        {"stage": "s5_gate", "event": "interrupt"},
        {"stage": "s4_plan", "event": "replanned"},
    ]

    text = tracing.render(audit, timings={"s4_plan": 2.5, "s5_gate": 0.1})

    assert text.count("2.5s total") == 1


def test_an_empty_trail_says_so_rather_than_printing_nothing():
    """A run that crashed in s1 must not look like a run that printed cleanly."""
    assert "no audit events" in tracing.render([])


def test_a_long_value_is_truncated_rather_than_wrapped():
    """A trace is read by scanning down. One wrapped value ruins that."""
    audit = [{"stage": "s4_plan", "event": "replanned", "reason": "x" * 400}]

    line = [l for l in tracing.render(audit).splitlines() if "replanned" in l][0]

    assert len(line) < 200
    assert line.endswith("...")


def test_the_langfuse_handler_is_absent_without_keys():
    """No keys, no handler - and every call site already tolerates None.

    This is what makes the hosted layer genuinely optional rather than
    merely broken when unconfigured.
    """
    import os

    from agentcore.llm import langfuse_handler

    langfuse_handler.cache_clear()
    if os.getenv("LANGFUSE_PUBLIC_KEY", "").strip():
        pytest.skip("Langfuse is configured in this environment")

    assert langfuse_handler() is None


def test_the_compiled_graph_keeps_get_state_when_tracing_is_bound():
    """`.with_config()` must not cost approval-resume.

    The handler is bound by wrapping the compiled graph. On a
    `CompiledStateGraph` that returns a `CompiledStateGraph`, so `get_state`
    and `stream` survive - but a binding that returned a `RunnableBinding`
    would break the console and `service.py`, which both reach through this
    for `get_state` to resume a paused run. Cheap to pin, expensive to find.
    """
    from langgraph.checkpoint.memory import MemorySaver

    from agentcore.pipeline.graph import build_app

    app = build_app(MemorySaver())

    assert hasattr(app, "get_state")
    assert hasattr(app, "stream")
    assert hasattr(app, "invoke")
