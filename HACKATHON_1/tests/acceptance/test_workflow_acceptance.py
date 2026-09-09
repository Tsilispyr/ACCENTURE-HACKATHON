"""Acceptance tests for the incident workflow.

These were placeholders until the tool contracts, approval resume, persistence
and report shape existed. Each keeps the constraint its placeholder set: real
graph, real tools, real evidence, and assertions about calls that actually
happened rather than about a state someone prefilled.
"""

from __future__ import annotations

import pytest
from langgraph.types import Command

# Private in adapters because nothing outside it should construct these; tests
# are the exception, since scripting them is how the run stays deterministic.
from hackathon1.adapters import _DiagnosisOut, _PlanOut, _RiskOut, _TriageOut
from hackathon1.graph import MAX_EXECUTION_ATTEMPTS
from hackathon1.world import ToolUnavailableError

pytestmark = pytest.mark.acceptance

# Service choice is load-bearing here, so it is spelled out. Each scenario in the
# estate has a different correct remediation, and the risk policy treats them
# differently too:
#
#   payment-service    scale is PARTIAL, restart is FULL but requires scale first.
#                      Not on the low-impact allowlist, so a restart is HIGH risk
#                      and must pass the approval gate.
#   order-service      restart alone is a FULL fix, scale is partial. On the
#                      allowlist, so a restart is MEDIUM -- executes unattended.
#   reporting-service  nothing works. On the allowlist, so the workflow can loop
#                      to the retry bound without ever stopping for approval.
CRITICAL = "payment-service"
LOW_IMPACT = "order-service"
UNFIXABLE = "reporting-service"


def triage(*areas, severity="high"):
    return _TriageOut(severity=severity, summary="Incident under investigation",
                      investigation_areas=list(areas))


def plan(action, revision_note="", steps=None):
    return _PlanOut(action=action, summary=f"{action}: {revision_note or action}",
                    steps=steps or [f"Run {action}"], rollback_steps=[])


# ---------------------------------------------------------------------------
# FR03, FR05-FR07 -- evidence actually flows from tools into the diagnosis
# ---------------------------------------------------------------------------


