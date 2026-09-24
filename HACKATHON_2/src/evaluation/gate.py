"""The CI gate. Runs the eval layers and EXITS NON-ZERO on regression.

The exit code is the whole point. An eval that prints a number and returns 0 is
a report; an eval that fails the build is a gate. The reference implementation
this is drawn from printed a pass rate and always succeeded, which is why a
regression could land unnoticed.

Thresholds are deliberately blunt. A gate nobody trusts gets disabled.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

REPORTS = Path(__file__).resolve().parents[2] / "evaluation-results" / "reports"

THRESHOLDS = {
    # recall@1 is what a user experiences. Watching only recall@3 let a
    # deliberate corpus regression through in testing: recall@1 halved while
    # recall@3 stayed at 100%, and the gate passed.
    "retrieval_recall_at_1": 0.70,
    "retrieval_recall_at_3": 0.80,
    "retrieval_mrr": 0.75,
    # What the pipeline ACTUALLY receives, after the distance ceiling. The
    # rows above measure ranking and stop short of the gate, which is how a
    # domain once scored 100% recall@1 while the vector arm returned nothing
    # for every question. See PROBLEMS P52.
    "retrieval_gated_recall_at_1": 0.60,
    "agent_pass_rate": 0.70,
    # A fabricated citation is worse than a missing answer, so this floor is
    # the highest of the lot. Coverage is absolute: a risk domain the brief
    # requires is either assessed or the assessment is incomplete.
    "citation_correctness": 0.90,
    "task_completion": 1.00,
}


def run(domain_name: str | None = None, *, skip_agent: bool = False) -> int:
    from agentcore.registry import load_domain

    domain = load_domain(domain_name)
    failures: list[str] = []
    report: dict = {
        "domain": domain.name,
        "at": datetime.now(timezone.utc).isoformat(),
        "thresholds": THRESHOLDS,
    }

    # --- 1. retrieval: fast, no LLM, and the most diagnostic ---------------
    try:
        from evaluation.retrieval_eval import run as run_retrieval

        retrieval = run_retrieval(domain.name, compare=False)
        report["retrieval"] = {"recall": retrieval.recall, "mrr": retrieval.mrr,
                               "cases": retrieval.scored}
        if retrieval.scored:
            for k in (1, 3):
                floor = THRESHOLDS[f"retrieval_recall_at_{k}"]
                actual = retrieval.recall.get(k, 0.0)
                if actual < floor:
                    failures.append(f"recall@{k} {actual:.0%} < {floor:.0%}")
            if retrieval.mrr < THRESHOLDS["retrieval_mrr"]:
                failures.append(
                    f"MRR {retrieval.mrr:.3f} < {THRESHOLDS['retrieval_mrr']:.2f}"
                )

        # What survives the distance ceiling. Everything above measures
        # RANKING and never touches the gate, so a mis-set ceiling passes all
        # of it while the pipeline receives nothing. Not hypothetical: that is
        # exactly what happened to vendor_risk, and BM25 hid it. PROBLEMS P52.
        from agentcore.rag.vector import open_store
        from evaluation.retrieval_eval import gated_yield

        policy = domain.retrieval_policy()
        gated = gated_yield(open_store(domain.corpus().collection),
                            domain.eval_cases(), policy)
        report["retrieval_gated"] = {
            "recall": gated.recall, "mrr": gated.mrr,
            "empty": gated.empty, "cases": gated.scored,
        }
        if gated.scored:
            floor = THRESHOLDS["retrieval_gated_recall_at_1"]
            actual = gated.recall.get(1, 0.0)
            if actual < floor:
                failures.append(
                    f"recall@1 THROUGH THE GATE {actual:.0%} < {floor:.0%} "
                    f"({gated.empty}/{gated.scored} questions returned nothing; "
                    f"max_distance={policy.max_distance} is likely wrong for this "
                    f"corpus - run evaluation.calibrate)"
                )
    except Exception as error:  # noqa: BLE001
        report["retrieval"] = {"error": str(error)[:200]}
        failures.append(f"retrieval eval could not run: {type(error).__name__}")

    # --- 2. agent: slower, costs tokens --------------------------------------
    if not skip_agent:
        from evaluation.agent_eval import run as run_agent

        results = run_agent(domain.name)
        rate = sum(r.passed for r in results) / max(len(results), 1)
        report["agent"] = {
            "pass_rate": rate,
            "cases": [
                {"question": r.question, "passed": r.passed, "reasons": r.reasons}
                for r in results
            ],
        }
        if rate < THRESHOLDS["agent_pass_rate"]:
            failures.append(f"agent pass rate {rate:.0%} < {THRESHOLDS['agent_pass_rate']:.0%}")

        # The assessment metrics, averaged across cases. These catch the
        # failures a pass rate hides: a run can pass its rubric while citing a
        # document that does not exist.
        from evaluation.agent_eval import aggregate

        means = aggregate(results)
        report["metrics"] = means
        for name, floor in (("citation_correctness", THRESHOLDS["citation_correctness"]),
                            ("task_completion", THRESHOLDS["task_completion"])):
            if name in means and means[name] < floor:
                failures.append(f"{name} {means[name]:.2f} < {floor:.2f}")

        # Hard failures, regardless of the aggregate: an adversarial case that
        # was ANSWERED is not a percentage point, it is a bug.
        for result in results:
            if result.adversarial and not result.passed:
                failures.append(f"adversarial case not refused: {result.question[:60]}")
            for reason in result.reasons:
                if reason.startswith("said a forbidden thing"):
                    failures.append(f"{reason} - {result.question[:50]}")

    report["failures"] = failures
    report["passed"] = not failures

    REPORTS.mkdir(parents=True, exist_ok=True)
    path = REPORTS / f"{domain.name}-{datetime.now(timezone.utc):%Y%m%dT%H%M%S}.json"
    path.write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")

    print(f"\n{'=' * 70}")
    if failures:
        print(f"EVAL GATE FAILED ({len(failures)}):")
        for failure in failures:
            print(f"  - {failure}")
    else:
        print("EVAL GATE PASSED")
    print(f"report: {path}")
    return 1 if failures else 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--skip-agent", action="store_true", help="retrieval only; no LLM calls")
    args = parser.parse_args()
    sys.exit(run(args.domain, skip_agent=args.skip_agent))
