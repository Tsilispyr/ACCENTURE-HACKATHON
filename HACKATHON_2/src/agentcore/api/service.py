"""The HTTP surface.

    POST /login                  -> a bearer token carrying the actor's scope
    POST /requests               -> run the pipeline; may pause for approval
    GET  /requests/{id}          -> the state of a run, from the checkpointer
    POST /requests/{id}/approve  -> resume a paused run
    GET  /chat                   -> read-only assistant
    GET  /healthz                -> liveness plus what is actually wired up

`/chat` deliberately binds RETRIEVAL TOOLS ONLY. There is no approval step on a
chat endpoint, so it must have nothing to approve - binding the mutating tools
there would be a way to run a high-risk action without the gate. It is not an
oversight that it can do less.

thread_id == request_id throughout. That one convention is what makes retrieval
and approval-resume work without a second store.
"""

from __future__ import annotations

import os
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from pydantic import BaseModel

from agentcore import observability, tracing
from agentcore.api.security import authenticate, current_actor, issue_token
from agentcore.contracts import Actor
from agentcore.pipeline.graph import build_app
from agentcore.registry import load_domain
from agentcore.world import bound

app = FastAPI(
    title="hackathon2",
    description="Domain-agnostic agentic pipeline: ground, plan, gate, act, verify, answer.",
    version="0.1.0",
)

# Azure Monitor, before anything serves a request. A no-op unless
# APPLICATIONINSIGHTS_CONNECTION_STRING is set, so a local run is unchanged.
# Module scope is the hook point because this app has no factory and no
# lifespan - there is nowhere later that runs exactly once.
observability.configure()
observability.instrument_fastapi(app)

# Langfuse, same rule: on only when keys are present. Reported by /healthz so
# a dead integration cannot hide behind a green health check. DECISIONS D49.
tracing.configure()

# One checkpointer for the process, so a paused run is resumable by a LATER
# request. A fresh one per call would lose every interrupt.
CHECKPOINTER = MemorySaver()
_graph = None


def graph():
    global _graph
    if _graph is None:
        _graph = build_app(CHECKPOINTER)
    return _graph


# ------------------------------------------------------------- schemas -----


class LoginIn(BaseModel):
    email: str
    password: str


class RequestIn(BaseModel):
    text: str | dict[str, Any]


class ApprovalIn(BaseModel):
    decision: str  # "approve" | "reject"
    reason: str = ""


# --------------------------------------------------------------- routes ----


@app.get("/healthz")
def healthz() -> dict[str, Any]:
    """Liveness AND what is actually wired up.

    Reporting the booleans matters: tracing and storage tend to fail SILENTLY,
    returning empty rather than raising, so a dead integration can hide for a
    long time behind a green health check.
    """
    domain = load_domain()
    return {
        "status": "ok",
        "domain": domain.name,
        # The always-on layer. It has no credentials and no network, so it is
        # reported as a constant rather than probed: if the service answers at
        # all, the audit trail works.
        "audit_trail_enabled": True,
        "tracing_enabled": tracing.enabled(),
        # Reported separately because they answer different questions and fail
        # independently: Langfuse is where one run is read while building,
        # Azure Monitor is where a fleet is watched in operation.
        "azure_monitor_enabled": observability.enabled(),
        "mcp_mode": os.getenv("MCP_MODE", "stdio"),
        "graph_arm": bool(domain.graph_queries()),
        "tools": len(domain.local_tools()) + len(domain.systems()),
    }


@app.post("/login")
def login(body: LoginIn) -> dict[str, str]:
    actor = authenticate(body.email, body.password)
    return {"token": issue_token(actor), "scope": actor.scope, "role": actor.role}


@app.post("/requests")
def submit(body: RequestIn, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """Run the pipeline. Returns either an answer or an approval request."""
    domain = load_domain()
    request = domain.parse_request(body.text, actor)
    config = {"configurable": {"thread_id": request.id}}

    with bound(actor, request.id):
        result = graph().invoke({"request": request, "actor": actor}, config)

    return _serialise(request.id, result)


@app.get("/requests/{request_id}")
def get_request(request_id: str, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    state = graph().get_state({"configurable": {"thread_id": request_id}})
    if not state.values:
        raise HTTPException(404, f"No request {request_id}")

    stored = state.values.get("request")
    if stored and stored.actor.id != actor.id and actor.role != "admin":
        # Do not confirm that someone else's request exists.
        raise HTTPException(404, f"No request {request_id}")

    return _serialise(request_id, state.values, interrupts=state.tasks)


@app.post("/requests/{request_id}/approve")
def approve(request_id: str, body: ApprovalIn,
            actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """Resume a run paused at the risk gate."""
    config = {"configurable": {"thread_id": request_id}}
    state = graph().get_state(config)
    if not state.values:
        raise HTTPException(404, f"No request {request_id}")
    if not state.tasks:
        raise HTTPException(409, "This request is not waiting for approval")

    with bound(actor, request_id):
        result = graph().invoke(
            Command(resume={"decision": body.decision, "reason": body.reason, "by": actor.id}),
            config,
        )
    return _serialise(request_id, result)


@app.get("/chat")
def chat(message: str, actor: Actor = Depends(current_actor)) -> dict[str, Any]:
    """Read-only assistant. Retrieval tools only - see the module docstring."""
    from langchain.agents import create_agent

    from agentcore.llm import chat_model
    from agentcore.tools.retrieval_tools import build_retrieval_tools

    domain = load_domain()
    with bound(actor, "chat"):
        agent = create_agent(
            chat_model(),
            tools=build_retrieval_tools(domain),  # deliberately NOT domain.systems()
            system_prompt=domain.persona(),
        )
        result = agent.invoke({"messages": [{"role": "user", "content": message}]})
    return {"response": result["messages"][-1].content, "read_only": True}


# ----------------------------------------------------------- serialising ---


def _serialise(request_id: str, state: dict, interrupts: Any = None) -> dict[str, Any]:
    """Everything that crosses the HTTP boundary passes through here.

    So what is ABSENT matters as much as what is present. `evidence` is
    reported as a count, never as text: the retrieved passages are untrusted
    attacker-influenced content, and echoing them to a client would hand an
    injected document a second delivery route. The citations a reader needs
    are already inside `answer`.

    The `audit` list IS returned in full, deliberately. It is the evidence that
    the guardrails ran, and a system that asks to be trusted should be able to
    show its working.
    """
    answer = state.get("answer")
    plan = state.get("plan")
    pending = state.get("__interrupt__") or ([] if interrupts is None else list(interrupts))

    payload: dict[str, Any] = {
        "request_id": request_id,
        "status": "awaiting_approval" if pending else "complete",
        "answer": answer.model_dump() if answer else None,
        "plan": plan.model_dump() if plan else None,
        "audit": state.get("audit", []),
        "evidence_count": len(state.get("evidence", [])),
    }

    if pending:
        first = pending[0]
        payload["approval_required"] = getattr(first, "value", None) or str(first)
    return payload
