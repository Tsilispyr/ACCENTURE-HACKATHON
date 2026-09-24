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

from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.risk import apply_floor, needs_approval


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    plan = state["plan"]

    # Re-stamp through the floor. The plan may have come from a replan, and a
    # floor applied once is not a floor.
    plan = apply_floor(plan, domain.action_risk())
    level = plan.max_risk
    audit = [audit_event("s5_gate", "risk_assessed", level=level, revision=plan.revision)]

    if not needs_approval(level):
        return {"plan": plan, "approved_plan_revision": plan.revision, "audit": audit}

    if state.get("approved_plan_revision") == plan.revision:
        audit.append(audit_event("s5_gate", "already_approved", revision=plan.revision))
        return {"plan": plan, "audit": audit}

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
                for s in plan.steps
            ],
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
        return {"plan": plan, "rejected": True, "audit": audit}

    # A conditional approval still authorises the work. The conditions are
    # carried into the answer so they appear in the report rather than being
    # lost the moment the reviewer clicks the button.
    return {
        "plan": plan,
        "approved_plan_revision": plan.revision,
        "approval_conditions": conditions,
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
