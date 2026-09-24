"""The state every stage reads and writes.

Accumulating fields use operator.add so parallel or repeated stages append
rather than overwrite - evidence gathered across retrieval retries, and step
results across a replan loop, both need that.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from agentcore.contracts import Answer, Evidence, Plan, Request, StepResult


class AgentState(TypedDict, total=False):
    request: Request
    evidence: Annotated[list[Evidence], operator.add]
    plan: Plan | None
    past_steps: Annotated[list[StepResult], operator.add]

    # Approval binds to a plan REVISION, not to the request. Replanning clears
    # it, so approving one plan never authorises a later one.
    approved_plan_revision: int | None
    rejected: bool
    # Conditions attached to a conditional approval, carried into the report.
    approval_conditions: list[str]

    refusal: str | None
    answer: Answer | None
    replan_count: int
    retrieval_attempts: int

    # Everything security-relevant that happened, in order. Surfaced in the API
    # response and asserted on by the trajectory eval.
    audit: Annotated[list[dict[str, Any]], operator.add]


def audit_event(stage: str, event: str, **detail: Any) -> dict[str, Any]:
    return {"stage": stage, "event": event, **detail}
