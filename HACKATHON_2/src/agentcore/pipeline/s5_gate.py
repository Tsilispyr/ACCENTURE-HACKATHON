"""Stage 5 - the risk gate and the human-in-the-loop interrupt.

The authoritative approval point. Everything downstream assumes that whatever
s6_act is allowed to do has already been approved here, which is what makes an
interrupt escaping from INSIDE the executor meaningful (see s6_act).

Approval binds to plan.revision. A replan bumps the revision and clears the
approval, so approving one plan never authorises a later one.
"""

from __future__ import annotations

from typing import Any

from langgraph.types import interrupt

from agentcore.contracts import RISK_ORDER, StepResult
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.risk import apply_floor, needs_approval
from agentcore.tools.registry import role_ceiling, steps_above_role
from agentcore.world import current_actor


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    plan = state["plan"]

    # Re-stamp through the floor. The plan may have come from a replan, and a
    # floor applied once is not a floor.
    floors = domain.action_risk()
    plan = apply_floor(plan, floors)

    # Skip what the requester's ROLE may not run, and only that. The rest of the
    # plan goes ahead. A step above the role would be swapped for a denial by the
    # executor anyway (tools/registry.py), so leaving it in would mean a person
    # is asked to approve something that can never happen - a real click that
    # changes nothing, and one a reader of the trace could mistake for
    # permission that was never grantable.
    role = current_actor().role
    blocked = steps_above_role(plan, floors, role)
    skipped: list[StepResult] = []
    if blocked:
        blocked_ids = {s.id for s, _ in blocked}
        detail = {
            "role": role, "ceiling": role_ceiling(role),
            "steps": [s.id for s, _ in blocked],
            "tools": [s.tool_hint for s, _ in blocked],
            "risks": [r for _, r in blocked],
        }
        if all(s.id in blocked_ids or s.status == "skipped" for s in plan.steps):
            # Nothing this role may run is left: refuse rather than pass an empty
            # plan on to look like a success.
            return {
                "plan": plan, "rejected": True,
                "audit": [audit_event("s5_gate", "role_denied", **detail)],
            }
        for step, risk in blocked:
            step.status = "skipped"
            skipped.append(StepResult(
                step_id=step.id, ok=False, skipped=True, owner=step.owner or "",
                error=f"skipped: role '{role}' may not use {step.tool_hint} ({risk} risk)",
            ))

    # What will actually run. Skipped steps do not count toward the level, so a
    # plan whose only high risk step was skipped does not ask for approval.
    runnable = [s for s in plan.steps if s.status != "skipped"]
    level = max((s.risk for s in runnable), key=lambda r: RISK_ORDER[r], default="none")
    audit = [audit_event("s5_gate", "risk_assessed", level=level, revision=plan.revision)]
    if blocked:
        audit.append(audit_event("s5_gate", "role_skipped", **detail))

    if not needs_approval(level):
        return {"plan": plan, "approved_plan_revision": plan.revision,
                "past_steps": skipped, "audit": audit}

    if state.get("approved_plan_revision") == plan.revision:
        audit.append(audit_event("s5_gate", "already_approved", revision=plan.revision))
        return {"plan": plan, "past_steps": skipped, "audit": audit}

    # Pause. LangGraph persists state here; the API resumes with Command(resume=...).
    decision = interrupt(
        {
            "reason": f"Plan revision {plan.revision} contains {level}-risk steps.",
            "revision": plan.revision,
            "steps": [
                # `owner` is here because a reviewer approving a step must be
                # shown WHO executes it. Without it, adding delegation would
                # quietly move work to a specialist the approver never saw.
                {"id": s.id, "description": s.description, "tool": s.tool_hint,
                 "risk": s.risk, "owner": s.owner}
                for s in runnable
            ],
            # What the reviewer is NOT being asked about, so they can see it was
            # left out rather than assume it was forgotten.
            "skipped": [{"id": s.id, "tool": s.tool_hint} for s, _ in blocked],
            # Three-way, not two. Binary approve/reject forces a reviewer to
            # either accept unmitigated risk or block everything, which is not
            # how any real approval works - most sign-offs are "yes, provided".
            "allowed_decisions": ["approve", "approve_with_conditions", "reject"],
        }
    )

    verdict, conditions = _read_decision(decision)
    audit.append(
        audit_event("s5_gate", verdict, revision=plan.revision,
                    conditions=len(conditions))
    )
    if verdict == "rejected":
        return {"plan": plan, "rejected": True, "past_steps": skipped, "audit": audit}

    # A conditional approval still authorises the work. The conditions are
    # carried into the answer so they appear in the report rather than being
    # lost the moment the reviewer clicks the button.
    return {
        "plan": plan,
        "approved_plan_revision": plan.revision,
        "approval_conditions": conditions,
        "past_steps": skipped,
        "audit": audit,
    }


APPROVE_WORDS = {"approve", "approved", "yes", "y"}
CONDITIONAL_WORDS = {"approve_with_conditions", "conditional", "approve with conditions"}


def _read_decision(decision: Any) -> tuple[str, list[str]]:
    """Return ("approved" | "approved_with_conditions" | "rejected", conditions).

    Fails CLOSED: anything not recognisably an approval is a rejection. A
    conditional approval with NO conditions attached is treated as a plain
    approval, because "conditional on nothing" is just approval with extra
    words - and silently inventing a condition would be worse.
    """
    conditions: list[str] = []

    if isinstance(decision, bool):
        return ("approved" if decision else "rejected", conditions)

    if isinstance(decision, dict):
        raw = decision.get("decision") or decision.get("type") or decision.get("action") or ""
        supplied = decision.get("conditions") or []
        conditions = [str(c).strip() for c in supplied if str(c).strip()]
    else:
        raw = decision

    word = str(raw).strip().lower()
    if word in CONDITIONAL_WORDS:
        return ("approved_with_conditions" if conditions else "approved", conditions)
    if word in APPROVE_WORDS:
        return ("approved", conditions)
    return ("rejected", [])


def _is_approval(decision: Any) -> bool:
    """Kept for the safety tests: is this ANY kind of approval?"""
    return _read_decision(decision)[0] != "rejected"


def route(state: AgentState) -> str:
    if state.get("rejected"):
        return "s8_compose"
    return "s6_act"
