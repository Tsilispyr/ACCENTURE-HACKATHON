"""LLM-as-judge: multi-criteria scoring, groundedness, and a position-bias control.

The position-bias control is the part almost nobody builds, and it is the
clearest signal that you know judges are themselves unreliable. Ask the same
model to compare A against B, then B against A. If the verdict flips, the judge
preferred a POSITION rather than an answer, and any number it produced about
that pair is noise.

Every structured call uses method="function_calling" - the form proven against
this Azure deployment.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from agentcore.llm import judge_model


class Judgment(BaseModel):
    passed: bool = Field(description="Does the answer satisfy the rubric?")
    reasoning: str = Field(description="One or two sentences. Name the deciding factor.")


class MultiCriteria(BaseModel):
    """Four axes, 1-5. Separating them stops one strong axis hiding three weak ones."""

    correctness: int = Field(ge=1, le=5, description="Is it factually right, per the sources?")
    relevance: int = Field(ge=1, le=5, description="Does it answer what was actually asked?")
    completeness: int = Field(ge=1, le=5, description="Are important parts missing?")
    grounding: int = Field(ge=1, le=5, description="Is every claim traceable to a source?")
    reasoning: str = ""

    @property
    def mean(self) -> float:
        return round(
            (self.correctness + self.relevance + self.completeness + self.grounding) / 4, 2
        )


class ClaimCheck(BaseModel):
    claim: str
    supported: bool
    evidence: str = Field(default="", description="The quote that supports it, or empty.")


class Groundedness(BaseModel):
    claims: list[ClaimCheck] = Field(default_factory=list)
    summary: str = ""

    @property
    def score(self) -> float:
        if not self.claims:
            return 1.0
        return round(sum(c.supported for c in self.claims) / len(self.claims), 2)

    @property
    def unsupported(self) -> list[str]:
        return [c.claim for c in self.claims if not c.supported]


class Preference(BaseModel):
    winner: str = Field(description="Exactly 'A' or 'B'.")
    reasoning: str = ""


def judge(question: str, answer: str, rubric: str) -> Judgment:
    model = judge_model().with_structured_output(Judgment, method="function_calling")
    return model.invoke(
        "You are grading an assistant's answer against a rubric. Be strict: a "
        "plausible answer that does not meet the rubric fails.\n\n"
        f"QUESTION:\n{question}\n\nRUBRIC:\n{rubric}\n\nANSWER:\n{answer}"
    )


def score_multi(question: str, answer: str, sources: str = "") -> MultiCriteria:
    model = judge_model().with_structured_output(MultiCriteria, method="function_calling")
    return model.invoke(
        f"Score this answer on four axes, 1-5.\n\nQUESTION:\n{question}\n\n"
        + (f"SOURCES:\n{sources}\n\n" if sources else "")
        + f"ANSWER:\n{answer}"
    )


def check_groundedness(answer: str, sources: str) -> Groundedness:
    """Split the answer into claims and check each against the sources.

    Per-claim rather than whole-answer: an answer that is 80% supported and 20%
    invented scores well as a whole and is dangerous in exactly the 20%.

    THE ABSENCE CASE is why this prompt is longer than it looks like it should
    be. An assessment is required to report what is MISSING - "no penetration
    test report was supplied", "data privacy could not be assessed". Those are
    the `basis="missing"` claims, and they are a feature (FR11), not sloppiness.

    Asked only "do the sources support this claim", a judge marks every one of
    them unsupported, because nothing in the sources says a thing is absent.
    That failed six of twelve eval cases for doing exactly what the design asks
    - an evaluator punishing the behaviour it exists to encourage, which is
    worse than no evaluator, because the obvious way to raise the score is to
    stop reporting gaps.

    So a claim of absence is checked the other way round: it is supported when
    the sources indeed do not contain the thing.
    """
    model = judge_model().with_structured_output(Groundedness, method="function_calling")
    return model.invoke(
        "Break the ANSWER into individual factual claims and decide whether the "
        "SOURCES support each one.\n\n"
        "A claim that ASSERTS something is supported only when the sources say "
        "it. Quote the supporting text. No quote means not supported.\n\n"
        "A claim that reports an ABSENCE - that something is missing, was not "
        "supplied, could not be assessed, or is not covered by the sources - is "
        "verified the other way round. It is SUPPORTED when the sources indeed do "
        "not contain that thing, and unsupported only when the sources plainly do "
        "contain it. Do not mark a claim of absence unsupported merely because no "
        "quote proves the absence; nothing ever could.\n\n"
        "Ignore remarks about the assessment's own scope or coverage. Those "
        "describe the report, not the subject, and are not factual claims about "
        "it.\n\n"
        f"SOURCES:\n{sources}\n\nANSWER:\n{answer}"
    )


def compare(question: str, answer_a: str, answer_b: str) -> Preference:
    model = judge_model().with_structured_output(Preference, method="function_calling")
    return model.invoke(
        f"Which answer is better?\n\nQUESTION:\n{question}\n\n"
        f"ANSWER A:\n{answer_a}\n\nANSWER B:\n{answer_b}\n\n"
        "Reply with exactly 'A' or 'B'."
    )


def compare_both_ways(question: str, first: str, second: str) -> dict:
    """Run the comparison twice with the order swapped.

    Returns consistent=False when the judge flipped, which means it preferred a
    position rather than an answer. Costs one extra call and is worth it.
    """
    forward = compare(question, first, second)
    backward = compare(question, second, first)

    # Map each verdict back to the actual answer it chose.
    forward_choice = "first" if forward.winner.strip().upper() == "A" else "second"
    backward_choice = "second" if backward.winner.strip().upper() == "A" else "first"

    return {
        "consistent": forward_choice == backward_choice,
        "winner": forward_choice if forward_choice == backward_choice else None,
        "forward": forward.winner,
        "backward": backward.winner,
        "reasoning": forward.reasoning,
    }
