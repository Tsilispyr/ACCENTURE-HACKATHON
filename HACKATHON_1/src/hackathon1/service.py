"""FastAPI service for the AI-Powered IT Incident Resolution Agent.

Endpoints follow the handout's recommended list:

    GET  /                        liveness; also the container healthcheck target
    GET  /health                  liveness plus subsystem status
    POST /incidents               submit an incident, run the workflow
    GET  /incidents/{id}          retrieve an incident's current state
    POST /incidents/{id}/approve  approve or reject a high-risk remediation
    GET  /chat                    read-only investigation assistant
    GET  /ui                      operator web page (static, dependency-free)

The workflow is the incident graph in `graph.py`, driven by
`adapters.LiveIncidentAdapter`. State lives in the graph's checkpointer, keyed
by thread id, and **the thread id is the incident id** -- that one convention is
what makes retrieval and approval-resume work without a second store.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from functools import lru_cache
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from langchain.agents import create_agent
from langgraph.types import Command
from pydantic import BaseModel, ConfigDict, Field
from uvicorn import run

from hackathon1.adapters import LiveIncidentAdapter
from hackathon1.graph import create_incident_app
from hackathon1.llm import llm  # loads .env as a side effect, see llm.py
from hackathon1.persistence import open_checkpointer
from hackathon1.tools import (
    get_incident_history,
    get_service_metrics,
    search_knowledge_base,
    search_logs,
)
from hackathon1.tracing import get_callback_handlers

logger = logging.getLogger(__name__)


class IncidentRequest(BaseModel):
    """The agreed five-field incident request, matching the handout's example.

    The wire field names are the handout's exact labels -- "Incident ID",
    "Service", ... -- so aliases carry them while the Python attributes stay
    snake_case. Pydantic reports the *alias* in validation errors, which is what
    lets a caller who omits "Severity" see that name back rather than an
    internal one.

    Every field is required on purpose: a silently-defaulted severity or a
    generated incident id would let a malformed request reach the workflow and
    be investigated as though it were real.
    """

    model_config = ConfigDict(populate_by_name=True)

    incident_id: str = Field(alias="Incident ID")
    service: str = Field(alias="Service")
    severity: str = Field(alias="Severity")
    description: str = Field(alias="Description")
    error: str = Field(alias="Error")


class ApprovalDecision(BaseModel):
    """Decision on a paused high-risk remediation."""

    approved: bool
    note: str | None = None


#: Set by the lifespan below. None until startup completes, and None forever if
#: Postgres is unreachable -- in which case create_incident_app falls back to its
#: own in-memory saver.
_checkpointer = None
_close_checkpointer = None


@asynccontextmanager
async def lifespan(_: FastAPI):
    """Open the checkpoint store once for the process, and close it cleanly.

    The pool belongs to the application lifetime, not to a request: opening one
    per incident would be both slow and a good way to exhaust Postgres
    connections under any real load.
    """
    global _checkpointer, _close_checkpointer
    _checkpointer, _close_checkpointer = await open_checkpointer()
    get_incident_app.cache_clear()  # rebuild with the real checkpointer
    try:
        yield
    finally:
        if _close_checkpointer is not None:
            await _close_checkpointer()


@lru_cache(maxsize=1)
def get_incident_app():
    """The compiled incident workflow, built once and reused.

    Lazy so that importing this module does not construct an LLM-backed adapter
    as a side effect -- which matters for any test that never runs the workflow.
    """
    return create_incident_app(LiveIncidentAdapter(), checkpointer=_checkpointer)


# Read-only investigation tools ONLY for /chat. The Tier 2 remediation tools --
# scale_connection_pool, restart_service, rollback_change -- are deliberately
# withheld: they mutate the estate and carry a risk level meant to pass the
# approval gate in the graph. A chat endpoint has no approval step, so binding
# them here would be a way to run a high-risk action without one. That is
# exactly the control tools.py exists to enforce; it should not have a side door.
@lru_cache(maxsize=1)
def get_chat_agent():
    """The /chat agent, created on first use and reused thereafter."""
    return create_agent(
        model=llm,
        tools=[search_logs, get_service_metrics, search_knowledge_base, get_incident_history],
        system_prompt=(
            "You are an IT operations assistant. Use the provided read-only tools to "
            "investigate services: application logs, metrics against baseline, "
            "operational runbooks and past incidents. You can diagnose, but you "
            "cannot change anything."
        ),
    )


app = FastAPI(title="AI-Powered IT Incident Resolution Agent", lifespan=lifespan)

# Permissive by default: everything here binds to localhost and holds no real
# data, and a browser frontend is expected. Tighten via configuration before
# this is ever exposed beyond a demo host.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _thread(incident_id: str) -> dict:
    """Config addressing one incident's checkpoint. thread_id IS the incident id."""
    return {"configurable": {"thread_id": incident_id}}


def _pending_approval(result: dict[str, Any]) -> dict | None:
    """The approval payload if the graph paused, else None.

    LangGraph reports a paused run through `__interrupt__`; its shape has
    varied across versions, so this reads defensively rather than assuming.
    """
    interrupts = result.get("__interrupt__")
    if not interrupts:
        return None
    first = interrupts[0]
    value = getattr(first, "value", first)
    return value if isinstance(value, dict) else {"message": str(value)}


