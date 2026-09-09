"""The AI-Powered IT Incident Resolution workflow.

A stateful, multi-pattern LangGraph workflow, not a prompt with a model behind
it. Of the patterns the handout lists, this graph demonstrates six:

    sequential            triage -> investigate -> diagnose -> plan -> assess
                          -> execute -> verify -> finalize
    conditional routing   route_after_risk, route_after_approval,
                          route_after_verification
    parallel fan-out      route_investigations dispatches one Send per area
    tool calling          through the adapter, so the graph stays testable
                          without tools and the tools stay usable without it
    human-in-the-loop     request_approval raises interrupt() for high risk
    loop / replanning     failed verification returns to planning, bounded

Specialized-node collaboration is the seventh: each node owns one step and they
collaborate through state rather than through a chat transcript.

Environment access goes through `IncidentAdapter`. `DeterministicIncidentAdapter`
implements it with no I/O for tests; `hackathon1.adapters.LiveIncidentAdapter`
implements it against the real tools and the LLM.

The CodeHub support-ticket graph that used to live here -- billing, tech and
account specialists handling refunds and SLA lookups -- has been removed. It was
a placeholder from before the business case landed, and the requirements
document mentions billing, refunds, SLAs and support tickets exactly zero times.
Its one genuinely useful behaviour, uploading the resolution report to object
storage, now happens in finalize_incident.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, Literal, Protocol, TypedDict, TypeVar, runtime_checkable

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send, interrupt
from pydantic import BaseModel, Field

from hackathon1.storage import upload_report

#: Bound on remediation attempts before the workflow gives up and reports the
#: incident unresolved. Without a cap, a plan that never satisfies verification
#: would replan forever.
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
    #: Object key of the stored report, or None when storage is unavailable.
    #: Best-effort by design -- see storage.upload_report.
    report_key: str | None = None
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
    incident_id = state.get("incident_id", "unknown")

    # Archive the full narrative to object storage. Best-effort: upload_report
    # swallows and logs any failure, because losing the archive copy must not
    # fail an incident that was otherwise resolved.
    report_key = upload_report(incident_id, _render_report(state, status, summary))

    report = IncidentReport(
        incident_id=incident_id,
        status=status,
        summary=summary,
        execution_attempts=state.get("execution_attempts", 0),
        plan_revision=plan.revision if plan is not None else None,
        simulated=True,
        report_key=report_key,
    )
    return {"final_report": report}


def _render_report(state: IncidentState, status: str, summary: str) -> str:
    """The human-readable incident record that gets archived.

    Deliberately assembled from state rather than generated: this is the audit
    trail, so it has to say what actually happened, not what a model would
    narrate about it.
    """
    # One list entry per line, joined once at the end. Headings and their bodies
    # are separate entries rather than embedded newlines, so every entry stays a
    # single line and the structure is obvious.
    lines = [
        f"# Incident {state.get('incident_id', 'unknown')}",
        f"Service: {state.get('service', 'unknown')}",
        f"Severity: {state.get('severity', 'unknown')}",
        f"Status: {status}",
        "",
        "## Summary",
        summary,
    ]

    diagnosis = state.get("diagnosis")
    if diagnosis is not None:
        lines += [
            "",
            f"## Root cause (confidence {diagnosis.confidence:.2f})",
            diagnosis.root_cause,
        ]
        if diagnosis.contributing_factors:
            lines += ["", "Contributing factors:"]
            lines += [f"- {factor}" for factor in diagnosis.contributing_factors]

    results = state.get("investigation_results", [])
    if results:
        lines += ["", "## Evidence"]
        for item in results:
            lines += ["", f"### {item.area} ({item.status})", item.summary]
            lines += [f"- {line}" for line in item.evidence]
            if item.error:
                lines.append(f"- error: {item.error}")

    plan_obj = state.get("remediation_plan")
    if plan_obj is not None:
        lines += ["", f"## Remediation (revision {plan_obj.revision})", plan_obj.summary]
        lines += [f"- {step}" for step in plan_obj.steps]

    risk = state.get("risk_assessment")
    if risk is not None:
        lines += [
            "",
            f"## Risk: {risk.risk_level} (approval required: {risk.requires_approval})",
        ]
        lines += [f"- {reason}" for reason in risk.reasons]

    lines += ["", "## Approval", str(state.get("approval_status", "not_required"))]

    verification = state.get("verification_result")
    if verification is not None:
        lines += ["", f"## Verification: {verification.status}", verification.summary]

    return "\n".join(lines)


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
