"""The assessment specific metrics, beyond retrieval and groundedness.

The brief names ten things to measure. Four were already covered elsewhere --
retrieval relevance by `retrieval_eval`, groundedness by `judge`, guardrail
compliance and injection resistance by `trajectory` - and latency by the stage
timings in the ledger. These are the rest:

    citation correctness   does the cited source actually say it?
    task completion        were all the required risk domains covered?
    tool correctness       were the right tools chosen for the job?
    agent delegation       was specialist work actually delegated?
    decision quality       does the verdict follow from the findings?

THE DIVIDING LINE that matters here: four of the five are DETERMINISTIC and one
needs a model. Deterministic checks cannot flatter you and cost nothing, so
anything that can be one is one. Only `decision quality` genuinely requires
judgement, because "does this conclusion follow" is not a string comparison.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from agentcore.contracts import Answer, match_citation

NL = chr(10)


# Citation support thresholds. The floor catches a citation whose source says
# nothing of the kind; the margin catches one that names the wrong passage of a
# document that WAS retrieved. Calibrated on the vendor_risk corpus - see
# PROBLEMS.md P36 for the run that produced them.
ABSOLUTE_FLOOR = 0.25
RELATIVE_MARGIN = 0.75


@dataclass
class MetricResult:
    name: str
    score: float           # 0.0 to 1.0
    passed: bool
    detail: str = ""
    evidence: dict[str, Any] = field(default_factory=dict)


# ------------------------------------------------- citation correctness -----


def citation_correctness(answer: Answer, evidence_texts: dict[str, str]) -> MetricResult:
    """Does each cited source actually contain what the claim says it does?

    Deterministic: overlap between the claim's words and the words of the source
    it names, **weighted by how rare each word is across the retrieved set**.

    The weighting is the part that makes this work. Plain overlap scored a claim
    about security certificates as supported by a section on data training,
    because both are policy prose and both contain "supplier" and "shall". A
    word that appears in every chunk discriminates nothing, so it is worth
    nothing here. What is left is the vocabulary specific to the passage - and
    a claim that genuinely came from a passage shares it.

    This cannot judge whether a citation is APT, only whether the cited document
    says anything of the kind. That is the cheap version of the right question,
    and it catches the failure that matters: a citation pointing at a document
    that does not support it. A fabricated citation is the most damaging error
    an assessment can make, because it is the one a reader is least likely to
    check. The expensive per claim version lives in `evaluation/judge.py`.
    """
    import math
    import re

    cited = [c for c in answer.claims if c.basis == "evidence" and c.citations]
    if not cited:
        return MetricResult("citation_correctness", 1.0, True, "no evidence claims to check")

    def words(text: str) -> set[str]:
        return {w for w in re.findall(r"[a-z]{5,}", text.lower())}

    # Inverse document frequency over the retrieved set. With one chunk every
    # weight collapses to the same value, which is the honest outcome: there is
    # nothing to discriminate against.
    corpus = {key: words(text) for key, text in evidence_texts.items()}
    total = max(len(corpus), 1)
    frequency: dict[str, int] = {}
    for bag in corpus.values():
        for word in bag:
            frequency[word] = frequency.get(word, 0) + 1

    def weight(word: str) -> float:
        # +1 smoothing, so a word in every chunk still weighs slightly above 0
        # rather than making a whole claim unscoreable.
        return math.log(1 + total / (1 + frequency.get(word, 0)))

    def overlap(claim_words: set[str], key: str, mass: float) -> float:
        return sum(weight(w) for w in claim_words & corpus[key]) / mass

    good, bad = 0, []
    for claim in cited:
        claim_words = words(claim.statement)
        mass = sum(weight(w) for w in claim_words) or 1.0

        # Two stages, the same shape as the retrieval distance gate: an
        # absolute floor, then a relative margin. The floor alone is not
        # enough - on a small corpus of similar prose, a claim about security
        # certificates clears it against a section on data training purely on
        # shared words like "supplier" and "shall". The margin asks the sharper
        # question: of everything retrieved, is the CITED passage the one that
        # best supports this claim? A citation beaten decisively by a passage
        # it did not name is pointing at the wrong place.
        best = max((overlap(claim_words, key, mass) for key in corpus), default=0.0)

        supported = False
        for citation in claim.citations:
            key = match_citation(citation, evidence_texts)
            if key is None:
                continue          # names nothing that was retrieved: fabricated
            score = overlap(claim_words, key, mass)
            if score >= ABSOLUTE_FLOOR and score >= RELATIVE_MARGIN * best:
                supported = True
                break
        good += supported
        if not supported:
            bad.append(f"{claim.statement[:60]} -> {claim.citations[:1]}")

    score = good / len(cited)
    return MetricResult(
        "citation_correctness", round(score, 3), score >= 0.7,
        f"{good}/{len(cited)} citations support their claim",
        {"unsupported": bad[:3]},
    )



# ----------------------------------------------------- task completion ------


def task_completion(answer: Answer, required_domains: list[str]) -> MetricResult:
    """Were all the required risk domains actually assessed?

    `assessed=False` counts as NOT covered. A finding that says "no evidence
    found" is a real result and still leaves the domain unassessed, which is
    exactly the distinction a coverage metric should preserve.
    """
    if not required_domains:
        return MetricResult("task_completion", 1.0, True, "no domains required")

    done = set(answer.assessed_domains)
    missing = [d for d in required_domains if d not in done]
    score = len(done & set(required_domains)) / len(required_domains)
    return MetricResult(
        "task_completion", round(score, 3), not missing,
        f"{len(done & set(required_domains))}/{len(required_domains)} domains assessed",
        {"missing": missing},
    )


# ------------------------------------------------------ tool correctness ----


def tool_correctness(audit: list[dict], expected_tools: list[str],
                     forbidden_tools: list[str] | None = None) -> MetricResult:
    """Were the right tools used, and no wrong ones?

    Reads the audit trail rather than asking a model what it did. An agent's
    account of its own tool use is exactly the thing least worth trusting.
    """
    used: set[str] = set()
    for event in audit:
        for name in event.get("tool_calls", []) or []:
            if name:
                used.add(name)

    forbidden_used = [t for t in (forbidden_tools or []) if t in used]
    if not expected_tools:
        passed = not forbidden_used
        return MetricResult("tool_correctness", 1.0 if passed else 0.0, passed,
                            f"used {sorted(used) or 'none'}",
                            {"forbidden_used": forbidden_used})

    hit = [t for t in expected_tools if t in used]
    score = len(hit) / len(expected_tools)
    if forbidden_used:
        score = 0.0
    return MetricResult(
        "tool_correctness", round(score, 3), score >= 0.5 and not forbidden_used,
        f"{len(hit)}/{len(expected_tools)} expected tools used",
        {"used": sorted(used), "missing": [t for t in expected_tools if t not in used],
         "forbidden_used": forbidden_used},
    )


# ------------------------------------------------------ agent delegation ----


def agent_delegation(audit: list[dict], *, expected: bool) -> MetricResult:
    """Was work handed to a specialist when it should have been?

    Delegation shows up as a `task` tool call, which is how a deep agent
    dispatches to a subagent. Measuring BOTH directions matters: delegating
    when it is not warranted costs a round trip for nothing, and that is a real
    failure mode rather than harmless enthusiasm.
    """
    delegated = sum(e.get("delegated", 0) for e in audit)
    available = any(e.get("event") == "specialists_available" for e in audit)

    if not available:
        return MetricResult("agent_delegation", 1.0, True, "no specialists were offered")

    if expected:
        return MetricResult(
            "agent_delegation", 1.0 if delegated else 0.0, bool(delegated),
            f"{delegated} delegation(s), expected at least one",
        )
    return MetricResult(
        "agent_delegation", 1.0 if not delegated else 0.5, True,
        f"{delegated} delegation(s), none required",
    )


# ------------------------------------------------------- decision quality ---


class DecisionJudgment(BaseModel):
    follows: bool = Field(description="Does the decision follow from the findings?")
    consistent: bool = Field(description="Is it consistent with the stated risk levels?")
    conditions_testable: bool = Field(
        default=True, description="If conditions are given, is each one checkable?"
    )
    reasoning: str = Field(description="One or two sentences naming the deciding factor.")


def decision_quality(answer: Answer, *, required: bool = False) -> MetricResult:
    """Does the verdict follow from the findings? The one that needs a model.

    A deterministic check can compare a decision against the highest risk
    level, and that is worth doing - but it cannot tell whether "approve with
    conditions" was reasonable GIVEN those particular conditions. That is a
    judgement, so it gets a judge.

    The deterministic half runs first and can fail the metric on its own, so a
    generous judge cannot rescue an obviously wrong verdict.
    """
    # A policy lookup - "how long is the notification window?" - has no
    # subject to decide about, and `pending` is the correct answer for it. This
    # metric first fired on every such case and failed them all, which is the
    # same defect as a coverage metric demanding four risk domains from a
    # one-line question: a metric that fires on correct behaviour gets switched
    # off, and then it protects nothing.
    if answer.decision == "pending":
        if required:
            return MetricResult("decision_quality", 0.0, False,
                                "an assessment case reached no decision")
        return MetricResult("decision_quality", 1.0, True,
                            "no decision was called for")

    levels = {f.level for f in answer.findings}
    worst = "high" if "high" in levels else "medium" if "medium" in levels else "low"

    # The hard rule: a high risk finding cannot end in unconditional approval.
    if worst == "high" and answer.decision == "approve":
        return MetricResult(
            "decision_quality", 0.0, False,
            "unconditional approval despite a high risk finding",
            {"worst_level": worst, "decision": answer.decision},
        )
    if answer.decision == "approve_with_conditions" and not answer.conditions:
        return MetricResult(
            "decision_quality", 0.0, False,
            "conditional approval with no conditions stated",
        )

    try:
        from agentcore.llm import judge_model

        findings = NL.join(
            f"- {f.domain}: {f.level} ({'assessed' if f.assessed else 'NOT assessed'}) {f.summary}"
            for f in answer.findings
        )
        conditions = NL.join(f"  {i}. {c}" for i, c in enumerate(answer.conditions, 1))
        verdict = judge_model().with_structured_output(
            DecisionJudgment, method="function_calling"
        ).invoke(
            # Deliberately does NOT restate the hard rules above. Giving the
            # judge a rule the deterministic half already enforces made it
            # pattern match: told that "high risk should not end in
            # unconditional approval", it failed a correct APPROVE WITH
            # CONDITIONS as though the conditions were not there. A judge
            # should only ever be asked what it alone can answer.
            "You are checking whether a procurement decision follows from its "
            "findings. The three possible decisions are approve, "
            "approve_with_conditions and reject.\n\n"
            f"FINDINGS:\n{findings}\n\n"
            f"DECISION: {answer.decision}\n"
            f"CONDITIONS:\n{conditions or '  (none)'}\n\n"
            "Approving with conditions over a high risk finding is correct when "
            "the conditions address that risk, so judge whether THESE conditions "
            "address THESE findings. A condition is testable when a person could "
            "check it and say yes or no."
        )
    except Exception as error:  # noqa: BLE001 - an unavailable judge is not a failure
        return MetricResult("decision_quality", 0.5, True,
                            f"judge unavailable ({type(error).__name__}); "
                            f"deterministic checks passed")

    score = sum([verdict.follows, verdict.consistent, verdict.conditions_testable]) / 3
    return MetricResult(
        "decision_quality", round(score, 3), verdict.follows and verdict.consistent,
        verdict.reasoning[:140], {"worst_level": worst},
    )


# ------------------------------------------------------------- all of it ----


def evaluate(answer: Answer, audit: list[dict], *, required_domains: list[str],
             evidence_texts: dict[str, str] | None = None,
             expected_tools: list[str] | None = None,
             forbidden_tools: list[str] | None = None,
             expect_delegation: bool = False,
             decision_required: bool = False) -> list[MetricResult]:
    """Every assessment metric for one run."""
    return [
        citation_correctness(answer, evidence_texts or {}),
        task_completion(answer, required_domains),
        tool_correctness(audit, expected_tools or [], forbidden_tools),
        agent_delegation(audit, expected=expect_delegation),
        decision_quality(answer, required=decision_required),
    ]
