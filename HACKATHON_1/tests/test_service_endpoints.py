"""FastAPI TestClient tests -- closes the gap flagged in
notes/07-HACKATHON1-REQUIREMENTS.md: neither docerz/test_agent.py nor
day21/src/day21/test_agent.py exercises the FastAPI layer itself, only the
underlying tool function. app_graph.ainvoke is monkeypatched so no LLM call
happens here either."""

from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from hackathon1 import service
from hackathon1.service import app

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
        "category": "billing",
        "complexity": "simple",
        "resolution": "Refund issued.",
        "findings": [],
        "report_key": None,
    }
    with patch("hackathon1.service.app_graph") as mock_graph:
        mock_graph.ainvoke = AsyncMock(return_value=fake_result)
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
    assert body["resolution"] == "Refund issued."
    assert body["service"] == "payment-service"


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
