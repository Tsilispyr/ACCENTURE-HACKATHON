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
from hackathon1.tools import check_known_issue, lookup_sla_hours
from hackathon1.tracing import get_callback_handlers


class IncidentRequest(BaseModel):
    service: str
    description: str
    severity: str


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
