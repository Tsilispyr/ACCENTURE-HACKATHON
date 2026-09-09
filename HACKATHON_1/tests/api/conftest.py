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
        "category": "incident",
        "complexity": "complex",
        "resolution": "Controlled graph result for API request-handling tests.",
        "findings": ["Controlled investigation finding."],
        "report_key": None,
    }
    mocked = MagicMock(spec=["ainvoke"])
    mocked.ainvoke = AsyncMock(return_value=result)
    # TODO(graph-integration): Patch where the endpoint looks up its workflow
    # dependency if the name, location, or calling convention are changed.
    monkeypatch.setattr(service, "app_graph", mocked)
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
        assert state["ticket_id"] == payload["Incident ID"]
        assert payload["Description"] in state["ticket_text"]
        assert payload["Error"] in state["ticket_text"]

    return check
