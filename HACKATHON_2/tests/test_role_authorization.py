"""Handout section 9: "Restrict sensitive MCP tools according to role/authorization".

Each role may call tools up to a risk ceiling: user low, admin high. A tool above the ceiling is swapped for a stand-in with the same name that
does nothing and says why, so a person watching the run sees the refusal.
"""

from __future__ import annotations

import pytest
from langchain_core.tools import tool

from agentcore.contracts import RISK_ORDER, Actor, PlanStep
from agentcore.tools.registry import (
    LOWEST_CEILING,
    ROLE_MAX_RISK,
    restrict_by_role,
    role_ceiling,
    tools_for_step,
)
from agentcore.world import bound

pytestmark = pytest.mark.workflow

CALLS: list[str] = []


@tool
def lookup(query: str) -> str:
    """Read only."""
    CALLS.append("lookup")
    return "found"


@tool
def calculate(amount: float) -> str:
    """A calculation."""
    CALLS.append("calculate")
    return "computed"


@tool
def record(vendor: str) -> str:
    """Writes a permanent record."""
    CALLS.append("record")
    return "recorded"


FLOORS = {"lookup": "low", "calculate": "medium", "record": "high"}
TOOLS = [lookup, calculate, record]


@pytest.fixture(autouse=True)
def _clear_calls():
    CALLS.clear()


def names_that_actually_run(role: str) -> list[str]:
    """Call every tool for this role and report which ones did real work."""
    for t in restrict_by_role(TOOLS, FLOORS, role):
        payload = {"query": "x"} if t.name == "lookup" else {"amount": 1.0} if t.name == "calculate" else {"vendor": "v"}
        t.invoke(payload)
    return list(CALLS)


def test_a_user_can_use_everything_except_the_tools_that_write():
    """CHANGED CONTRACT. This asserted a user could only read.

    The handout says one thing about this (section 9): "restrict SENSITIVE MCP
    tools according to role/authorization". It names no roles and defines no
    per-role permissions, so which tools are sensitive is our call, and the
    honest reading is the ones that CHANGE something.

    calculate_tco and get_budget change nothing. Denying them cost the
    commercial reviewer the tool that computes the total while leaving the
    write tools as the only thing actually restricted, which is backwards.
    """
    assert names_that_actually_run("user") == ["lookup", "calculate"]


def test_engineer_can_calculate_but_not_write():
    """CHANGED CONTRACT. This asserted engineer had the lowest ceiling.

    `engineer` is not a legacy label: it is what the terminal console, the chat
    UI and the demo accounts alice and bob all run as. Leaving it unlisted
    denied calculate_tco and get_budget on every one of those paths, so the
    commercial reviewer got a denial stand-in and its finding rested on
    nothing. agent_eval runs as admin and never saw it (PROBLEMS P60).

    Medium, not high: the calculation yes, the write tools no.
    """
    assert role_ceiling("engineer") == "medium"
    assert names_that_actually_run("engineer") == ["lookup", "calculate"]


def test_an_admin_can_use_everything():
    assert names_that_actually_run("admin") == ["lookup", "calculate", "record"]


# `procurement` was here as an example of an unlisted role. It is listed now,
# for the same reason as `engineer`: it is a role this system issues to a
# person doing the job. `ADMIN` stays, because case sensitivity is the point.
@pytest.mark.parametrize("role", ["anonymous", "", "ADMIN", "root", "auditor"])
def test_a_role_nobody_listed_gets_the_lowest_ceiling(role):
    """Fails closed: being unlisted must never be a way in. Case matters too."""
    assert role_ceiling(role) == "low"
    assert names_that_actually_run(role) == ["lookup"]


def test_a_denied_tool_says_why_and_keeps_its_name_and_arguments():
    restricted = {t.name: t for t in restrict_by_role(TOOLS, FLOORS, "user")}

    assert set(restricted) == {"lookup", "calculate", "record"}
    reply = restricted["record"].invoke({"vendor": "Asteria"})
    assert reply.startswith("Denied: role 'user'")
    assert "high risk" in reply and "Nothing was executed" in reply
    assert restricted["record"].metadata["denied_for_role"] == "user"
    assert "record" not in CALLS


def test_a_permitted_tool_is_the_original_object_not_a_copy():
    restricted = {t.name: t for t in restrict_by_role(TOOLS, FLOORS, "admin")}
    assert restricted["lookup"] is lookup
    assert restricted["calculate"] is calculate
    assert restricted["record"] is record


