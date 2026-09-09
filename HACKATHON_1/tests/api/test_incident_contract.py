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


class LegacyIncidentSchema(Exception):
    """Only the explicitly recognized old three-field validation mismatch. TODO: Discontinue"""


def require_five_field_endpoint(response):
    if response.status_code != 422:
        return
    detail = response.json().get("detail", [])
    old_fields = {("body", field) for field in ("service", "severity", "description")}
    if (
        isinstance(detail, list)
        and len(detail) == len(old_fields)
        and all(isinstance(error, dict) and error.get("type") == "missing" for error in detail)
        and {tuple(error.get("loc", ())) for error in detail} == old_fields
    ):
        raise LegacyIncidentSchema(
            "The endpoint still requires lowercase service/severity/description; "
            "implement the agreed Incident ID, Service, Severity, Description, Error request."
        )


# PLACEHOLDER(api-implementation): Remove this and the legacy detection helper
# after the 5 field API is implemented. Only the exact old schema mismatch is expected;
# wrong statuses, dropped fields, missing graph calls, and other failures fail.
# strict=True makes a newly passing test an XPASS failure until this TODO is
# removed, so unfinished requirements cannot silently turn into permanent skips.
pending_schema = pytest.mark.xfail(
    strict=True,
    raises=LegacyIncidentSchema,
    reason="TODO(api-implementation): replace the old request schema with the agreed five fields",
)
pytestmark = pytest.mark.api_contract


@pending_schema
@pytest.mark.parametrize("payload", CASES, ids=lambda case: case["Incident ID"])
def test_five_field_incident_is_accepted_and_forwarded(
    api_client, incident_graph, assert_incident_reaches_workflow, payload
):
    assert set(payload) == set(FIELDS)
    response = api_client.post("/incidents", json=payload)

    require_five_field_endpoint(response)
    # TODO(response-contract): The skeleton is synchronous and returns 200.
    # Update only if there is asynchronous acceptance (e.g. 202)
    # and a different workflow dispatch boundary. Otherwise remove this.
    assert response.status_code == 200, response.text
    assert_incident_reaches_workflow(incident_graph, payload)


@pending_schema
@pytest.mark.parametrize("missing_field", FIELDS)
def test_each_required_field_is_validated_before_workflow(
    api_client, incident_graph, missing_field
):
    payload = dict(CASES[0])
    del payload[missing_field]
    response = api_client.post("/incidents", json=payload)

    incident_graph.ainvoke.assert_not_awaited()
    require_five_field_endpoint(response)
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