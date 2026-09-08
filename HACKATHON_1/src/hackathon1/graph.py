"""CodeHub ticket triage-and-resolution graph.

Combines both of Day3's routing mechanisms (add_conditional_edges and
Command) plus the three patterns HACKATHON1.md names explicitly --
Orchestrator-Workers, Multi-Agent Supervisor, and Async LangGraph -- in one
cohesive graph, instead of three disconnected demos:

    START -> classify_ticket
               -> route_after_classify (add_conditional_edges)
                    -> "orchestrator" branch (complex tickets):
                         orchestrator -> dispatch_sections (Send fan-out)
                           -> investigate_section x N (parallel, async)
                                -> synthesize_incident_report (fan-in) -> END
                    -> "supervisor" branch (simple tickets):
                         supervisor (Command) -> billing_agent / tech_agent / account_agent
                           -> Command(goto=<other specialist> | END)

Every node is async (ainvoke throughout, graph invoked only via
app_graph.ainvoke) -- this is the one pattern with no working example
anywhere else in the repo: day04 has none, and day21/src/day21/aa.py's
attempt references an undefined `llm`, never calls `.invoke()` (missing
parens), and is never wired into a graph.
"""

from __future__ import annotations

import operator
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, Send
from pydantic import BaseModel, Field

from hackathon1.llm import llm
from hackathon1.storage import upload_report

# Only one specialist-to-specialist handoff is ever allowed, total.
# day04/sessionb/swarm.py's peer-to-peer pattern has no such cap and can
# bounce between two specialists forever if each keeps flagging the other as
# needed (per notes/07-HACKATHON1-REQUIREMENTS.md). This cap is a deliberate
# fix for that exact risk, applied here even though the supervisor's
# asymmetric structure (one initial route, then at most one handoff) is
# inherently less cycle-prone than swarm.py's fully peer-to-peer version.
MAX_HANDOFFS = 1


# ---------------------------------------------------------------------------
# State + structured-output schemas
# ---------------------------------------------------------------------------

class TicketState(TypedDict):
    ticket_id: str
    ticket_text: str
    category: Literal["billing", "tech", "account", "incident"] | None
    complexity: Literal["simple", "complex"] | None
    messages: Annotated[list[BaseMessage], add_messages]
    findings: Annotated[list[str], operator.add]
    plan_sections: list[str]
    handoff_count: int
    resolution: str | None
    report_key: str | None


class WorkerState(TypedDict):
    """Per-worker state for one orchestrator-dispatched investigation section."""

    ticket_text: str
    section: str


class Classification(BaseModel):
    category: Literal["billing", "tech", "account", "incident"] = Field(
        description="which team owns this ticket"
    )
    complexity: Literal["simple", "complex"] = Field(
        description="'complex' tickets get a multi-angle investigation before anyone replies; "
        "'simple' tickets go straight to a specialist"
    )


class Plan(BaseModel):
    sections: list[str] = Field(
        description="2-4 short, concrete investigation angles for this incident"
    )


class Route(BaseModel):
    agent: Literal["billing_agent", "tech_agent", "account_agent"] = Field(
        description="which specialist should handle this ticket first"
    )


class HandoffCheck(BaseModel):
    needs_other_specialist: bool = Field(
        description="True only if the ticket ALSO needs a different specialist's expertise"
    )
    other_specialist: Literal["billing_agent", "tech_agent", "account_agent"] | None = Field(
        default=None, description="which specialist, only if needs_other_specialist is True"
    )


# ---------------------------------------------------------------------------
# Classification + routing (mechanism 1: add_conditional_edges, day03/ex10.py)
# ---------------------------------------------------------------------------

async def classify_ticket(state: TicketState) -> dict:
    """Reads 'ticket_text', writes 'category' and 'complexity'.

    Rejects empty input before spending an LLM call on it -- validate at the
    boundary, per notes/03-THEORY.md's Day1 section, rather than sending
    garbage to the model.
    """
    if not state["ticket_text"] or not state["ticket_text"].strip():
        raise ValueError("ticket_text must not be empty")
    result = await llm.with_structured_output(Classification, method="function_calling").ainvoke(
        f"Classify this CodeHub support ticket: {state['ticket_text']!r}"
    )
    return {"category": result.category, "complexity": result.complexity}


def route_after_classify(state: TicketState) -> Literal["orchestrator", "supervisor"]:
    """Pure, LLM-free router -- 'complex' tickets get investigated first,
    'simple' tickets go straight to a specialist."""
    return "orchestrator" if state["complexity"] == "complex" else "supervisor"


# ---------------------------------------------------------------------------
# Orchestrator-Workers branch (complex tickets)
# ---------------------------------------------------------------------------

async def orchestrator(state: TicketState) -> dict:
    """Plans 2-4 investigation angles. Fan-out itself lives in
    dispatch_sections, kept as a separate function so planning is
    independently testable (unlike day04/sessionb/orchestrator.py, which
    fuses planning and fan-out into one function)."""
    plan = await llm.with_structured_output(Plan, method="function_calling").ainvoke(
        f"Plan investigation angles for this CodeHub incident: {state['ticket_text']!r}"
    )
    return {"plan_sections": plan.sections}


def dispatch_sections(state: TicketState) -> list[Send]:
    """Pure fan-out: one Send per planned section, dispatched in parallel.
    The worker count is decided at runtime by the LLM's plan, not fixed --
    the actual dynamic-fan-out mechanism from day04/sessionb/orchestrator.py."""
    return [
        Send("investigate_section", {"ticket_text": state["ticket_text"], "section": section})
        for section in state["plan_sections"]
    ]


