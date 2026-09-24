"""How a run is observed. Three layers, and only one of them always works.

    LANGGRAPH   always on, no account, no container, no network. Every stage
                appends to `state["audit"]`, and the graph streams one update
                per node as it completes. That IS the trace: what ran, in what
                order, what it decided and what it refused. `python -m
                agentcore.tracing "question"` renders it.

    LANGFUSE    optional. The hosted view of one run's prompts and outputs,
                bound once in `pipeline/graph.py`. The course's guide 04 is the
                Langfuse guide and this is the team's choice. On only when keys
                are present, so a machine without credentials runs normally and
                silently.

    AZURE       optional, separate module. `observability.py` exports spans to
                App Insights for operators watching a fleet rather than an
                engineer reading one run.

WHY THE LOCAL LAYER IS THE ONE THAT MATTERS. A hosted trace viewer is worth
having and cannot be relied on: it needs an account, outbound egress and a
working network, and a demo venue reliably supplies none of those. The audit
trail has no such dependency, which is why it carries the evidence a judge
would ask for - which specialist ran, what the gate decided, which tripwire
fired - rather than merely duplicating what a hosted viewer would show.

THE ONE RULE: never let telemetry break the thing it observes. Tracing is
configured defensively and every path degrades to silence.
"""

from __future__ import annotations

import os
from typing import Any

# Importing for the side effect: llm.py holds the only load_dotenv() in the
# codebase, so without this configure() reads an environment that has not been
# loaded yet and concludes that tracing is off when it is not.
import agentcore.llm  # noqa: F401

PUBLIC_KEY = "LANGFUSE_PUBLIC_KEY"
SECRET_KEY = "LANGFUSE_SECRET_KEY"

_configured = False


def configure() -> bool:
    """Report whether Langfuse will trace this process. Idempotent.

    Deliberately thin. Langfuse needs no process-wide switch: the handler is
    constructed in `llm.langfuse_handler()` only when a public key is present,
    and bound once in `graph.build_app()`. There is no environment variable
    that half-enables it, which is the failure this function exists to prevent
    for backends that do have one.
    """
    global _configured
    _configured = True
    return enabled()


def enabled() -> bool:
    """Is Langfuse configured? Reported by /healthz.

    Reported rather than assumed, because tracing fails SILENTLY: a bad key
    returns empty instead of raising, so a dead integration hides for a long
    time behind a green health check.
    """
    return bool(os.getenv(PUBLIC_KEY, "").strip() and os.getenv(SECRET_KEY, "").strip())


def status() -> str:
    """One line for a startup banner. Names the layer that IS on."""
    return "LangGraph audit trail" + (" + Langfuse" if enabled() else "")


# --------------------------------------------------------------- rendering ---
#
# The always-on layer. An audit trail is a list of dicts, which is the right
# shape to store and the wrong one to read at 16:00 with a judge watching.

_NOISE = {"stage", "event"}


def render(audit: list[dict[str, Any]], *, timings: dict[str, float] | None = None) -> str:
    """Turn an audit trail into something a person can read down.

    Grouped in ORDER OF OCCURRENCE, not sorted: the order is the finding. A
    plan revised twice, a gate that fired before grounding, a step retried -
    none of those are visible in a sorted list, and all of them are the kind of
    thing this exists to show.

    A stage is therefore headed again every time it is RE-ENTERED, not once on
    first sight. Heading only on first sight files a replan's events under
    whichever stage happened to run in between, which is the exact reading this
    is supposed to make obvious.

    The stage total is printed against the first visit only. Repeating it under
    each visit would read as a per-visit figure, and it is not one.
    """
    if not audit:
        return "  (no audit events; the run did not reach a stage)"

    lines: list[str] = []
    visits: dict[str, int] = {}
    previous: str | None = None
    for entry in audit:
        stage = str(entry.get("stage", "?"))
        if stage != previous:
            visits[stage] = visits.get(stage, 0) + 1
            if visits[stage] == 1:
                elapsed = (timings or {}).get(stage)
                suffix = f"   {elapsed:.1f}s total" if elapsed is not None else ""
            else:
                suffix = f"   (visit {visits[stage]})"
            lines.append(f"\n  {stage}{suffix}")
            previous = stage
        detail = {k: v for k, v in entry.items() if k not in _NOISE}
        rendered = ", ".join(f"{k}={_short(v)}" for k, v in detail.items())
        lines.append(f"      {entry.get('event', '?')}" + (f"  {rendered}" if rendered else ""))
    return "\n".join(lines)


def _short(value: Any, limit: int = 88) -> str:
    """Keep a trace scannable. A truncated value beats a wrapped one."""
    text = str(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


# --------------------------------------------------------------------- CLI ---


def trace_request(question: str, *, auto_approve: bool = True) -> tuple[list, dict]:
    """Run one request and return its audit trail plus per-stage wall time.

    Streams with `stream_mode="updates"`, which yields once per node as it
    finishes. That is the LangGraph-native observation point: no callback, no
    exporter, nothing to configure and nothing that can be unreachable.
    """
    import time

    from langgraph.checkpoint.memory import MemorySaver
    from langgraph.types import Command

    from agentcore.contracts import Actor
    from agentcore.pipeline.graph import build_app
    from agentcore.registry import load_domain
    from agentcore.world import bound

    actor = Actor(id="tracer", role="admin", scope="public")
    domain = load_domain()
    app = build_app(MemorySaver())
    request = domain.parse_request(question, actor)
    config = {"configurable": {"thread_id": f"trace-{request.id}"}}

    audit: list[dict[str, Any]] = []
    timings: dict[str, float] = {}

    with bound(actor, request.id):
        payload: object = {"request": request, "actor": actor}
        rounds = 0
        while True:
            mark = time.perf_counter()
            interrupted = False
            for update in app.stream(payload, config, stream_mode="updates"):
                now = time.perf_counter()
                for stage, delta in update.items():
                    if stage == "__interrupt__":
                        interrupted = True
                        continue
                    timings[stage] = timings.get(stage, 0.0) + (now - mark)
                    # Each node returns only ITS events, so appending here
                    # keeps them in execution order. Reading the final state
                    # would lose the ordering a revision makes visible.
                    if isinstance(delta, dict):
                        audit.extend(delta.get("audit") or [])
                mark = now
            if not (interrupted and auto_approve and rounds < 3):
                break
            rounds += 1
            payload = Command(resume={"decision": "approve", "by": "tracer"})

    return audit, timings


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Render a run as a trace, locally.")
    parser.add_argument("question", nargs="*", help="the request to trace")
    parser.add_argument("--no-approve", action="store_true",
                        help="stop at the approval gate instead of approving")
    args = parser.parse_args(argv)

    configure()
    question = " ".join(args.question).strip()
    if not question:
        parser.error("give a question to trace")

    print(f"tracing: {question}")
    print(f"observing with: {status()}\n")
    audit, timings = trace_request(question, auto_approve=not args.no_approve)
    print(render(audit, timings=timings))
    print(f"\n  {len(audit)} event(s) across {len(timings)} stage(s), "
          f"{sum(timings.values()):.1f}s")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
