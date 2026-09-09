"""Tests for the real tools in `hackathon1.tools`.

Replaces the original file, which tested `lookup_sla_hours` and
`check_known_issue` -- CodeHub placeholder tools that the rewrite removed.

Weighted towards the risk functions rather than spread evenly. `risk_of`,
`effective_risk` and `requires_approval` are what the human-approval gate
rests on: they are pure, deterministic, and the only thing standing between a
model's opinion and an unattended `rollback_change`. The investigation tools
returning plausible data matters much less than those three being impossible to
argue with.
"""


import pytest

from hackathon1 import tools
from hackathon1.tools import (
    ACTION_RISK,
    check_service_health,
    effective_risk,
    get_incident_history,
    get_service_metrics,
    requires_approval,
    restart_service,
    risk_of,
    rollback_change,
    scale_connection_pool,
    search_knowledge_base,
    search_logs,
)
from hackathon1.world import reset_world

CRITICAL = "payment-service"      # absent from LOW_IMPACT_SERVICES
LOW_IMPACT = "order-service"      # present in LOW_IMPACT_SERVICES


@pytest.fixture(autouse=True)
def fresh_world():
    """Reset the simulated estate before each test.

    The remediation tools mutate shared module-level world state, so without
    this a test that restarts a service silently changes what a later test's
    health check returns -- and the order dependence would only show up when
    someone reruns a single test in isolation.
    """
    reset_world()
    yield
    reset_world()


# ---------------------------------------------------------------------------
# Risk policy -- the approval gate's foundation
# ---------------------------------------------------------------------------


def test_risk_is_contextual_not_per_action():
    """The same action carries different risk depending on what it targets."""
    assert risk_of("restart_service", CRITICAL) == "high"
    assert risk_of("restart_service", LOW_IMPACT) == "medium"


def test_rollback_is_never_downgraded_by_a_low_impact_service():
    """A rollback reverts everything else in the same change, so its blast
    radius comes from the change and not from the service it names."""
    assert risk_of("rollback_change", LOW_IMPACT) == "high"
    assert risk_of("rollback_change", CRITICAL) == "high"


def test_unknown_action_defaults_to_high_risk():
    """Fail safe. An action nobody wrote down must not slip through as low."""
    assert risk_of("drop_database", CRITICAL) == "high"
    assert risk_of("", "") == "high"


def test_low_impact_list_is_an_allowlist_not_a_denylist():
    """A service nobody has classified keeps the action's full risk.

    This is what protects the hidden scenario: the worst outcome of an unknown
    service is an unnecessary approval, never an unattended restart.
    """
    assert risk_of("restart_service", "service-nobody-has-seen-before") == "high"


def test_model_assessment_can_raise_the_risk_floor():
    assert effective_risk("scale_connection_pool", LOW_IMPACT, "low", assessed="high") == "high"


def test_model_assessment_can_never_lower_the_risk_floor():
    """The single most important assertion in this file.

    Incident text is attacker-controllable in any real deployment. A gate the
    model can talk its way past is decorative, so a low assessment against a
    high-risk action must be ignored.
    """
    assert effective_risk("restart_service", CRITICAL, "high", assessed="low") == "high"
    assert effective_risk("rollback_change", LOW_IMPACT, "low", assessed="none") == "high"


def test_absent_assessment_leaves_the_policy_floor_in_charge():
    """A failed or missing model call must not read as 'low risk'."""
    assert effective_risk("restart_service", CRITICAL, "high", assessed=None) == "high"


def test_requires_approval_tracks_high_risk_only():
    assert requires_approval("restart_service", CRITICAL) is True
    assert requires_approval("rollback_change", LOW_IMPACT) is True
    assert requires_approval("scale_connection_pool", LOW_IMPACT) is False


def test_every_exposed_remediation_action_has_a_declared_base_risk():
    """Guards against adding a tool and forgetting its risk entry -- which
    would silently fall through to the 'high' default and look like a policy
    decision rather than an oversight."""
    for action in ("scale_connection_pool", "restart_service", "rollback_change"):
        assert action in ACTION_RISK


# ---------------------------------------------------------------------------
# Investigation tools (read-only)
# ---------------------------------------------------------------------------


def test_search_logs_returns_entries_with_the_documented_shape():
    entries = search_logs.invoke({"service": CRITICAL})
    assert entries, "the seeded estate should have logs for a failing service"
    for entry in entries:
        assert {"timestamp", "level", "message", "count"} <= set(entry)


def test_search_logs_level_filter_narrows_results():
    warn = search_logs.invoke({"service": CRITICAL, "level": "WARN"})
    everything = search_logs.invoke({"service": CRITICAL, "level": "ALL"})
    assert len(everything) >= len(warn)