# ------------------------------------------------ through the real registry --


def _restart_step():
    return PlanStep(id="1", description="Restart it", tool_hint="restart_service")


@pytest.mark.parametrize("role", ["user", "engineer"])
def test_restart_service_is_denied_below_admin(domain, role):
    with bound(Actor(id="t", role=role, scope="public")):
        tools = {t.name: t for t in tools_for_step(domain, _restart_step())}

    reply = tools["restart_service"].invoke({"service": "payments"})
    assert reply.startswith("Denied")


def test_restart_service_runs_for_an_admin(domain):
    import domains.deterministic as det

    det.reset()
    with bound(Actor(id="t", role="admin", scope="public")):
        tools = {t.name: t for t in tools_for_step(domain, _restart_step())}

    assert "restarted" in tools["restart_service"].invoke({"service": "payments"})


def test_a_step_that_names_no_tool_is_unaffected_by_role(domain):
    """Read-only retrieval is available to every role, however low."""
    with bound(Actor(id="t", role="user", scope="public")):
        names = [t.name for t in tools_for_step(domain, PlanStep(id="1", description="Look"))]
    assert names == ["search_corpus", "get_by_locator"]


def test_an_unbound_caller_is_anonymous_and_therefore_read_only(domain):
    """Nothing bound the actor: the ContextVar default is ANONYMOUS."""
    from agentcore.world import _actor
    from agentcore.contracts import ANONYMOUS

    token = _actor.set(ANONYMOUS)
    try:
        tools = {t.name: t for t in tools_for_step(domain, _restart_step())}
    finally:
        _actor.reset(token)

    assert tools["restart_service"].invoke({"service": "payments"}).startswith("Denied")


# ------------------------------------- the gate refuses before it asks ------
# A user or engineer whose plan needs a role they lack used to see the approval
# prompt, click approve, and only THEN be told "Denied". Nobody should be asked
# to approve what the requester could never have been allowed to run.

from langgraph.types import Command  # noqa: E402

from agentcore.contracts import Plan, Request  # noqa: E402
from agentcore.pipeline import s5_gate  # noqa: E402
from agentcore.pipeline.graph import build_app  # noqa: E402
from agentcore.pipeline.s2_guard_in import InjectionVerdict  # noqa: E402
from agentcore.pipeline.s3_ground import Sufficiency  # noqa: E402
from agentcore.pipeline.s4_plan import Draft, DraftStep  # noqa: E402
from agentcore.pipeline.s7_replan import Replan  # noqa: E402
from agentcore.tools.registry import steps_above_role  # noqa: E402
from domains.deterministic import DeterministicReport  # noqa: E402


def _restart_plan(llm) -> None:
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True))
    llm.queue(Draft, Draft(steps=[DraftStep(description="Restart it", tool_hint="restart_service")]))
    llm.queue(Replan, Replan(done=True, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary="done", resolved=True))


def _run_as(role: str, llm):
    _restart_plan(llm)
    actor = Actor(id=f"t-{role}", role=role, scope="public")
    config = {"configurable": {"thread_id": f"gate-{role}"}}
    request = Request(id="r1", raw_text="restart the payment service", actor=actor)
    app = build_app()
    with bound(actor, "r1"):
        return app.invoke({"request": request, "actor": actor}, config), config, app


@pytest.mark.parametrize("role", ["user", "engineer"])
def test_a_role_that_cannot_run_the_plan_is_refused_without_an_approval_prompt(llm, store, domain, role):
    result, _, _ = _run_as(role, llm)

    assert "__interrupt__" not in result, "a person was asked to approve something the role cannot run"
    events = [e["event"] for e in result["audit"]]
    assert "role_denied" in events
    assert "s6_act" not in [e["stage"] for e in result["audit"]]
    assert result["answer"].refused
    assert result["answer"].refusal_reason == "role not authorised"
    assert result["answer"].summary.startswith("Not authorised.")
    assert "restart_service (high risk)" in result["answer"].summary
    assert f"'{role}'" in result["answer"].summary


def test_an_admin_is_still_asked_to_approve(llm, store, domain):
    result, _, _ = _run_as("admin", llm)

    assert "__interrupt__" in result
    assert "role_denied" not in [e["event"] for e in result["audit"]]


