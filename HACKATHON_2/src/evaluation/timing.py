"""Per-stage wall time, measured by running a real request through the graph.

    DOMAIN=vendor_risk uv run python -m evaluation.timing

WHY THIS IS A COMMAND rather than a note. The stage timings in the ledger were
hand-measured once, and one of them - `s6_act` at 7.1 seconds - was timing a
stage that was CRASHING on every call (PROBLEMS P39). The number sat in the
ledger and would have gone onto a slide, describing a code path that did not
work, and nothing could have contradicted it because nothing else produced it.

The project's rule is that charts are generated from the ledger and never drawn
by hand. A measurement that only one person can reproduce breaks that rule
quietly. So: one command, recorded like every other measurement.

Stages are timed by streaming the graph, which yields one update per node as it
completes. A stage that runs several times - `s6_act` once per step, `s5_gate`
once per revision - is reported as a TOTAL and a call count, because "how long
does acting take" and "how long does one step take" are different questions and
the aggregate is the one that sets the pace of a demo.
"""

from __future__ import annotations

import argparse
import time
from dataclasses import dataclass, field

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agentcore.contracts import Actor
from agentcore.pipeline.graph import build_app
from agentcore.registry import load_domain
from agentcore.world import bound

TIMER = Actor(id="timer", role="admin", scope="public")


@dataclass
class StageTiming:
    stage: str
    seconds: float = 0.0
    calls: int = 0
    slowest: float = 0.0


@dataclass
class RunTiming:
    question: str
    total: float = 0.0
    stages: dict[str, StageTiming] = field(default_factory=dict)
    approvals: int = 0

    def record(self, stage: str, elapsed: float) -> None:
        entry = self.stages.setdefault(stage, StageTiming(stage))
        entry.seconds += elapsed
        entry.calls += 1
        entry.slowest = max(entry.slowest, elapsed)


def time_request(question: str, *, auto_approve: bool = True) -> RunTiming:
    """One request through the whole graph, timing each node as it completes."""
    domain = load_domain()
    app = build_app(MemorySaver())
    request = domain.parse_request(question, TIMER)
    config = {"configurable": {"thread_id": f"timing-{request.id}"}}

    timing = RunTiming(question=question)
    started = time.perf_counter()

    with bound(TIMER, request.id):
        payload: object = {"request": request, "actor": TIMER}
        rounds = 0

        while True:
            mark = time.perf_counter()
            interrupted = False

            for update in app.stream(payload, config, stream_mode="updates"):
                now = time.perf_counter()
                for stage in update:
                    if stage == "__interrupt__":
                        interrupted = True
                        continue
                    timing.record(stage, now - mark)
                mark = now

            if not (interrupted and auto_approve and rounds < 3):
                break
            # Approval time is the HUMAN's, not the system's, so it is excluded
            # from every stage total. A demo's pace depends on the machine.
            timing.approvals += 1
            rounds += 1
            payload = Command(resume={"decision": "approve", "by": "timer"})

    timing.total = time.perf_counter() - started
    return timing


def run(domain_name: str | None = None, *, question: str | None = None,
        record_run: bool = True) -> RunTiming:
    domain = load_domain(domain_name)
    if question is None:
        cases = [c for c in domain.eval_cases() if not c.adversarial]
        if not cases:
            raise SystemExit(f"{domain.name} has no non-adversarial eval case to time.")
        # The most demanding case, so the figure is a ceiling rather than a
        # best case. A timing that only covers the easy path is not a budget.
        question = max(cases, key=lambda c: len(c.question)).question

    print(f"Timing {domain.name}: {question[:70]}\n")
    timing = time_request(question)

    order = sorted(timing.stages.values(), key=lambda s: -s.seconds)
    width = max((len(s.stage) for s in order), default=12)
    for stage in order:
        repeats = f"  x{stage.calls} (slowest {stage.slowest:.2f}s)" if stage.calls > 1 else ""
        print(f"  {stage.stage:<{width}}  {stage.seconds:6.2f}s{repeats}")
    print(f"\n  {'TOTAL':<{width}}  {timing.total:6.2f}s"
          f"{f'  ({timing.approvals} approval round(s) excluded)' if timing.approvals else ''}")

    if record_run:
        from evaluation.ledger import record

        record(domain=domain.name, experiment="stage_timing", arm="total",
               metrics={"seconds": timing.total}, n=1, note=question[:80])
        for stage in order:
            record(domain=domain.name, experiment="stage_timing", arm=stage.stage,
                   metrics={"seconds": stage.seconds}, n=stage.calls, note=question[:80])
        print("\nrecorded to the ledger")

    return timing


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--question", default=None, help="override the case chosen")
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()
    run(args.domain, question=args.question, record_run=not args.no_record)
