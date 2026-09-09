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
from typing import Annotated, Any, Literal, Protocol, TypedDict, TypeVar, runtime_checkable

from langchain_core.messages import AIMessage, BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.types import Command, Send, interrupt
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


# ===========================================================================
# Incident-resolution workflow
# ===========================================================================
#
# This is intentionally separate from the ticket graph above.  The ticket
# graph is the service's existing compatibility surface; the workflow below
# can be constructed with a fake adapter and exercised without an LLM,
# storage, tools, or any other external service.

MAX_EXECUTION_ATTEMPTS = 3

InvestigationArea = Literal["logs", "metrics", "dependencies", "recent_changes"]
Severity = Literal["low", "medium", "high", "critical"]
RiskLevel = Literal["low", "medium", "high", "critical"]


class IncidentTriage(BaseModel):
    """Structured output of incident triage.

    Severity describes the incident's impact.  It is deliberately not used
    to decide whether approval is needed; remediation risk is assessed later
    from the proposed actions.
    """

    severity: Severity
    summary: str
    investigation_areas: list[InvestigationArea] = Field(
        description="Three or four investigation areas to run concurrently"
    )


class InvestigationResult(BaseModel):
    area: InvestigationArea
    status: Literal["succeeded", "failed"]
    summary: str
    evidence: list[str] = Field(default_factory=list)
    error: str | None = None


class DiagnosisResult(BaseModel):
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    contributing_factors: list[str] = Field(default_factory=list)


class RemediationPlan(BaseModel):
    revision: int = Field(ge=0)
    summary: str
    steps: list[str]
    rollback_steps: list[str] = Field(default_factory=list)


class RiskAssessment(BaseModel):
    risk_level: RiskLevel
    reasons: list[str] = Field(default_factory=list)
    requires_approval: bool = False


class ExecutionResult(BaseModel):
    attempt: int = Field(ge=1)
    status: Literal["succeeded", "failed"]
    summary: str
    simulated: bool = True
    error: str | None = None


class VerificationResult(BaseModel):
    status: Literal["passed", "failed"]
    summary: str
    checks: list[str] = Field(default_factory=list)


class IncidentReport(BaseModel):
    incident_id: str
    status: Literal["resolved", "unresolved", "approval_rejected"]
    summary: str
    execution_attempts: int
    plan_revision: int | None = None
    simulated: bool = True


class IncidentState(TypedDict, total=False):
    incident_id: str
    service: str
    description: str
    severity: Severity
    triage: IncidentTriage
    investigation_areas: list[InvestigationArea]
    investigation_results: Annotated[list[InvestigationResult], operator.add]
    diagnosis: DiagnosisResult
    remediation_plan: RemediationPlan
    risk_assessment: RiskAssessment
    approval_status: Literal["not_required", "pending", "approved", "rejected"]
    approved_plan_revision: int | None
    execution_attempts: int
    execution_result: ExecutionResult
    verification_result: VerificationResult
    final_report: IncidentReport


class IncidentWorkerState(TypedDict, total=False):
    """Private state sent to one parallel investigation worker."""

    incident_id: str
    service: str
    description: str
    severity: Severity
    triage: IncidentTriage
    investigation_area: InvestigationArea


@runtime_checkable
class IncidentAdapter(Protocol):
    """Async boundary for every environment-specific incident operation."""

    async def triage(self, state: IncidentState) -> IncidentTriage: ...

    async def investigate(
        self, area: InvestigationArea, state: IncidentState | IncidentWorkerState
    ) -> InvestigationResult: ...

    async def diagnose(self, state: IncidentState) -> DiagnosisResult: ...

    async def plan_remediation(self, state: IncidentState) -> RemediationPlan: ...

    async def assess_risk(self, state: IncidentState) -> RiskAssessment: ...

    async def execute(self, state: IncidentState) -> ExecutionResult: ...

    async def verify(self, state: IncidentState) -> VerificationResult: ...