def test_after_approval_the_executor_is_given_the_real_tool_not_a_denial(llm, store, domain, deep_agent):
    result, config, app = _run_as("admin", llm)
    resumed = app.invoke(Command(resume={"decision": "approve"}), config)

    assert "approved" in [e["event"] for e in resumed["audit"]]
    offered = {t.name: t for t in deep_agent.last["tools"]}
    assert not (offered["restart_service"].metadata or {}).get("denied_for_role")


def test_a_plan_that_needs_no_approval_is_not_blocked_at_the_gate(llm, store, domain, deep_agent):
    """A low risk plan for the lowest role runs; only a plan that would PAUSE is refused early."""
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True))
    llm.queue(Draft, Draft(steps=[DraftStep(description="Check", tool_hint="check_health")]))
    llm.queue(Replan, Replan(done=True, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary="ok", resolved=True))
    actor = Actor(id="t-user", role="user", scope="public")
    request = Request(id="r1", raw_text="is search healthy?", actor=actor)
    with bound(actor, "r1"):
        result = build_app().invoke({"request": request, "actor": actor},
                                    {"configurable": {"thread_id": "gate-low"}})

    assert "role_denied" not in [e["event"] for e in result["audit"]]
    assert "s6_act" in [e["stage"] for e in result["audit"]]
    assert not result["answer"].refused


# ---- the pure rule ---------------------------------------------------------


def _plan(*hints):
    return Plan(steps=[PlanStep(id=str(i), description="x", tool_hint=h) for i, h in enumerate(hints)])


def test_steps_above_a_role_are_listed_with_their_risk():
    plan = _plan("lookup", "calculate", "record", None)

    # A user loses the WRITE step and keeps the calculation: see
    # test_a_user_can_use_everything_except_the_tools_that_write.
    assert [(s.tool_hint, r) for s, r in steps_above_role(plan, FLOORS, "user")] == [
        ("record", "high")]
    assert steps_above_role(plan, FLOORS, "admin") == []


def test_the_gate_and_the_executor_agree_on_what_a_role_may_run():
    """One rule, two places: a step the gate lets through must not be denied at execution."""
    plan = _plan("lookup", "calculate", "record")
    for role in ("user", "engineer", "admin", "anonymous"):
        gate_says_no = {s.tool_hint for s, _ in steps_above_role(plan, FLOORS, role)}
        executor_denies = {t.name for t in restrict_by_role(TOOLS, FLOORS, role)
                           if t.metadata and t.metadata.get("denied_for_role")}
        assert gate_says_no == executor_denies, role


# ---- skipping only what the role may not run ------------------------------


def _mixed_plan(llm) -> None:
    llm.queue(InjectionVerdict, InjectionVerdict(is_injection=False))
    llm.queue(Sufficiency, Sufficiency(enough=True))
    llm.queue(Draft, Draft(steps=[
        DraftStep(description="Check", tool_hint="check_health"),
        DraftStep(description="Restart it", tool_hint="restart_service"),
    ]))
    llm.queue(Replan, Replan(done=True, remaining_steps=[]))
    llm.queue(DeterministicReport, DeterministicReport(summary="checked", resolved=True))


def _run_mixed(role: str, llm):
    _mixed_plan(llm)
    actor = Actor(id=f"t-{role}", role=role, scope="public")
    request = Request(id="r1", raw_text="check and restart search", actor=actor)
    app = build_app()
    with bound(actor, "r1"):
        return app.invoke({"request": request, "actor": actor},
                          {"configurable": {"thread_id": f"mixed-{role}"}})


def test_a_user_keeps_the_allowed_steps_and_only_the_forbidden_one_is_skipped(llm, store, domain, deep_agent):
    result = _run_mixed("user", llm)

    assert "__interrupt__" not in result, "nothing left in the plan needs approval"
    events = [e["event"] for e in result["audit"]]
    assert "role_skipped" in events and "role_denied" not in events
    assert "s6_act" in [e["stage"] for e in result["audit"]]
    status = {s.tool_hint: s.status for s in result["plan"].steps}
    assert status["restart_service"] == "skipped"
    assert status["check_health"] != "skipped"
    assert {t.name for t in deep_agent.last["tools"]} >= {"check_health"}
    assert not result["answer"].refused
    assert "Skipped for your role ('user'): restart_service (high risk)" in result["answer"].summary


