"""FastAPI TestClient tests -- closes the gap flagged in
notes/07-HACKATHON1-REQUIREMENTS.md: neither docerz/test_agent.py nor
day21/src/day21/test_agent.py exercises the FastAPI layer itself, only the
underlying tool function. app_graph.ainvoke is monkeypatched so no LLM call
happens here either."""

from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from hackathon1.service import app

client = TestClient(app)


# POSITIVE TEST: health check
def test_health_check():
    response = client.get("/")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# POSITIVE TEST: valid incident runs the (mocked) graph and returns its result
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
            json={"service": "Payment Service", "description": "Charged twice", "severity": "HIGH"},
        )
    assert response.status_code == 200
    body = response.json()
    assert body["resolution"] == "Refund issued."
    assert body["service"] == "Payment Service"


# NEGATIVE TEST: missing required field is rejected by Pydantic validation,
# never reaches the graph
def test_create_incident_missing_field():
    response = client.post("/incidents", json={"service": "Payment Service"})
    assert response.status_code == 422
