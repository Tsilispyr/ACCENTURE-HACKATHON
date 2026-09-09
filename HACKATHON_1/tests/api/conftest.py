"""Adapters for API contract tests"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from hackathon1 import service


@pytest.fixture
def api_client():
    # TODO(api-integration): Update this import/setup if the API moves. 
    # Use the actual endpoint.
    with TestClient(service.app) as client:
        yield client


@pytest.fixture
def incident_graph(monkeypatch):
    # PLACEHOLDER(workflow-result): This is only the current graph result shape.
    # TODO(report-contract): Replace it with the actual structured report.
    result = {
        "incident_id": "CONTROLLED",
        "service": "payment-service",
        "severity": "high",
        "investigation_results": [],
        "approval_status": "not_required",
        "execution_attempts": 1,
    }
    mocked = MagicMock(spec=["ainvoke"])
    mocked.ainvoke = AsyncMock(return_value=result)
    # TODO(graph-integration): Patch where the endpoint looks up its workflow
    # dependency if the name, location, or calling convention are changed.
    # The endpoint resolves its workflow through get_incident_app(), which is
    # lru_cached, so the cache is cleared around the patch or a real adapter
    # built by an earlier test would be reused here.
    service.get_incident_app.cache_clear()
    monkeypatch.setattr(service, "get_incident_app", lambda: mocked)
    return mocked


@pytest.fixture
def assert_incident_reaches_workflow():
    def check(mocked_graph, payload):
        mocked_graph.ainvoke.assert_awaited_once()
        state = mocked_graph.ainvoke.await_args.args[0]

        # PLACEHOLDER(state-mapping): The current graph uses ticket_id and
        # ticket_text. Its API adapter should preserve the supplied ID and carry
        # both the description and error into that text until state is redesigned.
        # TODO(state-contract): Change ONLY this mapping if the real state
        # uses dedicated incident_id/description/error fields or a nested model.
        # Preserve these behavioral checks: never replace the supplied ID or
        # silently discard the description/error before calling the workflow.
        # Canonical state names, set with the CodeHub graph's removal:
        # incident_id / service / severity / description.
        assert state["incident_id"] == payload["Incident ID"]
        assert state["service"] == payload["Service"]
        assert payload["Description"] in state["description"]
        assert payload["Error"] in state["description"]

    return check
