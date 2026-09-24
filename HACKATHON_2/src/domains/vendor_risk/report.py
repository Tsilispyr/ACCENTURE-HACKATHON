"""The shape of a vendor assessment.

Field descriptions are read BY THE MODEL, so they are written as instructions.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class VendorAssessment(BaseModel):
    """What a procurement committee needs in order to sign or not sign."""

    vendor: str = Field(description="The vendor's legal name as it appears in the record.")
    overall_risk: str = Field(description="none, low, medium or high. The highest domain wins.")

    recommendation: str = Field(
        description="approve, approve_with_conditions or reject."
    )
    rationale: str = Field(
        description="Two or three sentences. Name the finding that drove the recommendation."
    )

    conditions: list[str] = Field(
        default_factory=list,
        description=(
            "Required if recommending conditions. Each must be TESTABLE: a date, a "
            "document, a threshold. 'Improve security' is not a condition."
        ),
    )

    evidence_gaps: list[str] = Field(
        default_factory=list,
        description=(
            "What could not be verified from the supplied corpus. Name the specific "
            "artefact that is missing, not the topic."
        ),
    )
    contradictions: list[str] = Field(
        default_factory=list,
        description="Where two sources disagree, stated as both sides, not resolved.",
    )

    escalation_required: bool = Field(
        default=False,
        description="True if the decision exceeds the assessor's authority, e.g. over budget threshold.",
    )
