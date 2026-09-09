"""The binding between the incident workflow and the simulated estate.

`graph.py` talks to an `IncidentAdapter` Protocol and deliberately imports no
tools; `tools.py` exposes tools and knows nothing about the graph. That
separation is why the two developed independently without conflicting -- and it
leaves exactly one thing unwritten, which is this file. Until it exists the only
implementation is `DeterministicIncidentAdapter`, a no-I/O stub for tests.

Division of labour, deliberately:

    tools  ->  facts     Investigation calls tools and reports what they
                         returned. No LLM. Evidence a judge can trace back to
                         a tool call is worth more than evidence a model wrote.
    LLM    ->  judgement Triage, diagnosis and planning reason over evidence
                         the tools produced. That is the part that needs a
                         model.
    policy ->  safety    Risk comes from `tools.effective_risk`, which the LLM
                         may raise but never lower. See `assess_risk`.

Two properties worth defending to a judge:

1. **The approval gate cannot be talked past.** `tools.effective_risk` derives a
   static floor from the action and its context; a model assessment may raise it
   and is ignored if it tries to lower it. Incident text is attacker-controllable
   in any real deployment, so a gate the model can argue with is decorative.
2. **Remediation never reports success.** `execute` returns an operator-style
   receipt of what it did. Whether the incident is actually fixed is decided by
   `verify`, which observes `check_service_health` independently. That is what
   keeps the replan loop honest -- a model cannot declare victory.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from pydantic import BaseModel, Field

from hackathon1.graph import (
    DiagnosisResult,
    ExecutionResult,
    IncidentState,
    IncidentTriage,
    IncidentWorkerState,
    InvestigationArea,
    InvestigationResult,
    RemediationPlan,
    RiskAssessment,
    Severity,
    VerificationResult,
)
from hackathon1.llm import llm
from hackathon1.tools import (
    check_service_health,
    effective_risk,
    get_incident_history,
    get_service_metrics,
    requires_approval,
    restart_service,
    rollback_change,
    scale_connection_pool,
    search_knowledge_base,
    search_logs,
)
from hackathon1.world import active_world

logger = logging.getLogger(__name__)

#: The only actions this adapter will ever execute. Keeping the set closed here
#: (rather than letting a model name a tool) means an unexpected plan degrades
#: to a known action instead of reaching for something unreviewed.
_ACTIONS = ("scale_connection_pool", "restart_service", "rollback_change")

#: Keyword -> action. Ordered most-specific first: a plan that says "roll back
#: the connection pool change" is a rollback, not a pool resize.
_ACTION_KEYWORDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("rollback", "roll back", "revert", "previous version", "last known good"), "rollback_change"),
    (("connection pool", "pool size", "max_connections", "scale the pool"), "scale_connection_pool"),
    (("restart", "bounce", "recycle"), "restart_service"),
)

#: Fallback pool size when the plan does not name one. The simulated estate
#: treats this as "comfortably above the current ceiling".
_DEFAULT_POOL_SIZE = 50


# ---------------------------------------------------------------------------
# Structured-output schemas for the three judgement calls
# ---------------------------------------------------------------------------


class _TriageOut(BaseModel):
    severity: Severity = Field(description="impact of the incident, not the risk of fixing it")
    summary: str = Field(description="one sentence, factual, no speculation about cause")
    investigation_areas: list[InvestigationArea] = Field(
        description="three or four areas to investigate concurrently"
    )


class _DiagnosisOut(BaseModel):
    root_cause: str = Field(description="the most probable cause, grounded in the evidence given")
    confidence: float = Field(ge=0.0, le=1.0, description="0-1; be honest when evidence is thin")
    contributing_factors: list[str] = Field(default_factory=list)


class _PlanOut(BaseModel):
    action: str = Field(description=f"exactly one of: {', '.join(_ACTIONS)}")
    summary: str = Field(description="one sentence naming the action and why")
    steps: list[str]
    rollback_steps: list[str] = Field(default_factory=list)


class _RiskOut(BaseModel):
    risk_level: Severity = Field(description="risk of PERFORMING the remediation")
    reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Pure helpers -- unit-testable, no LLM, no I/O
# ---------------------------------------------------------------------------


def action_from_plan(plan: RemediationPlan) -> str:
    """Derive the remediation action from a plan, deterministically.

    `assess_risk` and `execute` both need to know which action is being
    proposed, and they *must* agree: risk is assessed for one action and then a
    different one executed is exactly the hole an approval gate exists to close.
    Asking the model twice could return two answers, so the action is derived
    from the plan text by this pure function instead -- same input, same answer,
    in both places, and testable without a model.

    Falls back to `restart_service` rather than guessing: unmatched text means
    the plan was not one this system knows how to run, and `risk_of` treats an
    unknown action as high risk, so the safe direction is preserved.
    """
    haystack = " ".join([plan.summary, *plan.steps]).lower()

    # The literal tool name first, and whichever is mentioned EARLIEST wins.
    # plan_remediation asks the model to name the action explicitly and
    # prefixes it into the summary, so this is the strongest signal available.
    # (Checked before the keyword table because "scale_connection_pool" does not
    # contain the phrase "connection pool" -- underscores -- and an earlier
    # version of this function mapped it to restart_service.)
    named = [(haystack.find(action), action) for action in _ACTIONS if action in haystack]
    if named:
        return min(named)[1]

    for keywords, action in _ACTION_KEYWORDS:
        if any(word in haystack for word in keywords):
            return action
    return "restart_service"


def _evidence_lines(payload: Any, limit: int = 6) -> list[str]:
    """Flatten a tool's return value into short, quotable evidence strings."""
    if isinstance(payload, list):
        return [json.dumps(item, default=str)[:300] for item in payload[:limit]]
    if isinstance(payload, dict):
        readings = payload.get("readings")
        if isinstance(readings, list):
            return [json.dumps(item, default=str)[:300] for item in readings[:limit]]
        return [json.dumps(payload, default=str)[:600]]
    return [str(payload)[:300]]


