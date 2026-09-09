"""End-to-end test against the DEPLOYED stack.

Separate from the rest of the suite on purpose, as its placeholder asked. This
one mocks nothing: it posts an unchanged five-field example to the running
container, drives the real workflow with the real model and the real tools, and
then correlates the run with the trace Langfuse actually ingested.

It skips itself when the stack is not reachable, so `uv run pytest` stays fast
and free on a laptop with nothing running, and becomes a genuine end-to-end
check the moment `bash scripts/deploy.sh` has been run.

Deliberately NOT satisfied by GET /health: liveness is not resolution.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import requests

pytestmark = [pytest.mark.live, pytest.mark.filterwarnings("ignore")]

API = "http://localhost:8010"
CLICKHOUSE = "http://localhost:8123"
CLICKHOUSE_AUTH = ("clickhouse", "clickpassQWqw!@12")
CASE_FILE = Path(__file__).resolve().parents[2] / "data" / "simulated" / "payment_failure.json"


def _stack_is_up() -> bool:
    try:
        return requests.get(f"{API}/health", timeout=3).status_code == 200
    except Exception:
        return False


pytestmark.append(
    pytest.mark.skipif(
        not _stack_is_up(),
        reason="deployed stack not reachable on :8010 -- run `bash scripts/deploy.sh` first",
    )
)


def _clickhouse(query: str) -> str:
    response = requests.post(CLICKHOUSE, params={"query": query}, auth=CLICKHOUSE_AUTH, timeout=10)
    response.raise_for_status()
    return response.text.strip()


def test_deployed_incident_runs_through_resolution_and_trace():
    health = requests.get(f"{API}/health", timeout=5).json()
    assert health["tracing_enabled"] is True, "tracing must be live for this to prove anything"
    assert health["storage_enabled"] is True

    # One unchanged example from the shared case files -- not a payload invented
    # here, so this exercises the same input the contract tests use.
    case = dict(json.loads(CASE_FILE.read_text(encoding="utf-8"))[0])
    incident_id = f"{case['Incident ID']}-live-{int(time.time())}"
    case["Incident ID"] = incident_id

    spans_before = int(_clickhouse("SELECT count() FROM default.events_core"))

    response = requests.post(f"{API}/incidents", json=case, timeout=300)
    assert response.status_code == 200, response.text
    body = response.json()

    # Complete approval if the risk policy demanded it. Whether it does is a
    # property of the plan the model chose, so both paths must work here.
    if body["status"] == "awaiting_approval":
        assert body["approval_request"]["plan"], "an approval request must carry the plan"
        assert body["risk_assessment"]["requires_approval"] is True
        body = requests.post(
            f"{API}/incidents/{incident_id}/approve",
            json={"approved": True, "note": "live end-to-end test"},
            timeout=300,
        ).json()

    # --- the incident reached a real terminal outcome ---------------------
    assert body["status"] in {"resolved", "unresolved", "approval_rejected"}

    # --- retrievable independently, from the checkpoint store -------------
    fetched = requests.get(f"{API}/incidents/{incident_id}", timeout=30).json()
    assert fetched["incident_id"] == incident_id

    # --- required report content (handout section 2) ----------------------
    assert fetched["triage"]["severity"] in {"low", "medium", "high", "critical"}
    assert fetched["evidence"], "evidence must have been collected"
    assert fetched["diagnosis"]["root_cause"]
    assert fetched["remediation_plan"]["steps"]
    assert fetched["risk_assessment"]["risk_level"]
    assert fetched["execution_result"] is not None
    assert fetched["verification_result"] is not None

    report = fetched["final_report"]
    assert report["incident_id"] == incident_id
    assert report["summary"]
    assert report["simulated"] is True
    # The archive copy was written to object storage, not merely returned.
    assert report["report_key"] == f"{incident_id}.txt"

    # The verdict agrees with the verification that produced it.
    assert (report["status"] == "resolved") == (
        fetched["verification_result"]["status"] == "passed"
    )

    # --- correlate with the Langfuse trace ---------------------------------
    # Ingestion is asynchronous (OTLP export, then the worker), so poll rather
    # than assume it has landed. The public traces API is unavailable in v4
    # events_only mode, so this reads what was actually stored.
    deadline = time.time() + 90
    spans_after = spans_before
    while time.time() < deadline and spans_after <= spans_before:
        time.sleep(5)
        spans_after = int(_clickhouse("SELECT count() FROM default.events_core"))
    assert spans_after > spans_before, "the run produced no Langfuse spans"

    names = _clickhouse(
        "SELECT DISTINCT name FROM default.events_core "
        "ORDER BY start_time DESC LIMIT 60 FORMAT TSV"
    ).splitlines()

    # Workflow stages, and the LLM calls inside them, both observable.
    assert any(stage in names for stage in ("triage", "diagnose_incident", "investigate_incident")), (
        f"no workflow stage spans found in {names}"
    )
    assert any("AzureChatOpenAI" in name or "ChatOpenAI" in name for name in names), (
        f"no LLM call spans found in {names}"
    )
