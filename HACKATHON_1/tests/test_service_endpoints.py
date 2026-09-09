"""FastAPI TestClient tests -- closes the gap flagged in
notes/07-HACKATHON1-REQUIREMENTS.md: neither docerz/test_agent.py nor
day21/src/day21/test_agent.py exercises the FastAPI layer itself, only the
underlying tool function. the incident app is monkeypatched so no LLM call
happens here either."""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from hackathon1 import service
from hackathon1.service import app
from hackathon1.tools import get_service_metrics
from hackathon1.world import active_world

client = TestClient(app)


# POSITIVE TEST: health check
def test_health_check():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# POSITIVE TEST: valid incident runs the (mocked) graph and returns its result
# Kept, not retired. tests/api/test_incident_contract.py now covers acceptance
# and forwarding of the five-field request, but it deliberately asserts nothing
# about the RESPONSE body (see its report-contract TODO). This test is the only
# one checking that the endpoint returns the workflow's result rather than
# swallowing it, so the coverage does not overlap.
def test_create_incident_valid_payload():
    fake_result = {
        "incident_id": "INC-9001",
        "service": "payment-service",
        "severity": "HIGH",
        "investigation_results": [],
        "approval_status": "not_required",
        "execution_attempts": 1,
    }
    mock_graph = MagicMock(spec=["ainvoke"])
    mock_graph.ainvoke = AsyncMock(return_value=fake_result)
    service.get_incident_app.cache_clear()
    with patch("hackathon1.service.get_incident_app", return_value=mock_graph):
        response = client.post(
            "/incidents",
            json={
                "Incident ID": "INC-9001",
                "Service": "payment-service",
                "Severity": "HIGH",
                "Description": "Charged twice",
                "Error": "Duplicate charge recorded.",
            },
        )
    assert response.status_code == 200
    body = response.json()
    assert body["incident_id"] == "INC-9001"
    assert body["service"] == "payment-service"
    assert body["approval_status"] == "not_required"


def test_create_incident_binds_one_world_that_reaches_parallel_branches():
    """The endpoint binds the estate; child tasks must inherit that same one.

    The world lives in a ContextVar, and an asyncio task runs on a *copy* of
    its parent's context. So a world created lazily inside a task -- which is
    what happens when nothing binds one -- is invisible to every sibling task
    and gone when the request ends. Under the investigation fan-out that split
    one run across several unrelated estates, and it made the deliberate
    first-call metrics failure unrecoverable: each retry landed in a fresh
    world whose call counter had reset, so it failed again, forever.

    This asserts the fix at the endpoint, in the shape the bug had: several
    concurrent branches see one World, and the flake fires once and then
    recovers.
    """
    seen: dict[str, object] = {}

    async def run_with_branches(*_args, **_kwargs):
        async def branch():
            return id(active_world()), get_service_metrics.invoke(
                {"service": "payment-service"}
            )["status"]

        results = await asyncio.gather(*(asyncio.create_task(branch()) for _ in range(3)))
        seen["worlds"] = {world for world, _ in results}
        seen["statuses"] = [status for _, status in results]
        return {"incident_id": "INC-9100", "investigation_results": []}

    mock_graph = MagicMock(spec=["ainvoke"])
    mock_graph.ainvoke = AsyncMock(side_effect=run_with_branches)
    service.get_incident_app.cache_clear()
    with patch("hackathon1.service.get_incident_app", return_value=mock_graph):
        response = client.post(
            "/incidents",
            json={
                "Incident ID": "INC-9100",
                "Service": "payment-service",
                "Severity": "HIGH",
                "Description": "Checkout latency",
                "Error": "Timeout waiting for a connection.",
            },
        )

    assert response.status_code == 200
    assert len(seen["worlds"]) == 1, "parallel branches must share the request's world"
    # The scenario's collector rejects the first read and answers the next.
    assert seen["statuses"].count("unavailable") == 1
    assert seen["statuses"].count("ok") == 2


# Retired as instructed: this checked one incomplete payload, and
# tests/api/test_incident_contract.py's
# test_each_required_field_is_validated_before_workflow now parametrises over
# ALL FIVE fields and additionally asserts the workflow is never awaited and
# that the error names the omitted field. Keeping this would have been strictly
# weaker duplicate coverage.


def test_chat_agent_is_created_on_first_chat_and_reused(mock_llm):
    agent = MagicMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [AIMessage(content="Four hours.")]})
    service.get_chat_agent.cache_clear()
    try:
        with patch("hackathon1.service.create_agent", return_value=agent) as create:
            assert client.get("/").status_code == 200
            create.assert_not_called()

            for _ in range(2):
                response = client.get("/chat", params={"message": "What is the HIGH SLA?"})
                assert response.json() == {"response": "Four hours.", "status": "success"}

            create.assert_called_once()
            assert create.call_args.kwargs["model"] is mock_llm
            assert agent.ainvoke.await_count == 2
    finally:
        service.get_chat_agent.cache_clear()


def test_ui_is_served_and_is_self_contained():
    """The operator page loads, and pulls nothing from the network.

    A CDN reference would render a blank page in a container with no internet
    access, which is the environment this actually runs in.
    """
    response = client.get("/ui")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]

    body = response.text
    # The five mandated request fields are all present as inputs.
    for field in ("Incident ID", "Service", "Severity", "Description", "Error"):
        assert f'name="{field}"' in body, f"missing input for {field!r}"

    # No external asset references.
    for marker in ("cdn.", "http://unpkg", "https://unpkg", "googleapis", "jsdelivr"):
        assert marker not in body, f"UI must not depend on {marker!r}"


def test_root_still_returns_json_for_the_container_healthcheck():
    """Adding the UI must not turn / into a web page: the compose healthcheck
    parses it, and a redirect or HTML body would fail the container."""
    response = client.get("/")
    assert response.json() == {"status": "ok"}
