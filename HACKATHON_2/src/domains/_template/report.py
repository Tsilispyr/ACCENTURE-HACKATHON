"""TIME BUDGET: 15 minutes. The shape of a good answer.

This is the schema the final structured call must fill, so it is also a
specification of what a complete answer contains. Field descriptions are read
BY THE MODEL - write them as instructions, not as documentation.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class TemplateReport(BaseModel):
    """Rename this to something meaningful for the scenario."""

    answer: str = Field(description="The direct answer, in plain language.")

    # Ask for the things a grader will look for. If the brief lists required
    # output fields, they belong here verbatim - then they cannot be forgotten.
    findings: list[str] = Field(
        default_factory=list,
        description="Facts established, each naming the source it came from.",
    )
    next_steps: list[str] = Field(
        default_factory=list, description="Concrete recommended actions, most important first."
    )
    confidence: str = Field(
        default="medium", description="high | medium | low, based on how well the sources covered it."
    )