def test_an_admin_with_the_same_plan_skips_nothing_and_is_asked(llm, store, domain):
    result = _run_mixed("admin", llm)

    assert "__interrupt__" in result
    assert "role_skipped" not in [e["event"] for e in result["audit"]]


def test_the_skipped_step_is_recorded_as_a_failed_result(llm, store, domain, deep_agent):
    result = _run_mixed("user", llm)

    skipped = [r for r in result["past_steps"] if not r.ok and "skipped" in (r.error or "")]
    assert len(skipped) == 1 and "restart_service" in skipped[0].error


def test_skipped_steps_do_not_raise_the_level_the_gate_assesses(monkeypatch, llm, store, domain):
    """A plan whose only restricted step is skipped needs no approval, and the gate says so.

    The skipped step is HIGH now rather than medium, because medium is inside a
    user's ceiling. The property under test is unchanged: a step removed for
    this caller must not drag the plan's assessed level up with it, or every
    run would stop for an approval of something that is not going to happen.
    """
    floors = {**domain.action_risk(), "check_health": "high"}
    monkeypatch.setattr(type(domain), "action_risk", lambda self: floors)
    plan = Plan(steps=[PlanStep(id="1", description="x", tool_hint="check_health")]
                + [PlanStep(id="2", description="y", tool_hint="lookup_ticket")])
    actor = Actor(id="t", role="user", scope="public")
    with bound(actor, "r1"):
        out = s5_gate.run({"plan": plan})

    assert out["plan"].steps[0].status == "skipped"
    assert out["approved_plan_revision"] == out["plan"].revision
    assert [e for e in out["audit"] if e["event"] == "risk_assessed"][0]["level"] == "low"


def test_the_skip_note_names_the_role_and_the_tools_and_is_empty_without_events():
    from agentcore.pipeline.s9_guard_out import role_skip_note

    event = {"role": "user", "tools": ["restart_service"], "risks": ["high"]}
    assert role_skip_note([event]) == (
        "[Skipped for your role ('user'): restart_service (high risk). This part was not "
        "executed; the analysis above covers only the remaining steps.]")
    assert role_skip_note([]) == ""


# ------------------------------- the roles this system actually issues ------
#
# The table shipped with {"user", "admin"} only, so `engineer` and
# `procurement` fell to the lowest ceiling and lost calculate_tco and
# get_budget. agent_eval runs as admin, so every metric stayed clean while the
# console, the chat UI and the API demo accounts all quietly got worse.
# PROBLEMS P60. These tests exist because the eval cannot see this.


@pytest.mark.parametrize("role", ["engineer", "procurement"])
def test_the_roles_real_entry_points_use_can_still_calculate(role):
    """console, chat UI and the demo accounts must keep their medium tools.

    A vendor assessment that cannot compute a total is not an assessment. If
    this fails, the commercial reviewer is receiving a denial stand-in and its
    finding rests on nothing.
    """
    assert RISK_ORDER[role_ceiling(role)] >= RISK_ORDER["medium"]


@pytest.mark.parametrize("role", ["user", "engineer", "procurement"])
def test_no_role_below_admin_may_write(role):
    """The other half. Reads and calculations yes, writes no.

    record_assessment, submit_for_signoff and raise_exception are high on the
    risk floor precisely because they change something somebody acts on.
    """
    assert RISK_ORDER[role_ceiling(role)] < RISK_ORDER["high"]


def test_every_role_the_code_issues_is_in_the_table():
    """An unlisted role still fails closed, but silently losing a tool is not
    the failure anyone wants to discover during a demo. This catches a new
    Actor(role=...) added anywhere in src/ that nobody thought to list.
    """
    import re
    from pathlib import Path

    root = Path(__file__).resolve().parents[1] / "src"
    issued = {
        match
        for path in root.rglob("*.py")
        for match in re.findall(r'Actor\([^)]*role\s*=\s*"([a-z_]+)"', path.read_text(encoding="utf-8"))
    }
    # ANONYMOUS is deliberately absent: an unauthenticated caller should hold
    # the lowest ceiling, and listing it would invite someone to raise it.
    deliberately_unlisted = {"anonymous"}
    unlisted = sorted(issued - set(ROLE_MAX_RISK) - deliberately_unlisted)

    assert not unlisted, (
        f"src/ creates Actor(role=...) for {unlisted}, which ROLE_MAX_RISK does not list. "
        f"They fall to '{LOWEST_CEILING}' and lose every tool above it. Add them or change "
        f"the caller."
    )