class DeterministicIncidentAdapter:
    """No-I/O adapter for demos and tests.

    Failure counts are configurable so retry, exhaustion, and partial
    investigation behavior can be tested deterministically.  ``execute``
    only describes a simulated action and never mutates a real system.
    """

    def __init__(
        self,
        *,
        risk_level: RiskLevel = "low",
        failing_investigations: set[InvestigationArea] | None = None,
        execution_failures_before_success: int = 0,
        verification_failures_before_success: int = 0,
        investigation_areas: list[InvestigationArea] | None = None,
    ) -> None:
        self.risk_level = risk_level
        self.failing_investigations = set(failing_investigations or ())
        self.execution_failures_before_success = execution_failures_before_success
        self.verification_failures_before_success = verification_failures_before_success
        self.investigation_areas = investigation_areas or ["logs", "metrics", "dependencies"]

    async def triage(self, state: IncidentState) -> IncidentTriage:
        severity = str(state.get("severity", "medium")).lower()
        if severity not in {"low", "medium", "high", "critical"}:
            severity = "medium"
        description = state.get("description", "").strip() or "Unspecified incident"
        return IncidentTriage(
            severity=severity,  # type: ignore[arg-type]
            summary=description,
            investigation_areas=list(self.investigation_areas),
        )

    async def investigate(
        self, area: InvestigationArea, state: IncidentState | IncidentWorkerState
    ) -> InvestigationResult:
        if area in self.failing_investigations:
            raise RuntimeError(f"{area} investigation unavailable")
        service = state.get("service", "affected service")
        return InvestigationResult(
            area=area,
            status="succeeded",
            summary=f"Deterministic {area} review completed for {service}.",
            evidence=[f"simulated:{area}:normalised-signal"],
        )

    async def diagnose(self, state: IncidentState) -> DiagnosisResult:
        successful = [
            item.area
            for item in state.get("investigation_results", [])
            if item.status == "succeeded"
        ]
        basis = ", ".join(successful) if successful else "fallback evidence"
        return DiagnosisResult(
            root_cause=f"Configuration drift indicated by {basis}.",
            confidence=0.85 if successful else 0.35,
            contributing_factors=["recent configuration change"],
        )

    async def plan_remediation(self, state: IncidentState) -> RemediationPlan:
        revision = state.get("execution_attempts", 0)
        return RemediationPlan(
            revision=revision,
            summary=f"Simulated remediation revision {revision}.",
            steps=["Simulate restoring the last known-good configuration"],
            rollback_steps=["Simulate restoring the pre-remediation snapshot"],
        )

    async def assess_risk(self, state: IncidentState) -> RiskAssessment:
        requires_approval = self.risk_level in {"high", "critical"}
        return RiskAssessment(
            risk_level=self.risk_level,
            reasons=["Deterministic policy assessment"],
            requires_approval=requires_approval,
        )

    async def execute(self, state: IncidentState) -> ExecutionResult:
        attempt = state.get("execution_attempts", 0) + 1
        failed = attempt <= self.execution_failures_before_success
        return ExecutionResult(
            attempt=attempt,
            status="failed" if failed else "succeeded",
            summary=(
                f"Simulated remediation attempt {attempt} failed."
                if failed
                else f"Simulated remediation attempt {attempt} completed."
            ),
            simulated=True,
            error="deterministic simulated failure" if failed else None,
        )

    async def verify(self, state: IncidentState) -> VerificationResult:
        attempt = state.get("execution_attempts", 0)
        execution = state.get("execution_result")
        failed = (
            execution is None
            or execution.status == "failed"
            or attempt <= self.verification_failures_before_success
        )
        return VerificationResult(
            status="failed" if failed else "passed",
            summary="Simulated checks failed." if failed else "Simulated checks passed.",
            checks=["health", "error-rate", "latency"],
        )


# A concise alias is useful in tests while keeping the implementation's intent
# explicit in production imports.
FakeIncidentAdapter = DeterministicIncidentAdapter

_IncidentModel = TypeVar("_IncidentModel", bound=BaseModel)
_DEFAULT_INVESTIGATION_AREAS: tuple[InvestigationArea, ...] = (
    "logs",
    "metrics",
    "dependencies",
    "recent_changes",
)


def _as_model(model: type[_IncidentModel], value: Any) -> _IncidentModel:
    """Accept model instances or mapping-like fake outputs."""
    return value if isinstance(value, model) else model.model_validate(value)


def _normalise_investigation_areas(
    requested: list[InvestigationArea],
) -> list[InvestigationArea]:
    """Return a stable, unique set of three or four supported branches."""
    selected: list[InvestigationArea] = []
    for area in requested:
        if area in _DEFAULT_INVESTIGATION_AREAS and area not in selected:
            selected.append(area)
    for area in _DEFAULT_INVESTIGATION_AREAS:
        if len(selected) >= 3:
            break
        if area not in selected:
            selected.append(area)
    return selected[:4]


async def triage(state: IncidentState, *, adapter: IncidentAdapter) -> dict:
    if not state.get("description", "").strip():
        raise ValueError("description must not be empty")
    result = _as_model(IncidentTriage, await adapter.triage(state))
    areas = _normalise_investigation_areas(result.investigation_areas)
    result = result.model_copy(update={"investigation_areas": areas})
    return {
        "triage": result,
        "severity": result.severity,
        "investigation_areas": areas,
        "investigation_results": [],
        "execution_attempts": state.get("execution_attempts", 0),
    }


