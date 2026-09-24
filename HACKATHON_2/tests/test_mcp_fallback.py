"""FR14: a tool, retrieval or agent failure must not take the run down.

This covers the TRANSPORT half specifically - an MCP server that times out or
is unreachable - by making `_run_agent` raise. The tool-returns-an-error half
and the specialist-raises half have their own tests in test_vendor_assessment
and test_delegation.
"""

from __future__ import annotations

import pytest

from agentcore.contracts import Request
from agentcore.pipeline.graph import build_app
from agentcore.pipeline.s2_guard_in import InjectionVerdict
from agentcore.pipeline.s3_ground import Sufficiency
from agentcore.pipeline.s4_plan import Draft, DraftStep
from agentcore.pipeline.s7_replan import Replan
from domains.deterministic import DeterministicReport

pytestmark = pytest.mark.workflow


def script(llm, *, steps, done=True, enough=True, summary="done"):
    """Queue one full pass: guard -> grade -> plan -> (replan) -> compose."""
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=enough, missing="" if enough else "the policy"))
    llm.queue(Draft, Draft(steps=steps))
    llm.queue(Replan, Replan(done=done, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary=summary, resolved=done))
    return llm


def invoke(app, text, actor, **config_extra):
    config = {"configurable": {"thread_id": f"t-{abs(hash(text)) % 10000}", **config_extra}}
    state = {"request": Request(id="r1", raw_text=text, actor=actor), "actor": actor}
    return app.invoke(state, config), config


def stages(result) -> list[str]:
    return [e["stage"] for e in result.get("audit", [])]


def events(result) -> list[str]:
    return [e["event"] for e in result.get("audit", [])]


# -------------------------------------------------------- failure & fallback ---


def test_tool_failure_is_handled_gracefully_without_crashing(llm, store, domain, actor, monkeypatch):
    """FR14: Handle at least one tool, retrieval or agent failure without crashing."""
    # Plan a single step that calls a tool, so there is something to fail.
    script(
        llm,
        steps=[DraftStep(description="Call flaky tool", tool_hint="flaky_tool")],
        summary="Assessment finished despite tool error.",
    )

    # Simulate the TRANSPORT failing rather than the tool returning an error:
    # a timeout or a dead MCP server, which is the case a try/except around a
    # tool call does not cover.
    def mock_failing_run_agent(agent, message):
        raise ConnectionError("MCP server connection timeout / tool failed")

    # Patched at _run_agent, the single point every step goes through.
    monkeypatch.setattr("agentcore.pipeline.s6_act._run_agent", mock_failing_run_agent)

    app = build_app()
    result, _ = invoke(app, "Assess vendor with external tool", actor)

    # 1. the run did not crash
    assert result is not None
    # 2. s6_act caught it and recorded step_failed, so the failure is ON THE
    #    RECORD rather than silently swallowed
    assert "step_failed" in events(result)
    # 3. the pipeline still reached compose and the output guard, so a dead
    #    tool degrades the answer instead of losing it
    assert "s8_compose" in stages(result)
    assert "s9_guard_out" in stages(result)