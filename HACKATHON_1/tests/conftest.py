"""Shared offline dependencies and the adapter between scenarios and graph code."""

import asyncio
import json
import socket
from pathlib import Path
from pydantic import BaseModel, Field
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from langchain_core.messages import AIMessage

from hackathon1 import graph, service, storage
from tests.tool_interfaces import PendingToolInterfaces


class Scenario(BaseModel):
    ticket_id: str
    ticket_text: str
    classification: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    route: dict = Field(default_factory=dict)
    handoff: dict = Field(default_factory=dict)
    resolution: str


@pytest.fixture
def pending_tool_mocks():
    """PLACEHOLDER: tool mocks, waiting code to update.

    TODO(tool-integration): Update PendingToolInterfaces to the implemented APIs,
    populate responses from scenario data, and patch the dependencies where
    the workflow looks them up.
    """
    mocks = create_autospec(PendingToolInterfaces, instance=True, spec_set=True)
    for name in (
        "search_logs",
        "get_service_metrics",
        "search_knowledge_base",
        "execute_remediation",
        "check_service_health",
    ):
        # PLACEHOLDER: no invented successful defaults. Once a scenario exists,
        # we will replace side_effect with its response function, or set side_effect=None
        # and return_value to its data. Async methods become AsyncMock objects.
        getattr(mocks, name).side_effect = NotImplementedError(
            f"PLACEHOLDER: configure and integrate {name} before using this fixture"
        )
    return mocks


# Temporary for early testing, remove later
@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    """Disable tracing and fail promptly on accidental network access."""
    def reject_network(*args, **kwargs):
        raise AssertionError("Offline test attempted network access; mock the dependency")

    monkeypatch.setattr(socket.socket, "connect", reject_network)
    monkeypatch.setattr(socket.socket, "connect_ex", reject_network)
    monkeypatch.setattr(socket, "create_connection", reject_network)
    monkeypatch.setattr(service, "get_callback_handlers", lambda: [])
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Mock LLM instead of calling real models."""
    model = MagicMock(spec=["ainvoke", "with_structured_output"])
    model.ainvoke = AsyncMock(side_effect=AssertionError("Configure the LLM response"))
    model.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=AssertionError("Configure the structured LLM response")
    )
    monkeypatch.setattr(graph, "get_llm", lambda: model)
    monkeypatch.setattr(service, "get_llm", lambda: model)
    return model


@pytest.fixture(autouse=True)
def storage_client(monkeypatch):
    """Test real report encoding/upload handling with a fake MinIO client."""
    client = MagicMock(spec=["bucket_exists", "make_bucket", "put_object"])
    client.bucket_exists.return_value = False
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    monkeypatch.setattr(storage, "_BUCKET", "test-reports")
    return client


@pytest.fixture
def scenarios():
    raw_data = json.loads((Path(__file__).parent / "fixtures" / "incidents.json").read_text())
    return [Scenario(**item) for item in raw_data]


@pytest.fixture
def run_workflow(mock_llm):
    """Configure scenario responses, then invoke a fresh real graph.

    Worker responses are selected by section. A barrier requires all workers to start 
    before any returns, detecting loss of parallel execution. The summary must 
    receive every worker's evidence.
    """
    async def run(scenario):
        responses = {
            graph.Classification: graph.Classification(**scenario["classification"]),
        }
        evidence = scenario.get("evidence", {})
        if evidence:
            responses[graph.Plan] = graph.Plan(sections=list(evidence))
        else:
            responses[graph.Route] = graph.Route(**scenario["route"])
            responses[graph.HandoffCheck] = graph.HandoffCheck(**scenario["handoff"])

        def structured(schema, **kwargs):
            assert schema in responses, f"Unexpected structured call: {schema.__name__}"
            return MagicMock(ainvoke=AsyncMock(return_value=responses[schema]))

        started = set()
        all_started = asyncio.Event()

        async def respond(prompt, **kwargs):
            if evidence:
                for section, finding in evidence.items():
                    if prompt.startswith(f"Investigate the '{section}' angle"):
                        assert section not in started, f"Duplicate worker: {section}"
                        started.add(section)
                        if len(started) == len(evidence):
                            all_started.set()
                        await asyncio.wait_for(all_started.wait(), timeout=3)
                        return AIMessage(content=finding)
                assert prompt.startswith("Summarize this incident investigation")
                assert started == set(evidence)
                for finding in evidence.values():
                    assert finding in prompt, "Synthesis is missing worker evidence"
            else:
                assert prompt.startswith("As the billing specialist")
            return AIMessage(content=scenario["resolution"])

        mock_llm.with_structured_output.side_effect = structured
        mock_llm.ainvoke.side_effect = respond
        state = {
            "ticket_id": scenario["ticket_id"],
            "ticket_text": scenario["ticket_text"],
            "category": None,
            "complexity": None,
            "messages": [],
            "findings": [],
            "plan_sections": [],
            "handoff_count": 0,
            "resolution": None,
            "report_key": None,
        }
        async with asyncio.timeout(10):
            return await graph.build_graph().compile().ainvoke(
                state, config={"recursion_limit": 12, "callbacks": []}
            )

    return run
