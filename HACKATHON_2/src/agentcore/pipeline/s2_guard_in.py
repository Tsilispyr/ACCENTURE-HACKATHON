"""Stage 2 - input guardrails. Deterministic, no LLM.

Runs before retrieval so a hostile request costs nothing. A refusal here jumps
straight to s9, skipping every stage that would spend money.
"""

from __future__ import annotations

from typing import Any

from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.patterns import Refusal, compile_patterns, screen


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    request = state["request"]
    patterns = compile_patterns(domain.blocked_patterns())

    try:
        screen(request.raw_text, patterns)
    except Refusal as refusal:
        return {
            "refusal": refusal.reason,
            "audit": [
                audit_event(
                    "s2_guard_in", "refused",
                    matched=refusal.matched, request_id=request.id,
                )
            ],
        }

    return {"audit": [audit_event("s2_guard_in", "passed")]}


def route(state: AgentState) -> str:
    """A refusal skips everything between here and the output guard."""
    return "s9_guard_out" if state.get("refusal") else "s3_ground"