async def investigate_section(state: WorkerState) -> dict:
    """One worker, run once per planned section, in parallel with the others."""
    finding = await llm.ainvoke(
        f"Investigate the '{state['section']}' angle of this CodeHub incident: "
        f"{state['ticket_text']!r}. One short paragraph."
    )
    return {"findings": [f"## {state['section']}\n{finding.content}"]}


async def synthesize_incident_report(state: TicketState) -> dict:
    """Fan-in: runs once every investigate_section worker from this
    Send-dispatched superstep has finished (same synchronization
    day04/sessionb/parallel.py's static fan-out relies on). Combines
    findings into a resolution and best-effort uploads the full report to
    MinIO -- a failure there must not fail ticket resolution (see storage.py)."""
    combined = "\n\n".join(state["findings"])
    summary = await llm.ainvoke(
        f"Summarize this incident investigation into a short resolution note "
        f"for the customer:\n\n{combined}"
    )
    resolution = summary.content
    report_key = upload_report(state["ticket_id"], f"{combined}\n\n---\n\n{resolution}")
    return {"resolution": resolution, "report_key": report_key}


# ---------------------------------------------------------------------------
# Multi-Agent Supervisor branch (simple tickets)
# (mechanism 2: Command(goto=...), day04/sessionb/multi_agent.py)
# ---------------------------------------------------------------------------

async def supervisor(state: TicketState) -> Command[Literal["billing_agent", "tech_agent", "account_agent"]]:
    """Routes to the first specialist. Combines the state update and the
    routing decision in one return value (Command) -- the alternative to
    add_conditional_edges used above for the orchestrator branch; see
    notes/03-THEORY.md for when to reach for which."""
    route = await llm.with_structured_output(Route, method="function_calling").ainvoke(
        f"Which specialist should first handle this ticket -- billing_agent, tech_agent, "
        f"or account_agent? {state['ticket_text']!r}"
    )
    return Command(goto=route.agent)


async def _specialist(
    state: TicketState, name: str, domain: str
) -> Command[Literal["billing_agent", "tech_agent", "account_agent", "__end__"]]:
    """Shared logic for all three specialists: reply, then decide whether to
    hand off -- capped at MAX_HANDOFFS total hops (see module docstring)."""
    reply = await llm.ainvoke(
        f"As the {domain} specialist, address this CodeHub ticket: {state['ticket_text']!r}"
    )
    resolution_so_far = state.get("resolution") or ""
    resolution = f"{resolution_so_far}\n[{name}] {reply.content}".strip()
    msg = AIMessage(content=f"[{name}] {reply.content}")

    handoff_count = state.get("handoff_count", 0)
    if handoff_count < MAX_HANDOFFS:
        check = await llm.with_structured_output(HandoffCheck, method="function_calling").ainvoke(
            f"{domain.capitalize()} specialist reviewing: {state['ticket_text']!r}. "
            f"Does this ALSO need a different specialist (not {name})?"
        )
        if check.needs_other_specialist and check.other_specialist and check.other_specialist != name:
            return Command(
                goto=check.other_specialist,
                update={"resolution": resolution, "handoff_count": handoff_count + 1, "messages": [msg]},
            )

    return Command(goto=END, update={"resolution": resolution, "messages": [msg]})


async def billing_agent(state: TicketState):
    """Handle billing issues; hand off once if the ticket also needs tech or account help."""
    return await _specialist(state, "billing_agent", "billing")


async def tech_agent(state: TicketState):
    """Handle technical issues; hand off once if the ticket also needs billing or account help."""
    return await _specialist(state, "tech_agent", "technical support")


async def account_agent(state: TicketState):
    """Handle account issues; hand off once if the ticket also needs billing or tech help."""
    return await _specialist(state, "account_agent", "account")


# ---------------------------------------------------------------------------
# Graph assembly
# ---------------------------------------------------------------------------

def build_graph() -> StateGraph:
    graph = StateGraph(TicketState)

    graph.add_node("classify_ticket", classify_ticket)
    graph.add_node("orchestrator", orchestrator)
    graph.add_node("investigate_section", investigate_section)
    graph.add_node("synthesize_incident_report", synthesize_incident_report)
    graph.add_node("supervisor", supervisor)
    graph.add_node("billing_agent", billing_agent)
    graph.add_node("tech_agent", tech_agent)
    graph.add_node("account_agent", account_agent)

    graph.add_edge(START, "classify_ticket")
    graph.add_conditional_edges(
        "classify_ticket", route_after_classify, ["orchestrator", "supervisor"]
    )

    # Orchestrator-Workers branch
    graph.add_conditional_edges("orchestrator", dispatch_sections, ["investigate_section"])
    graph.add_edge("investigate_section", "synthesize_incident_report")
    graph.add_edge("synthesize_incident_report", END)

    # Multi-Agent Supervisor branch -- no static edges out of supervisor/
    # billing_agent/tech_agent/account_agent: each returns Command(goto=...),
    # which handles routing (including END) itself.

    return graph


# Built once at import time, same convention as every llm/agent/graph in
# this repo (day02-day04, docerz, day21) -- StateGraph.compile() only
# validates the graph shape, it makes no network calls.
app_graph = build_graph().compile()
