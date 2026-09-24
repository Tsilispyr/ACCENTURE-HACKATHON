"""Run the whole pipeline over the domain's eval cases and grade the results.

Four layers per case, cheapest first:
  1. trajectory checks  deterministic, no LLM - did the process behave?
  2. must_not_say       deterministic string checks - hard failures
  3. assessment metrics citation / coverage / tool / delegation / decision
  4. LLM judge          the rubric, and groundedness for non-adversarial cases

A case is only "passed" if all of them agree. The judge alone is too generous;
the trajectory checks alone cannot tell whether the answer is any good.

WHY THE METRICS SIT HERE rather than in a separate script: they need the same
pipeline run the judge grades. Running the pipeline twice to measure it twice
would cost double and - worse - could measure two different runs, which is how
an evaluation quietly stops describing the system it claims to describe.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from typing import Any

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agentcore.contracts import Actor, EvalCase
from agentcore.pipeline.graph import build_app
from agentcore.registry import load_domain
from agentcore.world import bound
from evaluation import metrics, trajectory
from evaluation.judge import check_groundedness, judge

EVALUATOR = Actor(id="evaluator", role="admin", scope="public")


@dataclass
class CaseResult:
    question: str
    passed: bool
    answer: str = ""
    reasons: list[str] = field(default_factory=list)
    checks: list[trajectory.Check] = field(default_factory=list)
    metrics: list[metrics.MetricResult] = field(default_factory=list)
    groundedness: float | None = None
    adversarial: bool = False

    def scores(self) -> dict[str, float]:
        """Metric name to score, for the ledger and the charts."""
        return {m.name: m.score for m in self.metrics}


def run_case(case: EvalCase, *, auto_approve: bool = True) -> dict[str, Any]:
    """One full pipeline run, with a fresh thread so cases cannot interfere.

    Approvals are auto-approved so the eval measures the PIPELINE rather than a
    human's reaction time - but whether the gate fired is still recorded, and
    the trajectory check asserts on it.
    """
    domain = load_domain()
    app = build_app(MemorySaver())
    request = domain.parse_request(case.question, EVALUATOR)
    config = {"configurable": {"thread_id": f"eval-{request.id}"}}

    with bound(EVALUATOR, request.id):
        state = app.invoke({"request": request, "actor": EVALUATOR}, config)

        rounds = 0
        while "__interrupt__" in state and auto_approve and rounds < 3:
            state = app.invoke(Command(resume={"decision": "approve", "by": "evaluator"}), config)
            rounds += 1

    return state


def grade(case: EvalCase, state: dict) -> CaseResult:
    answer = state.get("answer")
    text = (answer.summary if answer else "") or ""
    result = CaseResult(question=case.question, passed=True, answer=text,
                        adversarial=case.adversarial)

    # 1. process
    result.checks = trajectory.run_checks(state, adversarial=case.adversarial)
    for check in result.checks:
        if not check.passed:
            result.passed = False
            result.reasons.append(f"trajectory: {check.name} ({check.detail})")

    # 2. hard string failures
    lowered = text.lower()
    for forbidden in case.must_not_say:
        if forbidden.lower() in lowered:
            result.passed = False
            result.reasons.append(f"said a forbidden thing: {forbidden!r}")

    # 3. the assessment metrics. Deterministic except for decision quality,
    #    and they run for adversarial cases too: a refusal still has to avoid
    #    inventing citations.
    if answer is not None:
        evidence_texts = {e.cite(): e.text for e in state.get("evidence", [])}
        result.metrics = metrics.evaluate(
            answer,
            state.get("audit", []),
            required_domains=case.expected_domains,
            evidence_texts=evidence_texts,
            expected_tools=case.expected_tools,
            forbidden_tools=case.forbidden_tools,
            expect_delegation=case.expect_delegation,
            # A case that declares risk domains is asking for an assessment,
            # and an assessment that reaches no verdict has not finished.
            decision_required=bool(case.expected_domains),
        )
        for metric in result.metrics:
            if not metric.passed:
                result.passed = False
                result.reasons.append(f"{metric.name}: {metric.detail}")

    # 4. the judge. An adversarial case is graded by whether it refused, which
    #    the trajectory check already decided - no need to pay for a judgment.
    if case.adversarial:
        return result

    sources = "\n\n".join(e.text for e in state.get("evidence", []))
    try:
        verdict = judge(case.question, text, case.rubric)
        if not verdict.passed:
            result.passed = False
            result.reasons.append(f"judge: {verdict.reasoning}")

        if sources and text:
            grounding = check_groundedness(text, sources)
            result.groundedness = grounding.score
            if grounding.unsupported:
                result.passed = False
                result.reasons.append(
                    f"unsupported claims: {'; '.join(grounding.unsupported[:2])}"
                )
    except Exception as error:  # noqa: BLE001 - a broken judge is not a failed case
        result.reasons.append(f"judge unavailable: {type(error).__name__}")

    return result


def run(domain_name: str | None = None, *, limit: int | None = None,
        record_run: bool = True) -> list[CaseResult]:
    """Every eval case, graded and recorded.

    Cases run in order and are NOT short-circuited on failure. A suite that
    stops at the first red tells you one thing is broken; this one tells you
    which things, which is the difference between a number you can act on and
    a number you can only worry about.

    `record_run=False` exists for tests, which must not append to the ledger.
    The ledger is append only precisely so a number on a slide can be traced
    back to the run that produced it, and a test polluting it would break that
    guarantee quietly.
    """
    domain = load_domain(domain_name)
    cases = domain.eval_cases()[:limit]
    results = []

    print(f"Agent eval: {domain.name}, {len(cases)} case(s)\n")
    for i, case in enumerate(cases, 1):
        state = run_case(case)
        result = grade(case, state)
        results.append(result)
        mark = "PASS" if result.passed else "FAIL"
        flag = " [adversarial]" if case.adversarial else ""
        print(f"  {i:>2}. {mark}{flag}  {case.question[:62]}")
        if result.metrics:
            print("        " + "  ".join(
                f"{m.name.split('_')[0]}={m.score:.2f}" for m in result.metrics
            ))
        for reason in result.reasons:
            print(f"        - {reason[:100]}")

    passed = sum(r.passed for r in results)
    rate = passed / max(len(results), 1)
    print(f"\npass rate: {passed}/{len(results)} = {rate:.0%}")

    means = aggregate(results)
    if means:
        print("metric means:")
        for name, value in means.items():
            print(f"   {name:<24} {value:.3f}")

    if record_run:
        try:
            from evaluation.ledger import record

            record(domain=domain.name, experiment="agent", arm="pipeline",
                   metrics={"pass_rate": rate, **means}, n=len(results),
                   note="agent_eval")
        except Exception as error:  # noqa: BLE001 - a ledger failure is not an eval failure
            print(f"   (not recorded: {type(error).__name__})")

    return results


def aggregate(results: list[CaseResult]) -> dict[str, float]:
    """Mean of each metric across cases. What the gate and the charts read."""
    totals: dict[str, list[float]] = {}
    for result in results:
        for name, score in result.scores().items():
            totals.setdefault(name, []).append(score)
    return {name: round(sum(v) / len(v), 3) for name, v in totals.items() if v}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    run(args.domain, limit=args.limit)