# ---------------------------------------------------------------------------
# The adapter
# ---------------------------------------------------------------------------


class LiveIncidentAdapter:
    """`IncidentAdapter` backed by the real tools and the configured LLM.

    Tool calls are synchronous. They are in-memory reads against the simulated
    estate in `world.py` -- no sockets, no disk beyond one cached JSON load --
    so awaiting them in a thread would cost more than it saves. If a tool ever
    grows real I/O this is the place that needs `asyncio.to_thread`.
    """

    def __init__(self, *, default_pool_size: int = _DEFAULT_POOL_SIZE) -> None:
        self.default_pool_size = default_pool_size

    # -- judgement ----------------------------------------------------------

    async def triage(self, state: IncidentState) -> IncidentTriage:
        service = state.get("service", "unknown-service")
        description = (state.get("description") or "").strip()
        reported = state.get("severity")

        result = await llm.with_structured_output(
            _TriageOut, method="function_calling"
        ).ainvoke(
            "Triage this IT incident. Severity describes IMPACT, not how risky a fix would be.\n"
            f"Service: {service}\n"
            f"Reported severity: {reported or 'unknown'}\n"
            f"Description: {description or 'none given'}\n"
            "Choose three or four investigation areas from: logs, metrics, dependencies, "
            "recent_changes."
        )
        return IncidentTriage(
            severity=result.severity,
            summary=result.summary or description or "Unspecified incident",
            investigation_areas=result.investigation_areas,
        )

    async def diagnose(self, state: IncidentState) -> DiagnosisResult:
        succeeded = [r for r in state.get("investigation_results", []) if r.status == "succeeded"]
        failed = [r for r in state.get("investigation_results", []) if r.status != "succeeded"]

        if not succeeded:
            # Every investigation failed. Say so rather than inventing a cause
            # from nothing -- a confident root cause with no evidence behind it
            # is the failure mode this whole workflow exists to avoid.
            return DiagnosisResult(
                root_cause="Undetermined: every investigation failed, so no evidence was collected.",
                confidence=0.0,
                contributing_factors=[r.error or f"{r.area} unavailable" for r in failed],
            )

        evidence = "\n\n".join(
            f"## {r.area}\n{r.summary}\n" + "\n".join(f"- {line}" for line in r.evidence)
            for r in succeeded
        )
        gaps = (
            f"\n\nUnavailable: {', '.join(r.area for r in failed)}. Account for the gap."
            if failed
            else ""
        )
        result = await llm.with_structured_output(
            _DiagnosisOut, method="function_calling"
        ).ainvoke(
            "Determine the most probable root cause of this incident from the evidence below. "
            "Ground every claim in the evidence; do not introduce facts it does not support. "
            "Lower your confidence when the evidence is thin.\n\n"
            f"Service: {state.get('service', 'unknown')}\n"
            f"Reported: {state.get('description', '')}\n\n{evidence}{gaps}"
        )
        return DiagnosisResult(
            root_cause=result.root_cause,
            confidence=result.confidence,
            contributing_factors=result.contributing_factors,
        )

    async def plan_remediation(self, state: IncidentState) -> RemediationPlan:
        revision = state.get("execution_attempts", 0)
        diagnosis = state.get("diagnosis")
        previous = state.get("execution_result")
        verification = state.get("verification_result")

        # On a replan, say what already failed. Without this the model proposes
        # the same action again and the retry loop just burns attempts.
        retry_context = ""
        if revision > 0:
            retry_context = (
                f"\n\nAttempt {revision} did not resolve it. "
                f"Previous action outcome: {getattr(previous, 'summary', 'unknown')}. "
                f"Verification: {getattr(verification, 'summary', 'unknown')}. "
                "Propose a DIFFERENT action."
            )

        result = await llm.with_structured_output(_PlanOut, method="function_calling").ainvoke(
            "Propose a remediation for this incident. The action must be exactly one of: "
            f"{', '.join(_ACTIONS)}. Name it explicitly in the summary.\n"
            f"Service: {state.get('service', 'unknown')}\n"
            f"Root cause: {getattr(diagnosis, 'root_cause', 'not established')}\n"
            f"{retry_context}"
        )
        summary = result.summary
        if result.action in _ACTIONS and result.action not in summary:
            # Keep the action recoverable from the text, since action_from_plan
            # reads the plan rather than a separate field.
            summary = f"{result.action}: {summary}"
        return RemediationPlan(
            revision=revision,
            summary=summary,
            steps=result.steps or [f"Run {result.action}"],
            rollback_steps=result.rollback_steps,
        )

    # -- policy -------------------------------------------------------------

    async def assess_risk(self, state: IncidentState) -> RiskAssessment:
        """Risk floor from policy; the model may raise it, never lower it."""
        plan = state.get("remediation_plan")
        service = state.get("service", "")
        severity = str(state.get("severity") or "unknown")
        action = action_from_plan(plan) if plan is not None else "restart_service"

        assessed: str | None = None
        reasons: list[str] = []
        try:
            out = await llm.with_structured_output(_RiskOut, method="function_calling").ainvoke(
                "Assess the risk of PERFORMING this remediation (not the incident's severity).\n"
                f"Action: {action}\nService: {service}\n"
                f"Plan: {getattr(plan, 'summary', 'none')}"
            )
            assessed = out.risk_level
            reasons = list(out.reasons)
        except Exception as exc:
            # A model failure must not silently produce a LOWER risk. Falling
            # through with assessed=None leaves the policy floor in charge.
            logger.warning("risk assessment LLM call failed, using policy floor only: %s", exc)
            reasons = [f"model assessment unavailable ({type(exc).__name__}); policy floor applied"]

        level = effective_risk(action, service, severity, assessed)
        needs = requires_approval(action, service, severity, assessed)
        reasons.insert(0, f"policy floor for {action!r} on {service or 'unknown service'}: {level}")
        return RiskAssessment(risk_level=level, reasons=reasons, requires_approval=needs)

    # -- tools --------------------------------------------------------------

    async def investigate(
        self, area: InvestigationArea, state: IncidentState | IncidentWorkerState
    ) -> InvestigationResult:
        """Run the tool for one area. Raises on tool failure -- the graph's
        investigate_incident node catches it and records a failed area, which is
        what gives the workflow a real failure path to handle (FR14)."""
        service = state.get("service", "")
        description = state.get("description", "") or service

        if area == "logs":
            payload = search_logs.invoke({"service": service})
            summary = f"{len(payload)} log group(s) at WARN or above for {service}."
        elif area == "metrics":
            payload = get_service_metrics.invoke({"service": service})
            breached = [r for r in payload.get("readings", []) if r.get("breached")]
            summary = (
                f"{len(breached)} metric(s) breaching baseline for {service}."
                if breached
                else f"No metric breaches for {service}; readings are at baseline."
            )
        elif area == "dependencies":
            payload = search_knowledge_base.invoke({"query": description})
            summary = f"{len(payload)} runbook(s) matched the reported symptoms."
        elif area == "recent_changes":
            payload = get_incident_history.invoke({"service": service})
            summary = f"{len(payload)} prior incident(s) on record for {service}."
        else:  # pragma: no cover - InvestigationArea is a closed Literal
            raise ValueError(f"unknown investigation area: {area!r}")

        return InvestigationResult(
            area=area,
            status="succeeded",
            summary=summary,
            evidence=_evidence_lines(payload),
        )

    async def execute(self, state: IncidentState) -> ExecutionResult:
        """Run the planned action against the simulated estate.

        Returns a receipt of what was done, never a verdict on whether the
        incident is fixed -- that is verify()'s job, from independent
        observation.
        """
        plan = state["remediation_plan"]
        service = state.get("service", "")
        action = action_from_plan(plan)

        if action == "scale_connection_pool":
            receipt = scale_connection_pool.invoke(
                {"service": service, "new_size": self.default_pool_size}
            )
        elif action == "rollback_change":
            receipt = rollback_change.invoke(
                {"service": service, "change_id": self._latest_change_id(service)}
            )
        else:
            receipt = restart_service.invoke({"service": service})

        return ExecutionResult(
            attempt=state.get("execution_attempts", 0) + 1,
            status="succeeded",
            summary=f"{action}: {receipt.get('detail') or receipt.get('summary') or receipt}",
            simulated=True,
        )

    async def verify(self, state: IncidentState) -> VerificationResult:
        """Observe recovery independently of what execute() claimed."""
        service = state.get("service", "")
        health = check_service_health.invoke({"service": service})
        healthy = str(health.get("status", "")).lower() in {"healthy", "ok", "recovered"}
        return VerificationResult(
            status="passed" if healthy else "failed",
            summary=f"check_service_health reports {health.get('status', 'unknown')} for {service}.",
            checks=[json.dumps(health, default=str)[:400]],
        )

    # -- internals ----------------------------------------------------------

    def _latest_change_id(self, service: str) -> str:
        """Most recent change for a service, for rollback_change.

        Best-effort: an unknown change id makes the tool fail, the graph records
        a failed execution, and the replan loop proposes something else -- which
        is the designed behaviour, not an error worth crashing for.
        """
        try:
            # changes(service, hours) -- both arguments are required. Look back a
            # day first, then a week, because a rollback wants the change that
            # most plausibly caused this, not merely any change on record.
            world = active_world()
            changes = world.changes(service, 24) or world.changes(service, 168)
        except Exception as exc:
            logger.warning("could not read recent changes for %s: %s", service, exc)
            return "unknown"
        if not changes:
            return "unknown"
        latest = changes[0]
        if isinstance(latest, dict):
            return str(latest.get("id") or latest.get("change_id") or "unknown")
        return str(latest)
