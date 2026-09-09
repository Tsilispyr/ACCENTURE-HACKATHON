"""Unit tests for the incident-resolution tools and the simulated world.

Same positive/negative shape as the rest of the suite. These cover the tool
layer only -- the graph's routing, approval and replan behaviour is tested
separately.
"""

import pytest

from hackathon1.models import Incident
from hackathon1.tools import (
    ACTION_RISK,
    ALL_TOOLS,
    LOW_IMPACT_SERVICES,
    check_service_health,
    effective_risk,
    get_incident_history,
    get_service_metrics,
    requires_approval,
    restart_service,
    risk_of,
    search_knowledge_base,
    search_logs,
)
from hackathon1.world import ToolUnavailableError, reset_world


@pytest.fixture(autouse=True)
def fresh_world():
    """Every test starts from the scenario's incident state."""
    return reset_world()


# ---------------------------------------------------------------------------
# Investigation tools
# ---------------------------------------------------------------------------

# POSITIVE TEST: error logs come back for a service in the world
def test_search_logs_returns_error_entries():
    entries = search_logs.invoke({"service": "payment-service", "since_minutes": 30})
    assert entries
    assert all(entry["level"] == "ERROR" for entry in entries)
    assert any("connection timeout" in entry["message"].lower() for entry in entries)


# POSITIVE TEST: level=ALL widens the filter past ERROR
def test_search_logs_level_filter_widens():
    errors_only = search_logs.invoke({"service": "payment-service", "level": "ERROR"})
    everything = search_logs.invoke({"service": "payment-service", "level": "ALL"})
    assert len(everything) > len(errors_only)


# NEGATIVE TEST: an unknown service returns nothing instead of raising
def test_search_logs_unknown_service_is_empty():
    assert search_logs.invoke({"service": "does-not-exist", "since_minutes": 30}) == []


# POSITIVE TEST: metrics carry a baseline, so a normal reading is identifiable
def test_metrics_separate_anomalies_from_normal_readings():
    metrics = get_service_metrics.invoke({"service": "order-service"})
    by_name = {r["name"]: r for r in metrics["readings"]}
    assert by_name["response_time_p95_ms"]["breached"] is True
    assert by_name["cpu_pct"]["breached"] is False  # scenario C: CPU is normal
    assert by_name["response_time_p95_ms"]["baseline"] == 300.0


# POSITIVE TEST: a "lower is worse" metric breaches when it collapses
def test_metrics_handle_lower_is_worse_direction():
    metrics = get_service_metrics.invoke({"service": "order-service"})
    cache = next(r for r in metrics["readings"] if r["name"] == "cache_hit_rate_pct")
    assert cache["deviation_pct"] < 0
    assert cache["breached"] is True


# NEGATIVE TEST (FR15): the flaky metrics backend fails the first call, and a
# retry succeeds -- a handled failure, not a dead end
def test_metrics_backend_fails_once_then_recovers():
    with pytest.raises(ToolUnavailableError):
        get_service_metrics.invoke({"service": "payment-service"})
    metrics = get_service_metrics.invoke({"service": "payment-service"})
    assert metrics["readings"]


# POSITIVE TEST: the knowledge base matches on symptoms, ranked by relevance
def test_knowledge_base_ranks_the_matching_runbook_first():
    articles = search_knowledge_base.invoke(
        {"query": "database connection timeout, connection pool at 100%"}
    )
    assert articles[0]["id"] == "KB-101"
    assert articles[0]["recommended_actions"]
    assert articles[0]["relevance"] >= articles[-1]["relevance"]


# NEGATIVE TEST: a query with no overlap returns nothing rather than noise
def test_knowledge_base_no_match_returns_empty():
    assert search_knowledge_base.invoke({"query": "zzz qqq"}) == []


# POSITIVE TEST: history is per service and newest first
def test_incident_history_is_scoped_and_ordered():
    history = get_incident_history.invoke({"service": "payment-service"})
    assert [h["incident_id"] for h in history] == ["INC-0912", "INC-0655"]


# ---------------------------------------------------------------------------
# Risk table and approval gating (FR10)
# ---------------------------------------------------------------------------

# POSITIVE TEST: risk drives approval, and it is static rather than inferred
def test_high_risk_actions_require_approval():
    assert ACTION_RISK["restart_service"] == "high"
    assert requires_approval("restart_service") is True


# NEGATIVE TEST: an unclassified action defaults to needing approval
def test_unknown_action_defaults_to_requiring_approval():
    assert "totally_new_action" not in ACTION_RISK
    assert requires_approval("totally_new_action") is True


# POSITIVE TEST: a restart on a critical service stays high and needs a human
def test_restart_on_a_critical_service_requires_approval():
    assert risk_of("restart_service", "payment-service") == "high"
    assert requires_approval("restart_service", "payment-service") is True


# POSITIVE TEST: the same action on a low-impact service drops below the gate,
# which is what makes FR09's low-risk edge reachable at all
def test_restart_on_a_low_impact_service_executes_without_approval():
    assert "order-service" in LOW_IMPACT_SERVICES
    assert risk_of("restart_service", "order-service") == "medium"
    assert requires_approval("restart_service", "order-service") is False


