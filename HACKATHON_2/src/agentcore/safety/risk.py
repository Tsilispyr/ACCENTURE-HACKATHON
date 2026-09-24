"""The risk floor the model cannot lower.

The single most important safety property in this system, and the cheapest.

A request's text is attacker-controllable in any real deployment, and so is
anything retrieved from a corpus or returned by a tool. So a gate the model can
ARGUE WITH is decorative. The model may raise a risk level - it knows things
the table does not, like "this restart targets the primary" - but it can never
lower one.

If the model's risk call fails entirely, `assessed` is None and the floor
stands. Failure makes the system more cautious, never less.
"""

from __future__ import annotations

from agentcore.contracts import RISK_ORDER, Plan, RiskLevel

# Anything a domain does not classify is "low". A mutating tool missing from
# the domain's action_risk() table is therefore a real gap, which is why
# test_domain_contract asserts every tool name appears in it.
DEFAULT_RISK: RiskLevel = "low"

# Which levels require a human before execution.
APPROVAL_REQUIRED = frozenset({"high"})


def risk_of(action: str, floors: dict[str, RiskLevel]) -> RiskLevel:
    """The table's verdict for one action, ignoring any model opinion."""
    return floors.get(action, DEFAULT_RISK)


def effective_risk(
    action: str,
    floors: dict[str, RiskLevel],
    assessed: str | None = None,
) -> RiskLevel:
    """max(floor, model assessment). Never less than the floor."""
    floor = risk_of(action, floors)
    if assessed is None:
        return floor
    key = str(assessed).strip().lower()
    if key not in RISK_ORDER:  # an unparseable answer is not an answer
        return floor
    return key if RISK_ORDER[key] > RISK_ORDER[floor] else floor  # type: ignore[return-value]


def needs_approval(level: RiskLevel) -> bool:
    return level in APPROVAL_REQUIRED


def apply_floor(
    plan: Plan,
    floors: dict[str, RiskLevel],
    baseline: RiskLevel = "none",
) -> Plan:
    """Re-stamp every step's risk through the floor.

    Run this on any plan that came from a model, including a replan. A step
    whose tool_hint is unknown keeps whatever the planner said, bounded below
    by DEFAULT_RISK.

    `baseline` is the risk of the REQUEST, independent of which tools answer
    it, and it exists because tool-derived risk alone misses the case that
    matters most. An assessment that reaches a consequential verdict reads
    policies and computes totals - all low and medium tools - so `max_risk`
    came out medium and no human was ever asked, on precisely the decision a
    human is required to make. Risk that lives in the QUESTION cannot be
    recovered from the answer's tool list. The domain supplies it via
    `request_risk()`; see PROBLEMS P59.
    """
    for step in plan.steps:
        step.risk = effective_risk(step.tool_hint or "", floors, step.risk)
    if plan.steps and RISK_ORDER.get(baseline, 0) > 0:
        # Raise the LAST step rather than every step. The baseline is a
        # property of the request as a whole, and stamping it onto each step
        # would tell the executor that reading a policy is a consequential
        # act, which would be false and would widen nothing but the audit.
        last = plan.steps[-1]
        if RISK_ORDER.get(baseline, 0) > RISK_ORDER.get(last.risk, 0):
            last.risk = baseline
    return plan
