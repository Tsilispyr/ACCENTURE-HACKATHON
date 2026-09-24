"""Delegation: the executor handing a step to a specialist, end to end and offline.

FR07 requires agent-to-agent interaction with at least two specialist agents,
and it is worth 10 of the 100 scoring points. Three specialists had been wired
as deep-agent subagents for a while and `agent_delegation` measured **0.00** on
every one of twelve eval cases.

Nothing in this file existed before, because nothing COULD: offline tests never
reached a deep agent at all. `s6_act` builds one with `chat_model()`, which in
tests is a ScriptedLLM and not a BaseChatModel, so construction raised and the
`except` recorded `step_failed`. Every test tolerated that, and one relied on
it. The path that delegates had never run under test, which is why a capability
that looked wired shipped doing nothing.

The `deep_agent` fixture in conftest.py is what makes it reachable.
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
from evaluation import metrics, trajectory

pytestmark = pytest.mark.workflow


def script(llm, *, steps, done=True, summary="done"):
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True, missing=""))
    llm.queue(Draft, Draft(steps=steps))
    llm.queue(Replan, Replan(done=done, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary=summary, resolved=done))
    return llm


def invoke(app, text, actor):
    config = {"configurable": {"thread_id": f"t-{abs(hash(text)) % 10000}"}}
    state = {"request": Request(id="r1", raw_text=text, actor=actor), "actor": actor}
    return app.invoke(state, config)


def audit(result, event: str) -> list[dict]:
    return [e for e in result.get("audit", []) if e.get("event") == event]


# ------------------------------------------------------- it actually fires ---


def test_a_step_with_an_owner_delegates_to_that_owner(llm, store, domain, actor, deep_agent):
    """The whole point. A plan naming an owner produces a `task` call to it."""
    script(llm, steps=[DraftStep(description="Review the handbook rule",
                                 owner="handbook_reviewer")])
    deep_agent.queue_delegation("handbook_reviewer", "The handbook says 30 minutes.")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    done = audit(result, "step_done")
    assert done, "the step never completed"
    assert done[0]["delegated"] == 1
    assert done[0]["delegated_to"] == ["handbook_reviewer"]


def test_the_owner_reaches_the_executor_prompt(llm, store, domain, actor, deep_agent):
    """The assignment must be IN the prompt, not merely in the plan."""
    script(llm, steps=[DraftStep(description="Review it", owner="service_reviewer")])
    deep_agent.queue_delegation("service_reviewer")

    invoke(build_app(), "Which service may be restarted?", actor)

    prompt = deep_agent.last["system_prompt"]
    assert "ASSIGNED TO service_reviewer" in prompt
    assert 'subagent_type="service_reviewer"' in prompt


def test_the_planner_learned_specialists_exist(llm, store, domain, actor, deep_agent):
    """`owners` on the planned event is the evidence. Empty means nothing can delegate."""
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.queue_delegation("handbook_reviewer")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    assert audit(result, "planned")[0]["owners"] == ["handbook_reviewer"]


def test_an_unowned_step_is_not_forced_to_delegate(llm, store, domain, actor, deep_agent):
    """Nothing ever tells the model to always call `task`. No owner, no delegation."""
    script(llm, steps=[DraftStep(description="Check the handbook", tool_hint="search_corpus")])
    deep_agent.queue_tool_calls("search_corpus")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    done = audit(result, "step_done")
    assert done[0]["delegated"] == 0
    assert done[0]["delegated_to"] == []


# ------------------------------------------------------------ the guards ----


def test_the_planner_may_not_invent_a_specialist(llm, store, domain, actor, deep_agent):
    """An owner naming nobody is worse than no owner: it hands work to a ghost."""
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True, missing=""))
    llm.queue(
        Draft,
        Draft(steps=[DraftStep(description="Review it", owner="ghost_reviewer")]),
        Draft(steps=[DraftStep(description="Review it", owner="handbook_reviewer")]),
    )
    llm.queue(Replan, Replan(done=True, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary="ok", resolved=True))
    deep_agent.queue_delegation("handbook_reviewer")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    assert audit(result, "invented_specialists")[0]["specialists"] == ["ghost_reviewer"]


def test_a_specialist_can_never_reach_a_tool_the_step_did_not_declare(
    llm, store, domain, actor, deep_agent
):
    """THE load-bearing safety test.

    s5_gate approved exactly what this step may do. If a specialist could reach
    further, delegation would be an allowlist escape wearing a helpful hat.
    """
    script(llm, steps=[DraftStep(description="Check it", tool_hint="search_corpus")])
    deep_agent.queue_delegation("handbook_reviewer")

    invoke(build_app(), "When does an incident get escalated?", actor)

    built = deep_agent.last
    allowed = {t.name for t in built["tools"]}
    for spec in built["subagents"]:
        reachable = {t.name for t in spec["tools"]}
        assert reachable <= allowed, f"{spec['name']} reaches {reachable - allowed}"


def test_specialists_are_isolated_not_forked(llm, store, domain, actor, deep_agent):
    """A forked subagent inherits the parent prompt AND a real `task` tool.

    Isolated ones get no `task` at all, so recursive delegation is structurally
    impossible rather than merely discouraged.
    """
    script(llm, steps=[DraftStep(description="Check it")])
    deep_agent.queue_delegation("handbook_reviewer")

    invoke(build_app(), "When does an incident get escalated?", actor)

    for spec in deep_agent.last["subagents"]:
        assert spec.get("mode", "isolated") == "isolated"


# -------------------------------------------------- the measurement holds ---


def test_delegation_is_recorded_even_when_the_step_fails(
    llm, store, domain, actor, deep_agent
):
    """The blind spot: `delegated` used to be written only on the success path.

    A run that delegated and THEN tripped the wire measured zero delegations,
    which is indistinguishable from one that never delegated. The audit trail
    agreed with a broken run because it never got far enough to disagree.
    """
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.queue_delegation("handbook_reviewer", **{"__interrupt__": ["outside"]})

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    tripped = audit(result, "TRIPWIRE_unapproved_action")
    assert tripped, "the tripwire did not fire"
    assert tripped[0]["delegated"] == 1
    assert tripped[0]["delegated_to"] == ["handbook_reviewer"]
    assert metrics.agent_delegation(result["audit"], expected=True).passed


def test_a_specialist_that_errors_does_not_take_the_run_down(
    llm, store, domain, actor, deep_agent
):
    """The agent failure test the handout's section 12 asks for.

    Section 12 wants at least one tool, retrieval or AGENT failure handled
    without crashing. MCP failure is covered by its own test; this is the
    agent half - a specialist that raises mid-step must not take the run down.
    """
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.raise_next(RuntimeError("specialist unavailable"))

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    assert result["answer"] is not None, "a failed specialist killed the run"
    failed = audit(result, "step_failed")
    assert failed and "specialist unavailable" in failed[0]["error"]


# ------------------------------------------------------------ anti-gaming ---


def test_delegating_to_general_purpose_does_not_satisfy_an_owned_step(
    llm, store, domain, actor, deep_agent
):
    """deepagents auto-injects a `general-purpose` subagent with broad reach.

    Counting `task` calls alone would let a hand-off to THAT satisfy a metric
    about specialists. The trajectory check compares against the plan's own
    declared owner, so it cannot.
    """
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.queue_delegation("general-purpose")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    check = trajectory.owned_steps_were_delegated(result)
    assert not check.passed
    assert "handbook_reviewer" in check.detail


def test_the_owned_step_check_passes_when_the_named_owner_ran(
    llm, store, domain, actor, deep_agent
):
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.queue_delegation("handbook_reviewer")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    assert trajectory.owned_steps_were_delegated(result).passed


def test_the_owned_step_check_is_silent_when_no_step_declared_an_owner(
    llm, store, domain, actor, deep_agent
):
    """Every existing eval case must be unaffected by this check."""
    script(llm, steps=[DraftStep(description="Check it", tool_hint="search_corpus")])
    deep_agent.queue_tool_calls("search_corpus")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    check = trajectory.owned_steps_were_delegated(result)
    assert check.passed and "no step declared an owner" in check.detail


# ------------------------------------------------- it reaches the answer ----


def test_the_specialist_is_named_in_what_the_composer_sees(
    llm, store, domain, actor, deep_agent
):
    """Attribution is what makes delegation improve the answer, not just a metric."""
    script(llm, steps=[DraftStep(description="Review it", owner="handbook_reviewer")])
    deep_agent.queue_delegation("handbook_reviewer", "Escalation is 30 minutes.")

    result = invoke(build_app(), "When does an incident get escalated?", actor)

    results = result.get("past_steps", [])
    assert results and results[0].owner == "handbook_reviewer"


def test_the_approval_gate_shows_who_will_execute_a_step(llm, store, domain, actor, deep_agent):
    """A reviewer approving a step must be shown who executes it."""
    from agentcore.pipeline import s5_gate

    script(llm, steps=[DraftStep(description="Restart it", tool_hint="restart_service",
                                 risk="high", owner="service_reviewer")])
    deep_agent.queue_delegation("service_reviewer")

    app = build_app()
    config = {"configurable": {"thread_id": "gate-owner"}}
    state = app.invoke(
        {"request": Request(id="r1", raw_text="Restart the payment service", actor=actor),
         "actor": actor},
        config,
    )

    assert "__interrupt__" in state
    steps = state["__interrupt__"][0].value["steps"]
    assert steps[0]["owner"] == "service_reviewer"
    assert s5_gate is not None
