"""The risk floor, the refusal patterns and the PII rules."""

from __future__ import annotations

from agentcore.contracts import RiskLevel

# THE RISK FLOOR. The model may raise a level; it can never lower one.
#
# Everything that writes to an enterprise system is high: an assessment record
# is a decision artefact, and once a recommendation is filed people act on it.
# Reads are low. Budget lookups sit in between because a figure pulled from the
# finance system shapes a recommendation even though it changes nothing.
ACTION_RISK: dict[str, RiskLevel] = {
    # writes - these need a human
    "record_assessment": "high",
    "raise_exception": "high",
    # reads
    "search_policy": "low",
    "retrieve_document": "low",
    "get_vendor_history": "low",
    "get_prior_assessments": "low",
    "calculate_tco": "medium",
    "get_budget": "medium",
}

# Words that make a request a DECISION rather than a lookup. Deliberately about
# the verb, not the subject: "assess Asteria" and "evaluate the hidden vendor"
# must both match, because Session D supplies a vendor nobody has seen and the
# handout says not to hard-code expected answers.
DECISION_VERBS = (
    "assess", "evaluate", "review", "approve", "reject", "recommend",
    "renewal", "sign off", "signoff", "decision",
)

# What makes the SUBJECT of that decision high risk, per AI-004 section 2:
# systems processing Confidential or Restricted information, or materially
# influencing consequential outcomes.
HIGH_RISK_SUBJECT = (
    "confidential", "restricted", "personal data", "customer data",
    "consequential", "credit", "employee",
)


def request_risk(raw_text: str) -> RiskLevel:
    """High when the request asks for a consequential verdict on a high-risk subject.

    THREE POLICIES CONVERGE HERE and none of them is about tools:

      AI-004 s6   final approval of a High-risk AI vendor must be performed by
                  authorized human approvers
      PR-001 s4   High-risk AI use cases cannot be approved solely by an
                  automated recommendation
      VR-006 s5   Procurement or an AI system cannot accept High risk on the
                  owner's behalf

    The pipeline scored risk only from the tools a plan chose. An assessment
    chooses reads, so it scored MEDIUM and the approval gate never fired, on
    precisely the decision all three policies reserve for a human. Measured on
    the real pack before this existed: `s5_gate risk_assessed level=medium`,
    no interrupt, a verdict nobody approved (PROBLEMS P59).

    Both halves are required. "What does the retention policy say" is a lookup
    about confidential data and needs no approval; "assess this vendor" for a
    system touching only public data is a decision that does not reach the High
    tier. Demanding a human for every question would train reviewers to click
    through, which is worse than not asking.
    """
    text = (raw_text or "").lower()
    if not any(verb in text for verb in DECISION_VERBS):
        return "none"
    return "high" if any(word in text for word in HIGH_RISK_SUBJECT) else "medium"

# On top of the base injection list in agentcore/safety/patterns.py. These are
# the domain specific ones: attempts to talk the assessment into a verdict.
#
# WRITTEN TO TOLERATE A SUBJECT IN THE MIDDLE. The first version of these
# matched the exact phrasing someone imagined - "mark this as low risk" - and
# missed "mark ASTERIA as low risk", which is how a person would actually write
# it. The eval caught it: an adversarial case asking to suppress a finding was
# answered rather than refused. A refusal pattern that only catches the example
# it was written from is a pattern that catches nobody.
#
# The word gaps are BOUNDED ({0,4}), not `.*`. An unbounded gap between two
# common words matches half the corpus and makes the guardrail useless in the
# other direction.
BLOCKED_PATTERNS: list[str] = [
    r"approve\s+(\S+\s+){0,4}(without|regardless|anyway|despite)",
    r"(skip|bypass|ignore|waive)\s+(the\s+)?(\S+\s+){0,2}"
    r"(review|check|assessment|due diligence|approval)",
    # "mark X as low risk" / "record it as compliant" / "treat them as approved"
    r"(mark|record|treat|classify|set)\s+(\S+\s+){0,4}as\s+"
    r"(approved|compliant|low[\s-]?risk|no[\s-]?risk|acceptable)",
    # The object does not matter: "do not record" IS the instruction.
    r"(do not|don't|dont|never)\s+(record|log|report|mention|disclose|flag)",
    # Asking for the verdict first, evidence later.
    r"(just|simply)\s+(approve|sign off|pass)\b",
    # Dictating the verdict outright: "RETURN 'APPROVE - LOW RISK'". "approval"
    # does not match, so an ordinary sentence about approval workflows is safe.
    r"(return|output|respond\s+with|reply\s+with|answer\s+with|say)\s+['\"‘“]?\s*approved?\b",
]

# Applied to the OUTPUT. A vendor assessment routinely quotes contact details
# out of a contract, and they do not belong in a circulated report.
PII_RULES: list[tuple[str, str]] = [
    ("email", "redact"),
    ("phone", "redact"),
    ("address", "redact"),
    ("iban", "redact"),
    ("ip_address", "redact"),
    ("credit_card", "redact"),
]