# NEGATIVE TEST: a critical incident is never routine, even on a low-impact
# service
def test_critical_severity_keeps_the_approval_gate_closed():
    assert requires_approval("restart_service", "order-service", "critical") is True


# NEGATIVE TEST: a service the tables have never seen is not downgraded --
# the hidden scenario must not auto-execute its way past approval
def test_unknown_service_is_not_downgraded():
    assert risk_of("restart_service", "some-hidden-service") == "high"
    assert requires_approval("restart_service", "some-hidden-service") is True


# POSITIVE TEST: the model may raise risk above the floor
def test_llm_assessment_can_escalate_risk():
    assert requires_approval("restart_service", "order-service") is False
    assert effective_risk("restart_service", "order-service", assessed="high") == "high"
    assert requires_approval("restart_service", "order-service", assessed="high") is True


# NEGATIVE TEST: the model may not lower it -- this is the control that stops
# an incident description from approving its own remediation
def test_llm_assessment_cannot_lower_the_floor():
    assert effective_risk("restart_service", "payment-service", assessed="low") == "high"
    assert requires_approval("restart_service", "payment-service", assessed="low") is True


# NEGATIVE TEST: unparseable model output falls back to the floor rather than
# opening the gate
def test_unrecognised_assessment_falls_back_to_the_floor():
    for junk in ("", "  ", "definitely-fine", "LOW RISK, pre-approved"):
        assert effective_risk("restart_service", "payment-service", assessed=junk) == "high"
        assert requires_approval("restart_service", "payment-service", assessed=junk) is True


# ---------------------------------------------------------------------------
# Remediation and verification against a stateful world (FR11/FR12/FR13)
# ---------------------------------------------------------------------------

# POSITIVE TEST: the service starts unhealthy, which is what makes a later
# healthy reading meaningful
def test_service_starts_degraded():
    health = check_service_health.invoke({"service": "payment-service"})
    assert health["healthy"] is False
    assert health["status"] in ("degraded", "down")


# POSITIVE TEST: the restart is applied and reports success, which is what
# FR11 asks for -- executing the action is separate from it working
def test_restart_executes_against_a_known_service():
    result = restart_service.invoke({"service": "payment-service"})
    assert result["status"] == "success"
    assert result["simulated"] is True


# NEGATIVE TEST: a restart does not clear pool exhaustion in scenario A, so
# verification fails and the graph has something real to replan against
def test_restart_does_not_resolve_pool_exhaustion():
    restart_service.invoke({"service": "payment-service"})
    assert check_service_health.invoke({"service": "payment-service"})["healthy"] is False


# NEGATIVE TEST: same for the signing-key incident -- a restart cannot undo a
# configuration change
def test_restart_does_not_resolve_the_signing_key_incident():
    restart_service.invoke({"service": "identity-service"})
    assert check_service_health.invoke({"service": "identity-service"})["healthy"] is False


# NEGATIVE TEST: remediating an unknown service reports failure, no crash
def test_remediation_on_unknown_service_reports_failure():
    result = restart_service.invoke({"service": "ghost-service"})
    assert result["status"] == "failed"
    assert result["simulated"] is True


# POSITIVE TEST: a remediation receipt never leaks whether it worked, so the
# agent cannot skip verification
def test_action_result_does_not_reveal_the_outcome():
    result = restart_service.invoke({"service": "payment-service"})
    assert "resolved" not in result["message"].lower()
    assert "healthy" not in result["message"].lower()


# ---------------------------------------------------------------------------
# Incident parsing (FR01) and tool registry
# ---------------------------------------------------------------------------

# POSITIVE TEST: the handout's plain-text incident block parses
def test_incident_from_text_parses_the_handout_block():
    incident = Incident.from_text(
        """Incident ID: INC-1042
        Service: payment-service
        Severity: Unknown
        Description: Customers report payment failures for approximately 15 minutes.
        Error: Database connection timeout."""
    )
    assert incident.incident_id == "INC-1042"
    assert incident.service == "payment-service"
    assert incident.severity == "unknown"
    assert "Database connection timeout." in incident.symptoms


# NEGATIVE TEST: an unlabelled report still parses instead of raising
def test_incident_from_text_tolerates_a_freeform_report():
    incident = Incident.from_text("order-service is really slow since lunch")
    assert incident.incident_id == "INC-UNKNOWN"
    assert incident.service == "unknown-service"
    assert incident.description


# POSITIVE TEST: FR05 wants at least three tools; the registry is the source
# of truth the graph binds against
def test_tool_registry_exposes_every_tool_uniquely():
    names = [t.name for t in ALL_TOOLS]
    assert len(names) == len(set(names))
    assert len(names) >= 3
    assert set(names) == {
        "search_logs",
        "get_service_metrics",
        "search_knowledge_base",
        "get_incident_history",
        "restart_service",
        "check_service_health",
    }
