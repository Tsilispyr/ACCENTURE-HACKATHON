"""TIME BUDGET: 15 minutes. The risk floor and the refusal rules.

Small file, disproportionate importance: ACTION_RISK is what the model cannot
argue its way past.
"""

from __future__ import annotations

from agentcore.contracts import RiskLevel

# THE RISK FLOOR. Tool name -> the lowest risk it can ever be treated as.
#
# The model may RAISE a level. It can never lower one. Anything missing here
# defaults to "low", so every mutating operation must appear - and
# test_domain_contract fails the build if one does not.
#
# Rule of thumb:
#   high    changes, deletes or moves something; interrupts work in flight
#   medium  changes configuration, but reversibly
#   low     reads
ACTION_RISK: dict[str, RiskLevel] = {
    # "restart_service": "high",
    # "scale_something": "medium",
    # "lookup_record": "low",
}

# Refusal patterns ON TOP of the base list in agentcore/safety/patterns.py.
# Only add domain-specific ones - generic prompt injection is already covered.
BLOCKED_PATTERNS: list[str] = [
    # r"delete (all|every) (record|customer|account)",
]

# (pii_type, "redact" | "block"). Applied to the OUTPUT.
PII_RULES: list[tuple[str, str]] = [
    ("email", "redact"),
    # ("credit_card", "block"),
]
