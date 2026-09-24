"""Stage 1 - whatever arrived becomes a Request.

The domain owns parsing, because the shape of an incoming record is exactly the
kind of thing that changes per scenario.
"""

from __future__ import annotations

from typing import Any

from agentcore.contracts import Actor, Request
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain


def run(state: AgentState) -> dict[str, Any]:
    raw = state.get("request")
    if isinstance(raw, Request):
        request = raw
    else:
        domain = load_domain()
        actor = state.get("actor") or Actor(id="anonymous")
        request = domain.parse_request(raw, actor)

    # The zeroes and the None are NOT redundant initialisation, and removing
    # them as boilerplate would be a real bug. The API keeps ONE checkpointer
    # for the process, so a thread that handled an earlier request can carry
    # its counters forward: a new request could start with `replan_count`
    # already at 3 and compose a partial answer without executing anything, or
    # with `approved_plan_revision` still set and walk straight past the
    # approval gate.
    #
    # Resetting here, at the one stage every request passes through first, is
    # what makes the loop caps and the gate per REQUEST rather than per thread.
    return {
        "request": request,
        "replan_count": 0,
        "retrieval_attempts": 0,
        "approved_plan_revision": None,
        "rejected": False,
        "audit": [audit_event("s1_intake", "request_parsed", request_id=request.id)],
    }
