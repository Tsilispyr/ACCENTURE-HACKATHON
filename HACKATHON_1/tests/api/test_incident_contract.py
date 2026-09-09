"""Five field request contract.

These tests post the generated examples unchanged and mock only the workflow.
They do not evaluate diagnoses or count as end-to-end incident coverage.
"""

import json
from pathlib import Path

import pytest


FIELDS = ("Incident ID", "Service", "Severity", "Description", "Error")
DATA_DIR = Path(__file__).resolve().parents[2] / "data" / "simulated"
# TODO(data-location): Update paths here if shared example files move.
CASE_FILES = (
    "payment_failure.json",
    "authentication_failure.json",
    "performance_degradation.json",
)
CASES = [
    case
    for filename in CASE_FILES
    for case in json.loads((DATA_DIR / filename).read_text(encoding="utf-8"))
]


# The five-field request is implemented, so the LegacyIncidentSchema detector
# and the strict xfail that guarded it are gone -- deleted per their own
# instruction, not left behind as a permanently-passing xfail.
pytestmark = pytest.mark.api_contract


@pytest.mark.parametrize("payload", CASES, ids=lambda case: case["Incident ID"])
def test_five_field_incident_is_accepted_and_forwarded(
    api_client, incident_graph, assert_incident_reaches_workflow, payload
):
    assert set(payload) == set(FIELDS)
    response = api_client.post("/incidents", json=payload)

    # TODO(response-contract): The skeleton is synchronous and returns 200.
    # Update only if there is asynchronous acceptance (e.g. 202)
    # and a different workflow dispatch boundary. Otherwise remove this.
    assert response.status_code == 200, response.text
    assert_incident_reaches_workflow(incident_graph, payload)


@pytest.mark.parametrize("missing_field", FIELDS)
def test_each_required_field_is_validated_before_workflow(
    api_client, incident_graph, missing_field
):
    payload = dict(CASES[0])
    del payload[missing_field]
    response = api_client.post("/incidents", json=payload)

    incident_graph.ainvoke.assert_not_awaited()
    assert response.status_code == 422, response.text
    # Verify the omitted field specifically, so rejection by an unrelated old
    # schema cannot incorrectly satisfy this negative test. This is FastAPI's
    # current validation format.
    errors = response.json()["detail"]
    assert any(
        error["loc"] == ["body", missing_field] and error["type"] == "missing"
        for error in errors
    ), errors


# TODO(report-contract): Add response field/evidence assertions after the report schema is ready.

# TODO(validation-policy): Add empty-string, malformed-ID, and severity-enum
# cases after their policy is in place.

# TODO(end-to-end): Exercise the real workflow/tools and deployed API separately.
# These mocked boundary tests do not satisfy end-to-end testing requirement.