"""FastAPI service wrapping the CodeHub ticket triage-and-resolution graph.

Mirrors docerz/day21's proven shape (GET /, GET /chat, POST /incidents) so
the adapted apiclient.py smoke-test script needs no payload changes -- only
POST /incidents now actually runs the graph instead of echoing input.
"""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from langchain.agents import create_agent
from pydantic import BaseModel
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
    service: str
    description: str
    severity: str


# Lightweight conversational agent for GET /chat, repointed onto the incident
# domain after tools.py was rewritten (the old lookup_sla_hours /
# check_known_issue no longer exist).
#
# Read-only investigation tools ONLY. The Tier 2 remediation tools --
# scale_connection_pool, restart_service, rollback_change -- are deliberately
# withheld here. They mutate the simulated estate and carry a risk level that
# is supposed to pass the approval gate in the graph; a chat endpoint has no
# approval step, so binding them to it would be a way to run a high-risk action
# without one. That is precisely the control tools.py exists to enforce, and it
# should not have a side door.
_chat_agent = create_agent(
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
async def health():
    return {"status": "ok"}  # health check; also the compose healthcheck target


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
    the actual business logic."""
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
    return {
        "ticket_id": ticket_id,
        "service": incident.service,
        "severity": incident.severity,
        "category": result.get("category"),
        "complexity": result.get("complexity"),
        "resolution": result.get("resolution"),
        "findings": result.get("findings"),
        "report_key": result.get("report_key"),
    }


if __name__ == "__main__":
    run(app, host="0.0.0.0", port=8000)
