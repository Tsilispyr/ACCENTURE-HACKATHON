"""What one run costs, in tokens and in euro.

    DOMAIN=vendor_risk uv run python -m evaluation.usage
    DOMAIN=vendor_risk uv run python -m evaluation.usage --question "..."

WHY THIS EXISTS. The handout's section 10 asks for "latency / cost - is
execution operationally reasonable". Latency was already measured by
`evaluation.timing`. Cost was not measured at all: the ledger held `seconds`
and nothing else, so "is this operationally reasonable" could only be answered
about half the question.

The hosted trace viewer shows token counts per run, and it is the wrong tool
for this. It needs an account and a network, it shows one run at a time, and
on a projector a Langfuse screen is unreadable. A number somebody can put on a
slide has to come from the ledger like every other number here.

HOW THE TOKENS ARE COUNTED. `UsageMetadataCallbackHandler` is a langchain
callback that accumulates `usage_metadata` from every model response under the
config it is bound to. That is the provider's OWN count, not an estimate from a
tokeniser, so it includes whatever the provider actually billed for - system
prompts, tool schemas, retries.

HOW THE PRICE IS APPLIED. Rates are per MILLION tokens and differ by model,
region and agreement, so they are read from the environment rather than
hard-coded to a number that would quietly go stale:

    LLM_PRICE_INPUT_PER_M   euro per 1M prompt tokens
    LLM_PRICE_OUTPUT_PER_M  euro per 1M completion tokens

Unset means unpriced rather than free: the token counts are still recorded and
the cost is reported as None. A zero would look like a measurement.
"""

from __future__ import annotations

import argparse
import os
import time
from dataclasses import dataclass, field
from typing import Any

# Defaults are DELIBERATELY absent. A plausible looking rate for a model nobody
# checked is worse than no rate, because it produces a number that survives
# into a slide unchallenged.
INPUT_PER_M = "LLM_PRICE_INPUT_PER_M"
OUTPUT_PER_M = "LLM_PRICE_OUTPUT_PER_M"


@dataclass
class Usage:
    """One run's token bill, by stage where the callback can attribute it."""

    question: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0
    seconds: float = 0.0
    per_model: dict[str, dict[str, int]] = field(default_factory=dict)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def cost(self) -> float | None:
        """Euro, or None when no rate is configured. Never 0.0 as a stand-in."""
        rate_in, rate_out = _rate(INPUT_PER_M), _rate(OUTPUT_PER_M)
        if rate_in is None and rate_out is None:
            return None
        return (
            self.input_tokens / 1_000_000 * (rate_in or 0.0)
            + self.output_tokens / 1_000_000 * (rate_out or 0.0)
        )


def _rate(name: str) -> float | None:
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def _collect(handler: Any) -> dict[str, dict[str, int]]:
    """Pull the accumulated counts off the callback, tolerantly.

    The handler exposes `usage_metadata` as {model_name: {input_tokens, ...}}.
    Read defensively: this is a convenience, and a langchain release that
    renames a field must cost a chart, never a run.
    """
    raw = getattr(handler, "usage_metadata", None) or {}
    out: dict[str, dict[str, int]] = {}
    for model, counts in raw.items():
        if not isinstance(counts, dict):
            continue
        out[str(model)] = {
            "input_tokens": int(counts.get("input_tokens", 0) or 0),
            "output_tokens": int(counts.get("output_tokens", 0) or 0),
            "total_tokens": int(counts.get("total_tokens", 0) or 0),
        }
    return out


def measure(question: str, *, auto_approve: bool = True) -> Usage:
    """Run one request with a usage callback bound, and report the bill."""
    from langchain_core.callbacks import UsageMetadataCallbackHandler
    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from agentcore.contracts import Actor
    from agentcore.pipeline.graph import build_app
    from agentcore.registry import load_domain
    from agentcore.world import bound

    actor = Actor(id="meter", role="admin", scope="public")
    domain = load_domain()
    app = build_app(MemorySaver())
    request = domain.parse_request(question, actor)

    handler = UsageMetadataCallbackHandler()
    config = {"configurable": {"thread_id": f"usage-{request.id}"},
              "callbacks": [handler]}

    usage = Usage(question=question)
    started = time.perf_counter()
    with bound(actor, request.id):
        payload: object = {"request": request, "actor": actor}
        rounds = 0
        while True:
            interrupted = False
            for update in app.stream(payload, config, stream_mode="updates"):
                if "__interrupt__" in update:
                    interrupted = True
            if not (interrupted and auto_approve and rounds < 3):
                break
            rounds += 1
            payload = Command(resume={"decision": "approve", "by": "meter"})
    usage.seconds = time.perf_counter() - started

    usage.per_model = _collect(handler)
    usage.input_tokens = sum(m["input_tokens"] for m in usage.per_model.values())
    usage.output_tokens = sum(m["output_tokens"] for m in usage.per_model.values())
    usage.calls = len(usage.per_model)
    return usage


def run(domain_name: str | None = None, *, question: str | None = None,
        record_run: bool = True) -> Usage:
    from agentcore.registry import load_domain

    domain = load_domain(domain_name)
    if question is None:
        cases = [c for c in domain.eval_cases() if not c.adversarial]
        if not cases:
            raise SystemExit(f"{domain.name} has no non-adversarial eval case.")
        # The most demanding case, so the figure is a CEILING rather than a
        # best case. A cost that only covers the easy path is not a budget.
        question = max(cases, key=lambda c: len(c.question)).question

    print(f"Metering {domain.name}: {question[:66]}\n")
    usage = measure(question)

    print(f"  input tokens   {usage.input_tokens:>9,}")
    print(f"  output tokens  {usage.output_tokens:>9,}")
    print(f"  total          {usage.total_tokens:>9,}")
    print(f"  seconds        {usage.seconds:>9.1f}")

    cost = usage.cost()
    if cost is None:
        print(f"\n  cost: NOT PRICED. Set {INPUT_PER_M} and {OUTPUT_PER_M} in .env")
        print("  (euro per MILLION tokens). Token counts above are real either way.")
    else:
        print(f"  cost           EUR {cost:>8.4f}")
        if usage.total_tokens:
            print(f"  per 1k runs    EUR {cost * 1000:>8.2f}")

    if record_run:
        from evaluation.ledger import record

        metrics = {
            "input_tokens": usage.input_tokens,
            "output_tokens": usage.output_tokens,
            "total_tokens": usage.total_tokens,
            "seconds": usage.seconds,
        }
        if cost is not None:
            metrics["cost_eur"] = cost
        record(domain=domain.name, experiment="usage", arm="per_run",
               metrics=metrics, n=1, note=question[:80])
        print("\n  recorded to the ledger")

    return usage


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--domain", default=None)
    parser.add_argument("--question", default=None)
    parser.add_argument("--no-record", action="store_true")
    args = parser.parse_args()
    run(args.domain, question=args.question, record_run=not args.no_record)