def _serialise(state: dict[str, Any]) -> dict[str, Any]:
    """Flatten graph state into a JSON response carrying the mandated fields."""

    def dump(key):
        value = state.get(key)
        return value.model_dump(mode="json") if hasattr(value, "model_dump") else value

    return {
        "incident_id": state.get("incident_id"),
        "service": state.get("service"),
        "severity": state.get("severity"),
        "triage": dump("triage"),
        "evidence": [
            item.model_dump(mode="json") if hasattr(item, "model_dump") else item
            for item in state.get("investigation_results", [])
        ],
        "diagnosis": dump("diagnosis"),
        "remediation_plan": dump("remediation_plan"),
        "risk_assessment": dump("risk_assessment"),
        "approval_status": state.get("approval_status", "not_required"),
        "execution_attempts": state.get("execution_attempts", 0),
        "execution_result": dump("execution_result"),
        "verification_result": dump("verification_result"),
        "final_report": dump("final_report"),
    }


#: Served at /ui, not /, because GET / is the container healthcheck target and
#: has to keep returning JSON. Read once at startup rather than per request --
#: it is a static file, and re-reading it on every page load would be pointless
#: I/O; a rebuild is what ships a change anyway.
_UI_PATH = Path(__file__).parent / "static" / "index.html"


@app.get("/ui", response_class=HTMLResponse)
async def ui():
    """The operator-facing page: the five-field form and the workflow's output."""
    try:
        return HTMLResponse(_UI_PATH.read_text(encoding="utf-8"))
    except OSError as exc:
        # The API is the product; a missing static file must not take it down.
        logger.warning("UI unavailable: %s", exc)
        raise HTTPException(status_code=404, detail="UI is not available in this build")


@app.get("/")
async def root():
    return {"status": "ok"}  # also the compose healthcheck target -- keep this path


@app.get("/health")
async def health():
    """Liveness plus which optional subsystems are actually live.

    Tracing and report storage degrade silently by design when unconfigured,
    which once hid the fact that neither was running at all. Reporting them
    here makes that visible instead of invisible.
    """
    from hackathon1 import storage

    from hackathon1 import persistence

    return {
        "status": "ok",
        "tracing_enabled": bool(get_callback_handlers()),
        "storage_enabled": storage._get_client() is not None,
        # "memory" means an incident awaiting approval will not survive a restart.
        "checkpoint_backend": persistence.BACKEND,
    }


@app.get("/chat")
async def chat(message: str):
    try:
        response = await get_chat_agent().ainvoke({"messages": [("user", message)]})
        return {"response": response["messages"][-1].content, "status": "success"}
    except Exception as e:
        return {
            "response": None,
            "status": "error",
            "error_type": type(e).__name__,
            "error_message": str(e),
            "reason": "Tool execution failed or agent could not complete the task",
        }


@app.post("/incidents")
async def create_incident(incident: IncidentRequest):
    """Run the incident workflow.

    Returns the completed incident, or -- when the remediation is high risk --
    pauses at the approval gate and returns `awaiting_approval` with the plan
    and its risk assessment for a human to decide on.
    """
    initial_state = {
        "incident_id": incident.incident_id,
        "service": incident.service,
        "severity": incident.severity,
        # Both the description and the error text reach the workflow. The error
        # line is usually the most diagnostic part of the report, and dropping
        # it would leave the graph investigating a vaguer incident than the one
        # that was actually filed.
        "description": f"{incident.description}\nError: {incident.error}",
    }
    config = _thread(incident.incident_id)
    config["callbacks"] = get_callback_handlers()

    result = await get_incident_app().ainvoke(initial_state, config=config)
    return _incident_response(incident.incident_id, result)


def _incident_response(incident_id: str, result: dict[str, Any]) -> dict[str, Any]:
    pending = _pending_approval(result)
    body = _serialise(result)
    if pending is not None:
        body["status"] = "awaiting_approval"
        body["approval_request"] = pending
    else:
        report = result.get("final_report")
        body["status"] = getattr(report, "status", "completed")
    body["incident_id"] = body.get("incident_id") or incident_id
    return body


@app.get("/incidents/{incident_id}")
async def get_incident(incident_id: str):
    """Retrieve an incident's current state from the checkpointer."""
    snapshot = await get_incident_app().aget_state(_thread(incident_id))
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No incident found with id {incident_id!r}")

    body = _serialise(snapshot.values)
    # A run paused at the approval gate still has tasks left to do; a finished
    # one does not. That is how a waiting incident is told from a completed one
    # without keeping separate bookkeeping.
    if snapshot.next:
        body["status"] = "awaiting_approval"
    else:
        report = snapshot.values.get("final_report")
        body["status"] = getattr(report, "status", "completed")
    return body


@app.post("/incidents/{incident_id}/approve")
async def approve_incident(incident_id: str, decision: ApprovalDecision):
    """Approve or reject a paused high-risk remediation, and resume the run.

    The graph re-checks the approval against the *plan revision* it applies to,
    so approving one plan does not authorise a different one produced by a later
    replan. This endpoint only carries the decision; it does not grant anything.
    """
    config = _thread(incident_id)
    snapshot = await get_incident_app().aget_state(config)
    if not snapshot or not snapshot.values:
        raise HTTPException(status_code=404, detail=f"No incident found with id {incident_id!r}")
    if not snapshot.next:
        raise HTTPException(
            status_code=409,
            detail=f"Incident {incident_id!r} is not waiting for approval.",
        )

    config["callbacks"] = get_callback_handlers()
    result = await get_incident_app().ainvoke(
        Command(resume={"approved": decision.approved, "note": decision.note}), config=config
    )
    return _incident_response(incident_id, result)


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=8000)
