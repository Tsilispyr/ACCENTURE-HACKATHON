"""Data contracts for the AI-Powered IT Incident Resolution Agent.

This module is the boundary between the two halves of the build: the graph
owns the workflow, this module owns the shapes that travel through it. It
imports nothing from world.py or tools.py on purpose -- the graph author can
`from hackathon1.models import Incident, IncidentReport` without pulling in
the simulated enterprise systems, and the mock world can be swapped or reset
without touching the contract.

Coverage of the handout:

* ``Incident``        -- FR01, the structured input.
* Evidence models     -- FR06, one per investigation tool's return shape.
* ``Diagnosis``       -- FR07, root cause tied to the evidence that supports it.
* ``RemediationPlan`` -- FR08, actions plus the risk level that drives FR10.
* ``ActionResult``    -- FR11, the outcome of a simulated remediation.
* ``VerificationResult`` -- FR12/FR13, did it work and which attempt was this.
* ``IncidentReport``  -- FR14, exactly the bullet list in handout section 2.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Vocabularies
# ---------------------------------------------------------------------------

Severity = Literal["critical", "high", "medium", "low", "unknown"]
RiskLevel = Literal["none", "low", "medium", "high"]
IncidentCategory = Literal[
    "application",
    "authentication",
    "database",
    "infrastructure",
    "payment",
    "unknown",
]
IncidentStatus = Literal[
    "received",
    "triaged",
    "investigating",
    "diagnosed",
    "awaiting_approval",
    "executing",
    "verifying",
    "resolved",
    "failed",
    "closed",
]
HealthStatus = Literal["healthy", "degraded", "down", "unknown"]


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# FR01 -- structured incident input
# ---------------------------------------------------------------------------

class Incident(BaseModel):
    """A single IT incident as it arrives from a user or a monitoring system.

    `severity` and `category` start as "unknown" and are filled in by the
    triage node (FR03). Nothing else in this model is derived -- everything
    the workflow produces lives in IncidentReport instead, so the input a
    judge posts to /incidents and the record the graph builds stay separable.
    """

    incident_id: str = Field(description="e.g. INC-1042")
    service: str = Field(description="affected service, e.g. payment-service")
    description: str = Field(description="free-text symptom report")

    severity: Severity = "unknown"
    category: IncidentCategory = "unknown"

    reported_at: datetime = Field(default_factory=_utc_now)
    source: Literal["user", "monitoring", "automated"] = "user"
    symptoms: list[str] = Field(
        default_factory=list, description="individual observations pulled out of the description"
    )
    affected_users: int | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def from_text(cls, text: str, **overrides: Any) -> "Incident":
        """Parse the handout's plain-text incident block (section 2).

            Incident ID: INC-1042
            Service: payment-service
            Severity: Unknown
            Description: Customers report payment failures ...

        Anything the block does not carry falls back to a default, so a
        hidden scenario in a slightly different shape still parses instead of
        raising. Unlabelled leftover lines become `symptoms`.
        """
        fields: dict[str, str] = {}
        leftovers: list[str] = []
        for raw_line in text.splitlines():
            line = raw_line.strip().lstrip("-*• ").strip()
            if not line:
                continue
            match = re.match(r"^([A-Za-z][A-Za-z ]{1,30}?)\s*[:\-]\s*(.+)$", line)
            if match:
                fields[match.group(1).strip().lower().replace(" ", "_")] = match.group(2).strip()
            else:
                leftovers.append(line)

        severity = (fields.get("severity") or "unknown").lower()
        if severity not in ("critical", "high", "medium", "low"):
            severity = "unknown"

        description = fields.get("description") or text.strip()
        # "Error: Database connection timeout" and any unlabelled line are
        # symptoms, not metadata -- they are the strongest triage signal.
        symptoms = [v for k, v in fields.items() if k in ("error", "symptom", "symptoms")]
        symptoms.extend(leftovers)

        data: dict[str, Any] = {
            "incident_id": fields.get("incident_id") or fields.get("id") or "INC-UNKNOWN",
            "service": fields.get("service") or "unknown-service",
            "description": description,
            "severity": severity,
            "symptoms": symptoms,
        }
        data.update(overrides)
        return cls(**data)

    def signal_text(self) -> str:
        """Description plus symptoms, for keyword search against the KB."""
        return " ".join([self.description, *self.symptoms])


# ---------------------------------------------------------------------------
# FR06 -- evidence returned by the investigation tools
# ---------------------------------------------------------------------------

class LogEntry(BaseModel):
    timestamp: str
    level: Literal["ERROR", "WARN", "INFO", "DEBUG"]
    service: str
    message: str
    count: int = Field(default=1, description="occurrences of this message in the window")


class MetricReading(BaseModel):
    """One metric with its own baseline attached.

    Current value alone is not evidence -- "cpu 44%" means nothing until you
    can see it against a 41% baseline. Carrying baseline and deviation on the
    reading is what lets the diagnosis node rule CPU *out* for scenario C
    instead of guessing.
    """

    name: str
    current: float
    baseline: float
    unit: str
    deviation_pct: float
    breached: bool = Field(description="True when the deviation exceeds this metric's tolerance")


class ServiceMetrics(BaseModel):
    service: str
    window_minutes: int
    collected_at: str
    readings: list[MetricReading]

    def breached(self) -> list[MetricReading]:
        return [r for r in self.readings if r.breached]


class RunbookEntry(BaseModel):
    id: str
    title: str
    symptoms: list[str]
    probable_cause: str
    recommended_actions: list[str]
    relevance: float = Field(description="0-1 keyword overlap with the query")


class PastIncident(BaseModel):
    incident_id: str
    service: str
    occurred_at: str
    summary: str
    root_cause: str
    resolution_action: str
    time_to_resolve_minutes: int


class ChangeEvent(BaseModel):
    """A deployment or config change. Scenario B has no log or metric that
    points at its root cause -- only this. Without a change feed the agent
    has to guess, which is the "plausible LLM answer" failure the handout
    rejects in section 15."""

    change_id: str
    service: str
    type: Literal["deployment", "config", "infrastructure"]
    description: str
    occurred_at: str
    author: str
    rollback_available: bool


class Evidence(BaseModel):
    """Everything the investigation phase collected, including what it failed
    to collect. `gaps` is how FR15 stays visible: a tool that blew up
    degrades the evidence instead of killing the run."""

    logs: list[LogEntry] = Field(default_factory=list)
    metrics: ServiceMetrics | None = None
    runbooks: list[RunbookEntry] = Field(default_factory=list)
    history: list[PastIncident] = Field(default_factory=list)
    changes: list[ChangeEvent] = Field(default_factory=list)
    gaps: list[str] = Field(
        default_factory=list, description="tools that failed or returned nothing usable"
    )


# ---------------------------------------------------------------------------
# FR07/FR08 -- diagnosis and remediation planning
# ---------------------------------------------------------------------------

class Diagnosis(BaseModel):
    root_cause: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(
        description="short quotes or metric names the root cause rests on"
    )
    ruled_out: list[str] = Field(default_factory=list)


class RemediationAction(BaseModel):
    action: str = Field(description="tool name, e.g. restart_service")
    params: dict[str, Any] = Field(default_factory=dict)
    risk: RiskLevel
    rationale: str
    requires_approval: bool = Field(
        description="from tools.requires_approval: a static floor the LLM can raise but never lower"
    )


class RemediationPlan(BaseModel):
    actions: list[RemediationAction]
    overall_risk: RiskLevel
    rollback_note: str | None = None


# ---------------------------------------------------------------------------
# FR10-FR13 -- approval, execution, verification
# ---------------------------------------------------------------------------

class ApprovalRecord(BaseModel):
    required: bool
    status: Literal["not_required", "pending", "approved", "rejected"] = "not_required"
    approver: str | None = None
    decided_at: datetime | None = None
    comment: str | None = None


class ActionResult(BaseModel):
    """Outcome of one simulated remediation.

    Deliberately does *not* say whether the incident is fixed. The world
    knows, but reporting it here would let the agent skip verification and
    still look correct -- FR12 only means something if recovery has to be
    observed through check_service_health.
    """

    action: str
    service: str
    params: dict[str, Any] = Field(default_factory=dict)
    status: Literal["success", "failed", "rejected"]
    message: str
    executed_at: str
    simulated: bool = True


class HealthReport(BaseModel):
    service: str
    status: HealthStatus
    healthy: bool
    checks: list[MetricReading] = Field(default_factory=list)
    summary: str


class VerificationResult(BaseModel):
    resolved: bool
    attempt: int = Field(description="1 for the first pass, incremented on every replan")
    health: HealthReport | None = None
    notes: str = ""


# ---------------------------------------------------------------------------
# FR14 -- the final structured report (handout section 2's bullet list)
# ---------------------------------------------------------------------------

class IncidentReport(BaseModel):
    incident: Incident
    status: IncidentStatus = "received"

    evidence: Evidence = Field(default_factory=Evidence)
    diagnosis: Diagnosis | None = None
    plan: RemediationPlan | None = None
    approval: ApprovalRecord = Field(default_factory=lambda: ApprovalRecord(required=False))
    executions: list[ActionResult] = Field(default_factory=list)
    verification: VerificationResult | None = None

    attempts: int = 0
    summary: str = ""
    completed_at: datetime | None = None
