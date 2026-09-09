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

from functools import lru_cache

from fastapi import FastAPI, HTTPException
from langchain.agents import create_agent
from pydantic import BaseModel, ConfigDict, Field
from uvicorn import run

from hackathon1.graph import app_graph
from hackathon1.llm import llm  # loads .env as a side effect, see llm.py
from hackathon1.tools import (
    get_incident_history,
    get_service_metrics,
    search_knowledge_base,
    search_logs,
)
from hackathon1.tracing import get_callback_handlers


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


# In-memory store for GET /incidents/{id} -- keyed by ticket_id, populated by
# POST /incidents. Reset on every app restart; that's fine for a hackathon demo.
_incidents_store: dict[str, dict] = {}


# Lightweight conversational agent for GET /chat, repointed onto the incident
# domain after tools.py was rewritten (the old lookup_sla_hours /
# check_known_issue no longer exist).
#
# Built lazily and cached, not at import time. Constructing an agent is a real
# side effect of importing this module otherwise -- it binds tools and an LLM
# client before anyone has asked for one, which slows startup and makes the
# module awkward to import in a test that never touches /chat.
#
# Read-only investigation tools ONLY. The Tier 2 remediation tools --
# scale_connection_pool, restart_service, rollback_change -- are deliberately
# withheld here. They mutate the simulated estate and carry a risk level that
# is supposed to pass the approval gate in the graph; a chat endpoint has no
# approval step, so binding them to it would be a way to run a high-risk action
# without one. That is precisely the control tools.py exists to enforce, and it
# should not have a side door.
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
    """Runs the full ticket triage-and-resolution graph -- upgrades the
    stub that echoed input in docerz/day21's version of this endpoint into
    the actual business logic. Stores the result so GET /incidents/{id}
    can retrieve it afterwards."""
    # The caller's incident id is preserved, never replaced with a generated
    # one -- an operator who posts INC-1042 has to be able to find INC-1042
    # afterwards, and the id is how this correlates to their own systems.
    ticket_id = incident.incident_id
    initial_state = {
        "ticket_id": ticket_id,
        # Both the description and the error text reach the workflow. The error
        # line is usually the most diagnostic part of the report, and dropping
        # it silently would leave the graph investigating a vaguer incident
        # than the one that was actually filed.
        "ticket_text": f"{incident.description}\nError: {incident.error}",
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
        "incident_id": ticket_id,
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
