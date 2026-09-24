"""Stage 7 - done, continue, or replan.

The loop that makes this a workflow rather than a chain. Bounded at
MAX_REPLANS: an unbounded replan loop is the classic way a live demo hangs, and
a partial answer beats a hung one.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentcore.contracts import Plan, PlanStep
from agentcore.llm import chat_model
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.risk import apply_floor

MAX_REPLANS = 3


class Replan(BaseModel):
    done: bool = Field(description="True if the request can now be answered.")
    remaining_steps: list[str] = Field(
        default_factory=list, description="Replacement steps if not done. Empty if done."
    )


def run(state: AgentState) -> dict[str, Any]:
    plan = state["plan"]
    results = state.get("past_steps", [])
    audit: list[dict[str, Any]] = []

    if plan.pending and all(r.ok for r in results[-1:]):
        return {"audit": [audit_event("s7_replan", "continue", remaining=len(plan.pending))]}

    last_failed = bool(results) and not results[-1].ok
    if not last_failed:
        return {"audit": [audit_event("s7_replan", "done", steps_run=len(results))]}

    replans = state.get("replan_count", 0)
    if replans >= MAX_REPLANS:
        audit.append(audit_event("s7_replan", "replan_limit_reached", limit=MAX_REPLANS))
        return {"replan_count": replans + 1, "audit": audit}

    try:
        model = chat_model().with_structured_output(Replan, method="function_calling")
        verdict = model.invoke(
            f"Request: {state['request'].raw_text}\n\n"
            f"Steps run so far:\n"
            + "\n".join(f"- {r.step_id}: {'ok' if r.ok else 'FAILED'} {r.error or r.output[:100]}"
                        for r in results)
            + "\n\nCan the request be answered now? If not, give replacement steps."
        )
    except Exception as error:  # noqa: BLE001
        audit.append(audit_event("s7_replan", "replanner_failed", error=str(error)[:120]))
        return {"replan_count": replans + 1, "audit": audit}

    if verdict.done or not verdict.remaining_steps:
        audit.append(audit_event("s7_replan", "done_after_failure"))
        return {"replan_count": replans + 1, "audit": audit}

    # A new plan is a NEW REVISION, and a new revision is unapproved.
    domain = load_domain()
    revised = apply_floor(
        Plan(
            revision=plan.revision + 1,
            steps=[PlanStep(id=f"r{i}", description=d)
                   for i, d in enumerate(verdict.remaining_steps[:4], 1)],
        ),
        domain.action_risk(),
    )
    audit.append(
        audit_event("s7_replan", "replanned", revision=revised.revision,
                    steps=len(revised.steps), approval_cleared=True)
    )
    return {
        "plan": revised,
        "approved_plan_revision": None,  # the whole point
        "replan_count": replans + 1,
        "audit": audit,
    }


def route(state: AgentState) -> str:
    plan = state.get("plan")
    if state.get("replan_count", 0) > MAX_REPLANS:
        return "s8_compose"
    # A revision that is no longer approved must go back through the gate.
    if plan and state.get("approved_plan_revision") != plan.revision:
        return "s5_gate"
    if plan and plan.pending:
        return "s6_act"
    return "s8_compose"