def route_investigations(state: IncidentState) -> list[Send]:
    """Conditionally fan out the three or four branches chosen by triage."""
    worker_base = {
        "incident_id": state.get("incident_id", "unknown"),
        "service": state.get("service", "unknown"),
        "description": state["description"],
        "severity": state.get("severity", "medium"),
        "triage": state["triage"],
    }
    return [
        Send("investigate_incident", {**worker_base, "investigation_area": area})
        for area in state["investigation_areas"]
    ]


async def investigate_incident(
    state: IncidentWorkerState, *, adapter: IncidentAdapter
) -> dict:
    area = state["investigation_area"]
    try:
        result = _as_model(InvestigationResult, await adapter.investigate(area, state))
        if result.area != area:
            result = result.model_copy(update={"area": area})
    except Exception as exc:
        result = InvestigationResult(
            area=area,
            status="failed",
            summary=f"The {area} investigation could not be completed.",
            error=f"{type(exc).__name__}: {exc}",
        )
    return {"investigation_results": [result]}


async def diagnose_incident(state: IncidentState, *, adapter: IncidentAdapter) -> dict:
    result = _as_model(DiagnosisResult, await adapter.diagnose(state))
    return {"diagnosis": result}


async def plan_incident_remediation(
    state: IncidentState, *, adapter: IncidentAdapter
) -> dict:
    result = _as_model(RemediationPlan, await adapter.plan_remediation(state))
    # The workflow owns revision identity.  An adapter cannot accidentally
    # reuse the approved revision after a failed execution.
    revision = state.get("execution_attempts", 0)
    result = result.model_copy(update={"revision": revision})
    return {
        "remediation_plan": result,
        "approved_plan_revision": None,
        "approval_status": "pending",
    }


async def assess_remediation_risk(
    state: IncidentState, *, adapter: IncidentAdapter
) -> dict:
    result = _as_model(RiskAssessment, await adapter.assess_risk(state))
    # Approval policy is enforced by orchestration, not delegated to an
    # adapter: high/critical risk always pauses.
    requires_approval = result.requires_approval or result.risk_level in {"high", "critical"}
    result = result.model_copy(update={"requires_approval": requires_approval})
    return {
        "risk_assessment": result,
        "approval_status": "pending" if requires_approval else "not_required",
        "approved_plan_revision": None,
    }


def route_after_risk(state: IncidentState) -> Literal["request_approval", "execute_remediation"]:
    return (
        "request_approval"
        if state["risk_assessment"].requires_approval
        else "execute_remediation"
    )


def _approval_granted(decision: Any) -> bool:
    if isinstance(decision, bool):
        return decision
    if isinstance(decision, str):
        return decision.strip().lower() in {"approve", "approved", "yes"}
    if isinstance(decision, dict):
        value = decision.get("approved", decision.get("decision"))
        return _approval_granted(value)
    return False


async def request_approval(state: IncidentState) -> dict:
    plan = state["remediation_plan"]
    risk = state["risk_assessment"]
    decision = interrupt(
        {
            "kind": "remediation_approval",
            "incident_id": state.get("incident_id", "unknown"),
            "plan_revision": plan.revision,
            "plan": plan.model_dump(mode="json"),
            "risk": risk.model_dump(mode="json"),
            "message": "Approve this simulated high-risk remediation plan?",
        }
    )
    if _approval_granted(decision):
        return {
            "approval_status": "approved",
            "approved_plan_revision": plan.revision,
        }
    return {"approval_status": "rejected", "approved_plan_revision": None}


def route_after_approval(
    state: IncidentState,
) -> Literal["execute_remediation", "finalize_incident"]:
    plan = state["remediation_plan"]
    approved = (
        state.get("approval_status") == "approved"
        and state.get("approved_plan_revision") == plan.revision
    )
    return "execute_remediation" if approved else "finalize_incident"


async def execute_remediation(state: IncidentState, *, adapter: IncidentAdapter) -> dict:
    attempt = state.get("execution_attempts", 0) + 1
    risk = state["risk_assessment"]
    plan = state["remediation_plan"]
    if risk.requires_approval and (
        state.get("approval_status") != "approved"
        or state.get("approved_plan_revision") != plan.revision
    ):
        # Defence in depth for direct node calls or future graph edits.
        result = ExecutionResult(
            attempt=attempt,
            status="failed",
            summary="Simulation blocked because this plan revision was not approved.",
            error="approval_required",
        )
        return {"execution_result": result, "execution_attempts": attempt}

    try:
        result = _as_model(ExecutionResult, await adapter.execute(state))
        result = result.model_copy(update={"attempt": attempt, "simulated": True})
    except Exception as exc:
        result = ExecutionResult(
            attempt=attempt,
            status="failed",
            summary=f"Simulated execution attempt {attempt} failed gracefully.",
            simulated=True,
            error=f"{type(exc).__name__}: {exc}",
        )
    return {"execution_result": result, "execution_attempts": attempt}


