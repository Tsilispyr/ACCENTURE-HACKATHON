from langgraph.graph import StateGraph
from langgraph.types import Command, Send

from hackathon1.graph import (
    MAX_EXECUTION_ATTEMPTS,
    DeterministicIncidentAdapter,
    build_incident_graph,
    create_incident_app,
    route_investigations,
)


def _initial_state(*, severity="critical"):
    return {
        "incident_id": "INC-101",
        "service": "payments",
        "description": "Payment requests are returning elevated errors.",
        "severity": severity,
        "investigation_results": [],
        "execution_attempts": 0,
    }


def _config(thread_id):
    return {"configurable": {"thread_id": thread_id}}


def test_build_incident_graph_is_separate_and_uncompiled():
    graph = build_incident_graph(DeterministicIncidentAdapter())
    assert isinstance(graph, StateGraph)
    assert "classify_ticket" not in graph.nodes
    assert {
        "triage",
        "investigate_incident",
        "diagnose",
        "plan_remediation",
        "assess_risk",
        "request_approval",
        "execute_remediation",
        "verify_remediation",
        "finalize_incident",
    }.issubset(graph.nodes)


def test_conditional_investigation_routing_dispatches_selected_parallel_workers():
    state = {
        "description": "errors",
        "severity": "high",
        "triage": object(),
        "investigation_areas": ["logs", "dependencies", "recent_changes"],
    }
    sends = route_investigations(state)
    assert len(sends) == 3
    assert all(isinstance(item, Send) and item.node == "investigate_incident" for item in sends)
    assert [item.arg["investigation_area"] for item in sends] == [
        "logs",
        "dependencies",
        "recent_changes",
    ]


async def test_low_risk_flow_handles_partial_investigation_failure_and_resolves():
    adapter = DeterministicIncidentAdapter(
        risk_level="low",
        failing_investigations={"metrics"},
        investigation_areas=["logs", "metrics", "dependencies", "recent_changes"],
    )
    result = await create_incident_app(adapter).ainvoke(
        _initial_state(), config=_config("partial-investigation")
    )

    assert len(result["investigation_results"]) == 4
    failed = [item for item in result["investigation_results"] if item.status == "failed"]
    assert len(failed) == 1
    assert failed[0].area == "metrics"
    assert result["severity"] == "critical"
    assert result["risk_assessment"].risk_level == "low"
    assert result["approval_status"] == "not_required"
    assert result["final_report"].status == "resolved"
    assert result["final_report"].simulated is True


async def test_high_risk_flow_interrupts_and_rejection_prevents_execution():
    app = create_incident_app(DeterministicIncidentAdapter(risk_level="high"))
    config = _config("reject-high-risk")

    paused = await app.ainvoke(_initial_state(severity="low"), config=config)
    approval_request = paused["__interrupt__"][0].value
    assert approval_request["kind"] == "remediation_approval"
    assert paused["severity"] == "low"
    assert paused["risk_assessment"].risk_level == "high"

    result = await app.ainvoke(Command(resume={"approved": False}), config=config)
    assert result["execution_attempts"] == 0
    assert "execution_result" not in result
    assert result["final_report"].status == "approval_rejected"


async def test_replanned_high_risk_plan_requires_fresh_approval():
    adapter = DeterministicIncidentAdapter(
        risk_level="high", execution_failures_before_success=1
    )
    app = create_incident_app(adapter)
    config = _config("approve-each-revision")

    first_pause = await app.ainvoke(_initial_state(), config=config)
    assert first_pause["__interrupt__"][0].value["plan_revision"] == 0

    second_pause = await app.ainvoke(Command(resume="approve"), config=config)
    assert second_pause["execution_attempts"] == 1
    assert second_pause["__interrupt__"][0].value["plan_revision"] == 1
    assert second_pause["approval_status"] == "pending"

    result = await app.ainvoke(Command(resume="approve"), config=config)
    assert result["final_report"].status == "resolved"
    assert result["execution_attempts"] == 2
    assert result["approved_plan_revision"] == 1


async def test_execution_failures_stop_at_bounded_attempt_limit():
    adapter = DeterministicIncidentAdapter(
        execution_failures_before_success=MAX_EXECUTION_ATTEMPTS + 5
    )
    result = await create_incident_app(adapter).ainvoke(
        _initial_state(), config=_config("bounded-execution")
    )

    assert result["execution_attempts"] == MAX_EXECUTION_ATTEMPTS
    assert result["remediation_plan"].revision == MAX_EXECUTION_ATTEMPTS - 1
    assert result["verification_result"].status == "failed"
    assert result["final_report"].status == "unresolved"
    assert result["final_report"].simulated is True


async def test_execution_exceptions_are_recorded_and_bounded():
    class RaisingExecutionAdapter(DeterministicIncidentAdapter):
        async def execute(self, state):
            raise RuntimeError("simulator unavailable")

    result = await create_incident_app(RaisingExecutionAdapter()).ainvoke(
        _initial_state(), config=_config("execution-exception")
    )

    assert result["execution_attempts"] == MAX_EXECUTION_ATTEMPTS
    assert result["execution_result"].status == "failed"
    assert result["execution_result"].error == "RuntimeError: simulator unavailable"
    assert result["final_report"].status == "unresolved"
