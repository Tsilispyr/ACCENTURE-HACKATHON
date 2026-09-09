"""FastAPI service wrapping the CodeHub ticket triage-and-resolution graph.

Mirrors docerz/day21's proven shape (GET /, GET /chat, POST /incidents) so
the adapted apiclient.py smoke-test script needs no payload changes -- only
POST /incidents now actually runs the graph instead of echoing input.

GET /health and GET /incidents/{id} added on top of that shape to match the
hackathon handout's recommended endpoint list exactly. Incident results are
kept in a simple in-memory dict -- fine for a hackathon demo (single process,
no restart expected mid-run); swap for a real store (e.g. the app-db-init
Postgres database) if that ever changes.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI, HTTPException
from langchain.agents import create_agent
from pydantic import BaseModel
from uvicorn import run

from hackathon1.graph import app_graph
from hackathon1.llm import llm  # loads .env as a side effect, see llm.py
from hackathon1.tools import check_known_issue, lookup_sla_hours
from hackathon1.tracing import get_callback_handlers


class IncidentRequest(BaseModel):
    service: str
    description: str
    severity: str


# In-memory store for GET /incidents/{id} -- keyed by ticket_id, populated by
# POST /incidents. Reset on every app restart; that's fine for a hackathon demo.
_incidents_store: dict[str, dict] = {}


# Lightweight conversational agent for GET /chat -- the Day3 prebuilt-agent
# pattern (see docerz/day21's agent.py), given real CodeHub domain tools
# instead of a generic calculator.
_chat_agent = create_agent(
    model=llm,
    tools=[lookup_sla_hours, check_known_issue],
    system_prompt="You are a CodeHub support assistant. Use the provided tools "
    "to answer questions about SLAs and known issues.",
)

app = FastAPI(title="Hackathon 1 -- CodeHub Ticket Triage")


@app.get("/")
async def root():
    return {"status": "ok"}  # also the compose healthcheck target -- keep this path


@app.get("/health")
async def health():
    """Same check as GET / -- kept as its own literal path since the hackathon
    handout's recommended endpoint list names /health specifically."""
    return {"status": "ok"}


@app.get("/chat")
async def chat(message: str):
    try:
        response = await _chat_agent.ainvoke({"messages": [("user", message)]})
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
    """Runs the full ticket triage-and-resolution graph -- upgrades the
    stub that echoed input in docerz/day21's version of this endpoint into
    the actual business logic. Stores the result so GET /incidents/{id}
    can retrieve it afterwards."""
    ticket_id = f"T-{uuid.uuid4().hex[:8]}"
    initial_state = {
        "ticket_id": ticket_id,
        "ticket_text": incident.description,
        "category": None,
        "complexity": None,
        "messages": [],
        "findings": [],
        "plan_sections": [],
        "handoff_count": 0,
        "resolution": None,
        "report_key": None,
    }
    result = await app_graph.ainvoke(
        initial_state, config={"callbacks": get_callback_handlers()}
    )
    response_body = {
        "ticket_id": ticket_id,
        "service": incident.service,
        "severity": incident.severity,
        "category": result.get("category"),
        "complexity": result.get("complexity"),
        "resolution": result.get("resolution"),
        "findings": result.get("findings"),
        "report_key": result.get("report_key"),
    }
    _incidents_store[ticket_id] = response_body
    return response_body


@app.get("/incidents/{ticket_id}")
async def get_incident(ticket_id: str):
    """Retrieve a previously created incident's result by its ticket_id."""
    incident = _incidents_store.get(ticket_id)
    if incident is None:
        raise HTTPException(status_code=404, detail=f"No incident found with id {ticket_id!r}")
    return incident


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=8000)