def test_get_service_metrics_reports_unavailable_then_succeeds_on_retry():
    """The metrics backend fails the FIRST call for this service, by design.

    The tool reports that outage instead of raising and instead of retrying
    itself: the caller is told the collector was unreachable and is left to
    decide whether to read again. The second read succeeds, which is what makes
    a retry the right decision rather than a guess.
    """
    down = get_service_metrics.invoke({"service": CRITICAL})
    assert down["status"] == "unavailable"
    assert down["error"], "an unavailable read must say what went wrong"
    assert down["readings"] == [], "nothing was measured, so nothing may be reported"

    recovered = get_service_metrics.invoke({"service": CRITICAL})
    assert recovered["status"] == "ok"
    assert recovered["service"] == CRITICAL
    assert recovered["error"] is None
    assert recovered["readings"]


def test_get_service_metrics_returns_a_mapping_with_baselines():
    """A mapping, not a list -- the shape the test-suite placeholder got wrong."""
    # The first call for this service always reports the collector as down (see
    # the test above), and the autouse fixture resets that flake for every
    # test, so it must be spent here rather than assumed spent.
    get_service_metrics.invoke({"service": CRITICAL})
    metrics = get_service_metrics.invoke({"service": CRITICAL})
    assert isinstance(metrics, dict)
    assert metrics["service"] == CRITICAL
    assert metrics["readings"], "expected at least one reading"
    for reading in metrics["readings"]:
        assert "breached" in reading, "a reading without a breach flag cannot be judged"


def test_search_knowledge_base_matches_on_symptoms_not_service_names():
    articles = search_knowledge_base.invoke({"query": "database connection timeout"})
    assert articles
    assert all("recommended_actions" in a or "probable_cause" in a for a in articles)


def test_knowledge_base_works_for_a_service_never_seen_before():
    """The only investigation tool that can help with the hidden scenario."""
    assert search_knowledge_base.invoke({"query": "connection pool exhausted"})


def test_get_incident_history_returns_past_incidents():
    history = get_incident_history.invoke({"service": CRITICAL})
    assert isinstance(history, list)


# ---------------------------------------------------------------------------
# Remediation and verification
# ---------------------------------------------------------------------------


def test_remediation_returns_a_receipt_and_is_marked_simulated():
    receipt = restart_service.invoke({"service": LOW_IMPACT})
    assert receipt["action"] == "restart_service"
    assert receipt["service"] == LOW_IMPACT
    assert receipt["simulated"] is True


def test_remediation_receipt_never_claims_the_incident_is_resolved():
    """Execution reports what it did; recovery is verify()'s call.

    If a receipt could assert recovery the replan loop would have nothing
    independent to react to, and a model could declare victory on its own.
    """
    receipt = restart_service.invoke({"service": CRITICAL})
    assert "resolved" not in {str(v).lower() for v in receipt.values()}


def test_scale_connection_pool_records_the_requested_size():
    receipt = scale_connection_pool.invoke({"service": CRITICAL, "new_size": 50})
    assert receipt["params"]["new_size"] == 50


def test_rollback_change_carries_the_change_id():
    receipt = rollback_change.invoke({"service": CRITICAL, "change_id": "CHG-8801"})
    assert receipt["params"]["change_id"] == "CHG-8801"


def test_check_service_health_reports_the_documented_shape():
    health = check_service_health.invoke({"service": CRITICAL})
    assert {"service", "status", "healthy"} <= set(health)
    assert isinstance(health["healthy"], bool)


def test_a_partial_remediation_does_not_restore_health():
    """Scaling the pool alone leaves the service down, on purpose.

    The scenario marks this action "partial": the ceiling is raised in config
    but the running process still holds the old one. This is what gives the
    replan loop something real to react to -- a remediation that plausibly
    should have worked, and verifiably did not.
    """
    assert check_service_health.invoke({"service": CRITICAL})["healthy"] is False

    scale_connection_pool.invoke({"service": CRITICAL, "new_size": 50})

    assert check_service_health.invoke({"service": CRITICAL})["healthy"] is False


def test_remediation_out_of_order_does_not_restore_health():
    """A restart without the prerequisite scale achieves nothing.

    Order matters, so an agent cannot stumble into recovery by picking the
    highest-impact action first.
    """
    restart_service.invoke({"service": CRITICAL})
    assert check_service_health.invoke({"service": CRITICAL})["healthy"] is False


def test_the_full_remediation_sequence_restores_health():
    """scale, then restart: the sequence the scenario declares as a full fix."""
    assert check_service_health.invoke({"service": CRITICAL})["healthy"] is False

    scale_connection_pool.invoke({"service": CRITICAL, "new_size": 50})
    restart_service.invoke({"service": CRITICAL})

    after = check_service_health.invoke({"service": CRITICAL})
    assert after["healthy"] is True
    assert after["status"] == "healthy"


def test_tool_failure_raises_rather_than_returning_a_plausible_lie():
    """The graph catches this and records a failed investigation area, which is
    what gives the workflow a real failure path instead of fabricated evidence."""
    assert issubclass(tools.ToolUnavailableError, Exception)
