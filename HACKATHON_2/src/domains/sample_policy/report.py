"""The shape of an answer in this domain."""

from __future__ import annotations

from pydantic import BaseModel, Field


class PolicyAnswer(BaseModel):
    """A grounded answer to a regulatory question."""

    answer: str = Field(description="The direct answer, in plain language.")
    obligations: list[str] = Field(
        default_factory=list,
        description="Concrete duties this imposes, each naming who carries it.",
    )
    deadlines: list[str] = Field(
        default_factory=list, description="Any time limits, with the article they come from."
    )
    articles_relied_on: list[str] = Field(
        default_factory=list, description="e.g. ['Article 33(1)', 'Article 34']"
    )
    caveat: str = Field(
        default="This is the regulation's text, not legal advice.",
        description="Always present.",
    )
