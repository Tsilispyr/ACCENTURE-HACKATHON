"""Behavior checks for the real graph, with deterministic external responses."""

import logging

import pytest

pytestmark = pytest.mark.workflow


async def test_simple_incident_resolves_without_investigation(
    scenarios, run_workflow, storage_client
):
    scenario = scenarios["simple"]
    result = await run_workflow(scenario)

    assert result["ticket_id"] == scenario["ticket_id"]
    assert result["category"] == scenario["classification"]["category"]
    assert result["complexity"] == "simple"
    assert scenario["resolution"] in result["resolution"]
    assert len(result["messages"]) == 1
    assert result["handoff_count"] == 0
    assert result["findings"] == []
    assert result["report_key"] is None
    storage_client.bucket_exists.assert_not_called()
    storage_client.put_object.assert_not_called()


async def test_complex_incident_combines_parallel_findings_and_stores_report(
    scenarios, run_workflow, storage_client
):
    scenario = scenarios["complex"]
    result = await run_workflow(scenario)

    assert result["category"] == scenario["classification"]["category"]
    assert result["complexity"] == "complex"
    assert result["resolution"] == scenario["resolution"]
    assert len(result["findings"]) == len(scenario["evidence"])
    for finding in scenario["evidence"].values():
        assert sum(finding in item for item in result["findings"]) == 1

    storage_client.make_bucket.assert_called_once_with("test-reports")
    storage_client.put_object.assert_called_once()
    args, kwargs = storage_client.put_object.call_args
    assert args[0] == "test-reports"
    assert args[1] == result["report_key"] == f"{scenario['ticket_id']}.txt"
    report_bytes = args[2].getvalue()
    assert kwargs["length"] == len(report_bytes)
    report = report_bytes.decode("utf-8")
    for finding in result["findings"]:
        assert finding in report
    assert result["resolution"] in report


async def test_storage_failure_preserves_resolution_and_logs_warning(
    scenarios, run_workflow, storage_client, caplog
):
    storage_client.put_object.side_effect = ConnectionError("Simulated storage outage")
    scenario = scenarios["complex"]

    with caplog.at_level(logging.WARNING, logger="hackathon1.storage"):
        result = await run_workflow(scenario)

    storage_client.put_object.assert_called_once()
    assert result["report_key"] is None
    assert result["resolution"] == scenario["resolution"]
    assert len(result["findings"]) == len(scenario["evidence"])
    assert "MinIO upload failed" in caplog.text
    assert scenario["ticket_id"] in caplog.text
