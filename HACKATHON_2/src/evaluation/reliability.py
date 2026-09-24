"""Run ONE eval case many times and report the distribution.

    DOMAIN=vendor_risk uv run python -m evaluation.reliability --runs 10
    DOMAIN=vendor_risk uv run python -m evaluation.reliability --case 5 --runs 10 --label baseline

WHY A SEPARATE TOOL FROM `agent_eval`. That runs every case once and reports an
aggregate. This runs one case many times and reports the spread, which is a
different question and the only one that can answer "did that change help".

The project learned this the hard way, twice in one hour:

  PROBLEMS P53  the aggregate pass rate swung 0.167 to 0.583 on unchanged code,
                so a single run cannot show a regression. One LLM judge flips
                one case, and one case is 8.3 points.

  PROBLEMS P54  the flagship case went 4/4, 4/4, 4/4, 4/4 before a merge and
                2/4, 2/4, 3/4, 4/4 after it. That IS a real signal, and it was
                nearly dismissed as more of P53's noise.

Telling those apart took repeated runs of the SAME case. Nothing else would
have done it, which is why this is a command rather than a note.

Results go to the ledger under the `reliability` experiment, labelled, so two
sides of an A/B comparison sit next to each other permanently.
"""

from __future__ import annotations

import argparse
import statistics
import time
from dataclasses import dataclass, field
from typing import Any

from agentcore.registry import load_domain
from evaluation.agent_eval import run_case


@dataclass
class RunOutcome:
    assessed: int = 0
    required: int = 0
    claims: int = 0
    delegations: int = 0
    steps: int = 0
    decision: str = "?"
    compose_failed: bool = False
    refused: bool = False
    seconds: float = 0.0
    error: str = ""

    @property
    def clean(self) -> bool:
        """Everything the case is supposed to produce, produced."""
        return (
            not self.compose_failed
            and not self.error
            and (self.required == 0 or self.assessed == self.required)
        )


@dataclass
class Distribution:
    label: str
    question: str
    outcomes: list[RunOutcome] = field(default_factory=list)

    def summary(self) -> dict[str, float]:
        if not self.outcomes:
            return {}
        assessed = [o.assessed for o in self.outcomes]
        return {
            "runs": len(self.outcomes),
            "clean_rate": sum(o.clean for o in self.outcomes) / len(self.outcomes),
            "assessed_mean": statistics.mean(assessed),
            "assessed_min": min(assessed),
            "assessed_max": max(assessed),
            "delegations_mean": statistics.mean(o.delegations for o in self.outcomes),
            "compose_failures": sum(o.compose_failed for o in self.outcomes),
            "seconds_mean": statistics.mean(o.seconds for o in self.outcomes),
        }


def one_run(case, required_domains: list[str]) -> RunOutcome:
    """One full pipeline execution, reduced to the numbers worth comparing."""
    started = time.perf_counter()
    outcome = RunOutcome(required=len(required_domains))
    try:
        state = run_case(case)
    except Exception as error:  # noqa: BLE001 - a crashed run is a data point
        outcome.error = f"{type(error).__name__}: {error}"[:120]
        outcome.seconds = time.perf_counter() - started
        return outcome

    answer = state.get("answer")
    audit = state.get("audit", [])

    outcome.assessed = len([f for f in getattr(answer, "findings", []) if f.assessed])
    outcome.claims = len(getattr(answer, "claims", []))
    outcome.decision = getattr(answer, "decision", "?")
    outcome.refused = bool(getattr(answer, "refused", False))
    outcome.delegations = sum(e.get("delegated", 0) for e in audit)
    outcome.steps = len([e for e in audit if e.get("event") == "executing"])
    failures = [e for e in audit if e.get("event") == "compose_failed"]
    outcome.compose_failed = bool(failures)
    if failures:
        outcome.error = str(failures[0].get("error", ""))[:120]
    outcome.seconds = time.perf_counter() - started
    return outcome


def run(domain_name: str | None = None, *, case_index: int = 5, runs: int = 5,
        label: str = "", record_run: bool = True) -> Distribution:
    domain = load_domain(domain_name)
    cases = domain.eval_cases()
    if not 0 <= case_index < len(cases):
        raise SystemExit(f"case {case_index} is out of range: {domain.name} has {len(cases)}")

    case = cases[case_index]
    required = case.expected_domains or []
    result = Distribution(label=label or "run", question=case.question)

    print(f"{domain.name} case {case_index}, {runs} run(s)")
    print(f"  {case.question[:72]}")
    if label:
        print(f"  label: {label}")
    print()

    for attempt in range(1, runs + 1):
        outcome = one_run(case, required)
        result.outcomes.append(outcome)
        mark = "ok  " if outcome.clean else "FAIL"
        coverage = f"{outcome.assessed}/{outcome.required}" if outcome.required else "n/a"
        print(f"  {attempt:>2}. {mark} domains={coverage} decision={outcome.decision:<22}"
              f" claims={outcome.claims:<3} deleg={outcome.delegations} "
              f"steps={outcome.steps} {outcome.seconds:.0f}s")
        if outcome.error:
            print(f"        {outcome.error}")

    summary = result.summary()
    print()
    print(f"  clean          {summary['clean_rate']:.0%}  "
          f"({sum(o.clean for o in result.outcomes)}/{len(result.outcomes)})")
    print(f"  domains        mean {summary['assessed_mean']:.2f}  "
          f"range {summary['assessed_min']:.0f} to {summary['assessed_max']:.0f}")
    print(f"  delegations    mean {summary['delegations_mean']:.2f}")
    print(f"  compose fails  {summary['compose_failures']:.0f}")
    print(f"  seconds        mean {summary['seconds_mean']:.0f}")

    if record_run:
        from evaluation.ledger import record

        record(domain=domain.name, experiment="reliability", arm=label or f"case{case_index}",
               metrics=summary, n=runs, note=case.question[:80])
        print("\n  recorded to the ledger")

    return result


def compare(a: Distribution, b: Distribution) -> None:
    """Print two distributions side by side. The A/B this exists for."""
    left, right = a.summary(), b.summary()
    print()
    print(f"  {'':<16}{a.label:>14}{b.label:>14}")
    for key in ("runs", "clean_rate", "assessed_mean", "delegations_mean", "seconds_mean"):
        print(f"  {key:<16}{left.get(key, 0):>14.2f}{right.get(key, 0):>14.2f}")
    print()
    # Deliberately not a verdict. With ten runs a side this is a signal to read,
    # not a significance test, and dressing it up as one would invite the same
    # over-reading that P53 caused.
    print("  Read the clean rate and the domain mean together. A difference in one")
    print("  and not the other usually means the change moved coverage, not correctness.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--case", type=int, default=5,
                        help="index into domain.eval_cases(); 5 is the vendor_risk assessment")
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--label", default="", help="names this arm in the ledger")
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()
    run(args.domain, case_index=args.case, runs=args.runs,
        label=args.label, record_run=not args.no_record)