async def verify_remediation(state: IncidentState, *, adapter: IncidentAdapter) -> dict:
    execution = state.get("execution_result")
    if execution is None or execution.status == "failed":
        return {
            "verification_result": VerificationResult(
                status="failed",
                summary="Verification cannot pass because simulated execution failed.",
            )
        }
    try:
        result = _as_model(VerificationResult, await adapter.verify(state))
    except Exception as exc:
        result = VerificationResult(
            status="failed",
            summary=f"Verification failed gracefully: {type(exc).__name__}: {exc}",
        )
    return {"verification_result": result}


def route_after_verification(
    state: IncidentState,
) -> Literal["plan_remediation", "finalize_incident"]:
    if (
        state["verification_result"].status == "failed"
        and state.get("execution_attempts", 0) < MAX_EXECUTION_ATTEMPTS
    ):
        return "plan_remediation"
    return "finalize_incident"


async def finalize_incident(state: IncidentState) -> dict:
    """Create a deterministic report without another adapter/model call."""
    approval_status = state.get("approval_status")
    verification = state.get("verification_result")
    if approval_status == "rejected":
        status: Literal["resolved", "unresolved", "approval_rejected"] = "approval_rejected"
        summary = "High-risk simulated remediation was not approved; no execution occurred."
    elif verification is not None and verification.status == "passed":
        status = "resolved"
        summary = verification.summary
    else:
        status = "unresolved"
        execution = state.get("execution_result")
        summary = (
            execution.summary
            if execution is not None
            else "Incident was not remediated."
        )
    plan = state.get("remediation_plan")
    report = IncidentReport(
        incident_id=state.get("incident_id", "unknown"),
        status=status,
        summary=summary,
        execution_attempts=state.get("execution_attempts", 0),
        plan_revision=plan.revision if plan is not None else None,
        simulated=True,
    )
    return {"final_report": report}


def build_incident_graph(adapter: IncidentAdapter | None = None) -> StateGraph:
    """Build, but do not compile, the isolated incident workflow."""
    selected_adapter = adapter or DeterministicIncidentAdapter()
    graph = StateGraph(IncidentState)

    async def triage_node(state: IncidentState) -> dict:
        return await triage(state, adapter=selected_adapter)

    async def investigate_node(state: IncidentWorkerState) -> dict:
        return await investigate_incident(state, adapter=selected_adapter)

    async def diagnose_node(state: IncidentState) -> dict:
        return await diagnose_incident(state, adapter=selected_adapter)

    async def plan_node(state: IncidentState) -> dict:
        return await plan_incident_remediation(state, adapter=selected_adapter)

    async def risk_node(state: IncidentState) -> dict:
        return await assess_remediation_risk(state, adapter=selected_adapter)

    async def execute_node(state: IncidentState) -> dict:
        return await execute_remediation(state, adapter=selected_adapter)

    async def verify_node(state: IncidentState) -> dict:
        return await verify_remediation(state, adapter=selected_adapter)

    graph.add_node("triage", triage_node)
    graph.add_node("investigate_incident", investigate_node)
    graph.add_node("diagnose", diagnose_node)
    graph.add_node("plan_remediation", plan_node)
    graph.add_node("assess_risk", risk_node)
    graph.add_node("request_approval", request_approval)
    graph.add_node("execute_remediation", execute_node)
    graph.add_node("verify_remediation", verify_node)
    graph.add_node("finalize_incident", finalize_incident)

    graph.add_edge(START, "triage")
    graph.add_conditional_edges("triage", route_investigations, ["investigate_incident"])
    graph.add_edge("investigate_incident", "diagnose")
    graph.add_edge("diagnose", "plan_remediation")
    graph.add_edge("plan_remediation", "assess_risk")
    graph.add_conditional_edges(
        "assess_risk", route_after_risk, ["request_approval", "execute_remediation"]
    )
    graph.add_conditional_edges(
        "request_approval", route_after_approval, ["execute_remediation", "finalize_incident"]
    )
    graph.add_edge("execute_remediation", "verify_remediation")
    graph.add_conditional_edges(
        "verify_remediation", route_after_verification, ["plan_remediation", "finalize_incident"]
    )
    graph.add_edge("finalize_incident", END)
    return graph


def create_incident_app(
    adapter: IncidentAdapter | None = None, *, checkpointer: Any | None = None
):
    """Compile an incident app; a fresh in-memory saver enables HITL by default."""
    saver = checkpointer if checkpointer is not None else InMemorySaver()
    return build_incident_graph(adapter=adapter).compile(checkpointer=saver)