async def test_diagnosis_uses_collected_tool_evidence(run_incident, scripted_llm, tool_spy):
    scripted_llm.set(_TriageOut, triage("logs", "dependencies", "recent_changes"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(
        root_cause="Connection pool exhausted", confidence=0.9,
        contributing_factors=["recent change"]))
    scripted_llm.set(_PlanOut, plan("scale_connection_pool"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    result, _, _ = await run_incident(service=CRITICAL)

    # Three distinct meaningful tools were genuinely invoked (FR05's minimum).
    assert tool_spy.called("search_logs")
    assert tool_spy.called("search_knowledge_base")
    assert tool_spy.called("get_incident_history")

    # Evidence is retained on state, and is the tools' real output rather than
    # anything the model produced.
    areas = {r.area: r for r in result["investigation_results"]}
    assert set(areas) == {"logs", "dependencies", "recent_changes"}
    assert all(r.status == "succeeded" for r in areas.values())
    assert areas["logs"].evidence, "log evidence should carry the tool's actual records"

    # And the diagnosis was produced from that evidence, not seeded into state.
    assert result["diagnosis"].root_cause == "Connection pool exhausted"


# ---------------------------------------------------------------------------
# FR08, FR11-FR12 -- low risk executes, and recovery is verified independently
# ---------------------------------------------------------------------------


async def test_low_risk_action_is_executed_and_recovery_verified(
    run_incident, scripted_llm, tool_spy
):
    # restart on order-service: MEDIUM risk under the policy, so no approval, and
    # a full fix in the estate. No low-risk action fully resolves anything, which
    # is itself deliberate -- the cheap action is never the whole answer.
    scripted_llm.set(_TriageOut, triage("logs"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Stuck worker", confidence=0.9))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    result, _, _ = await run_incident(service=LOW_IMPACT)

    assert result["risk_assessment"].requires_approval is False
    assert result["approval_status"] == "not_required"
    assert tool_spy.called("restart_service")

    # Execution precedes verification -- recovery is never claimed from the
    # executor's own return value.
    names = tool_spy.names()
    assert names.index("restart_service") < names.index("check_service_health")

    # And the reported outcome follows the verification, not the execution.
    assert result["verification_result"].status == "passed"
    assert result["final_report"].status == "resolved"


# ---------------------------------------------------------------------------
# FR10 -- high risk cannot execute without approval
# ---------------------------------------------------------------------------


async def test_high_risk_action_does_not_execute_while_awaiting_approval(
    run_incident, scripted_llm, tool_spy
):
    scripted_llm.set(_TriageOut, triage("logs"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Needs restart", confidence=0.8))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="high", reasons=["restart"]))

    result, _, _ = await run_incident(service=CRITICAL)

    assert result["risk_assessment"].requires_approval is True
    assert result.get("__interrupt__"), "the run should be paused at the approval gate"
    # The whole point: nothing was executed while waiting.
    assert not tool_spy.called("restart_service")
    assert result.get("execution_result") is None


async def test_rejected_approval_never_executes_the_action(
    run_incident, scripted_llm, tool_spy
):
    scripted_llm.set(_TriageOut, triage("logs"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Needs restart", confidence=0.8))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="high", reasons=["restart"]))

    _, app, config = await run_incident(service=CRITICAL)
    health_before = [c for c in tool_spy.calls if c.name == "check_service_health"]

    # Reject through the real resume mechanism, not by editing state.
    result = await app.ainvoke(Command(resume={"approved": False}), config=config)

    assert result["approval_status"] == "rejected"
    assert not tool_spy.called("restart_service")
    assert result["final_report"].status == "approval_rejected"
    # The estate was not touched: no verification ran, so nothing observed it change.
    assert len([c for c in tool_spy.calls if c.name == "check_service_health"]) == len(health_before)


# ---------------------------------------------------------------------------
# FR02, FR10-FR12 -- approval resumes the same incident
# ---------------------------------------------------------------------------


async def test_approved_action_resumes_the_same_incident(
    run_incident, scripted_llm, tool_spy
):
    scripted_llm.set(_TriageOut, triage("logs", "metrics"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Needs restart", confidence=0.8))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="high", reasons=["restart"]))

    paused, app, config = await run_incident(incident_id="INC-RESUME", service=CRITICAL)
    evidence_before = list(paused["investigation_results"])
    assert not tool_spy.called("restart_service")

    result = await app.ainvoke(Command(resume={"approved": True}), config=config)

    # Same incident, same evidence -- not a fresh run.
    assert result["incident_id"] == "INC-RESUME"
    assert [r.area for r in result["investigation_results"]] == [r.area for r in evidence_before]
    # Executed only after approval, and verification followed.
    assert tool_spy.called("restart_service")
    assert result.get("verification_result") is not None

    # A restart alone does not fix payment-service (it needs the pool scaled
    # first), so verification fails and the workflow replans. The revised plan is
    # high risk too -- and it pauses for approval AGAIN rather than riding on the
    # earlier one. Approving a plan authorises that plan, not the incident.
    assert result["approval_status"] == "pending"
    assert result.get("__interrupt__"), "the replanned high-risk action must be re-approved"


# ---------------------------------------------------------------------------
# FR12-FR13 -- failed verification replans, and recovery follows the sequence
# ---------------------------------------------------------------------------


async def test_failed_verification_triggers_replanning_before_recovery(
    run_incident, scripted_llm, tool_spy
):
    """The estate requires scale THEN restart; a restart alone does nothing.

    So the first plan genuinely fails verification -- no scripted failure needed,
    the simulation supplies it -- and the revised plan is what recovers.
    """
    scripted_llm.set(_TriageOut, triage("logs"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Pool exhausted", confidence=0.9))
    scripted_llm.set(_PlanOut,
                     plan("scale_connection_pool", "raise the ceiling"),
                     plan("restart_service", "apply the new ceiling"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    result, app, config = await run_incident(service=LOW_IMPACT)

    # First attempt executed and was verified as NOT recovered.
    assert tool_spy.count("check_service_health") >= 2, "verification ran after each attempt"
    assert result["execution_attempts"] >= 2, "a replan happened"
    assert tool_spy.called("scale_connection_pool")
    assert tool_spy.called("restart_service")

    # Order proves replanning rather than a single batched execution.
    names = tool_spy.names()
    assert names.index("scale_connection_pool") < names.index("check_service_health")
    assert names.index("restart_service") > names.index("check_service_health")

    # Recovery claimed only after a passing verification.
    assert result["verification_result"].status == "passed"
    assert result["final_report"].status == "resolved"


# ---------------------------------------------------------------------------
# Bounded retry
# ---------------------------------------------------------------------------


async def test_persistent_verification_failure_stops_at_the_agreed_bound(
    run_incident, scripted_llm, tool_spy
):
    """Never recovers: no remediation in the estate fixes this service."""
    scripted_llm.set(_TriageOut, triage("logs"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Unknown", confidence=0.3))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    result, _, _ = await run_incident(service=UNFIXABLE)

    # Read the bound from the implementation rather than hard-coding a guess.
    assert result["execution_attempts"] == MAX_EXECUTION_ATTEMPTS
    assert result["verification_result"].status == "failed"
    assert result["final_report"].status == "unresolved"
    # No fabricated healthy result anywhere.
    assert result["final_report"].simulated is True


# ---------------------------------------------------------------------------
# FR15 -- a tool failure does not crash the workflow
# ---------------------------------------------------------------------------


async def test_investigation_tool_failure_is_handled_without_crashing(
    run_incident, scripted_llm, tool_spy
):
    scripted_llm.set(_TriageOut, triage("logs", "metrics", "dependencies"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(root_cause="Pool exhausted", confidence=0.6))
    scripted_llm.set(_PlanOut, plan("scale_connection_pool"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    # Fail at the real tool boundary, not by stubbing the adapter.
    tool_spy.fail("get_service_metrics", ToolUnavailableError("metrics backend down"))

    result, _, _ = await run_incident(service=LOW_IMPACT)

    by_area = {r.area: r for r in result["investigation_results"]}
    assert by_area["metrics"].status == "failed"
    assert "ToolUnavailableError" in (by_area["metrics"].error or "")
    # No invented findings from the failed call.
    assert by_area["metrics"].evidence == []
    # Earlier evidence retained, and the workflow ran to a real conclusion.
    assert by_area["logs"].status == "succeeded"
    assert by_area["logs"].evidence
    assert result["final_report"] is not None


# ---------------------------------------------------------------------------
# Section 2 -- the final report carries the mandated outcomes
# ---------------------------------------------------------------------------


async def test_final_report_contains_the_required_incident_outcomes(
    run_incident, scripted_llm, tool_spy
):
    # severity="high", not "critical": critical escalates the risk floor (a
    # restart on an allowlisted service goes medium -> high), which would send
    # this through the approval gate and out of the unattended path under test.
    scripted_llm.set(_TriageOut, triage("logs", "metrics", severity="high"))
    scripted_llm.set(_DiagnosisOut, _DiagnosisOut(
        root_cause="Connection pool exhausted", confidence=0.92,
        contributing_factors=["traffic spike"]))
    scripted_llm.set(_PlanOut, plan("restart_service"))
    scripted_llm.set(_RiskOut, _RiskOut(risk_level="low", reasons=[]))

    result, _, _ = await run_incident(incident_id="INC-REPORT", service=LOW_IMPACT)

    # Every mandated output is present AND matches what the run actually did,
    # so merely returning all the field names cannot satisfy this.
    assert result["triage"].severity == "high"                          # classification+severity
    assert result["investigation_results"]                              # evidence collected
    assert result["diagnosis"].root_cause == "Connection pool exhausted"  # probable root cause
    assert result["remediation_plan"].steps                             # remediation
    assert result["risk_assessment"].risk_level in {"low", "medium", "high", "critical"}
    assert result["approval_status"] == "not_required"                  # approval status
    assert result["execution_result"].status == "succeeded"             # execution
    assert result["verification_result"].status == "passed"             # verification

    report = result["final_report"]
    assert report.incident_id == "INC-REPORT"
    assert report.status == "resolved"                                  # final status
    assert report.summary                                               # concise report
    assert report.execution_attempts == result["execution_attempts"]
    assert report.plan_revision == result["remediation_plan"].revision

    # The report's verdict agrees with the verification that produced it, rather
    # than being an independently optimistic claim.
    assert (report.status == "resolved") == (result["verification_result"].status == "passed")
