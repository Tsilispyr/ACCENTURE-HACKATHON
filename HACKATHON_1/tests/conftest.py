"""Shared offline dependencies and the adapter between scenarios and graph code."""

import asyncio
import json
import socket
from pathlib import Path
from pydantic import BaseModel, Field
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest
from langchain_core.messages import AIMessage

from hackathon1 import adapters, graph, service, storage
from tests.tool_interfaces import ToolInterfaces


class Scenario(BaseModel):
    ticket_id: str
    ticket_text: str
    classification: dict = Field(default_factory=dict)
    evidence: dict = Field(default_factory=dict)
    route: dict = Field(default_factory=dict)
    handoff: dict = Field(default_factory=dict)
    resolution: str


@pytest.fixture
def tool_mocks():
    """Autospec mocks of the real tools, each failing loudly until configured.

    The names and arity come from `ToolInterfaces`, which mirrors
    `hackathon1.tools`, so a call the real tool would reject fails here too.
    Deliberately no successful defaults: a mock that invents a plausible return
    value lets a test pass while asserting nothing about real behaviour.
    """
    mocks = create_autospec(ToolInterfaces, instance=True, spec_set=True)
    for name in (
        "search_logs",
        "get_service_metrics",
        "search_knowledge_base",
        "get_incident_history",
        "scale_connection_pool",
        "restart_service",
        "rollback_change",
        "check_service_health",
    ):
        getattr(mocks, name).side_effect = NotImplementedError(
            f"configure {name} with scenario data before using this fixture"
        )
    return mocks


# Temporary for early testing, remove later
@pytest.fixture(autouse=True)
def offline_dependencies(monkeypatch):
    """Disable tracing and fail promptly on accidental network access."""
    # Loopback must stay allowed. On Windows the asyncio event loop builds its
    # self-pipe from a real socket pair on 127.0.0.1, so a blanket connect block
    # does not catch a rogue API call -- it breaks asyncio itself, and every
    # async test in the suite errors with "attempted network access" pointing at
    # a 127.0.0.1 ephemeral port. TestClient and any local stub need it too.
    # Blocking only non-loopback keeps the intent: nothing leaves this machine.
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_create_connection = socket.create_connection

    def _is_loopback(address):
        if isinstance(address, (tuple, list)) and address:
            host = str(address[0])
            return host in {"127.0.0.1", "::1", "localhost", ""} or host.startswith("127.")
        return False  # AF_UNIX paths and the like are not remote either, but rare here

    def _guard_method(real):
        def wrapper(self, address, *args, **kwargs):
            if not _is_loopback(address):
                raise AssertionError(
                    f"Offline test attempted network access to {address!r}; mock the dependency"
                )
            return real(self, address, *args, **kwargs)
        return wrapper

    def _guard_create_connection(address, *args, **kwargs):
        if not _is_loopback(address):
            raise AssertionError(
                f"Offline test attempted network access to {address!r}; mock the dependency"
            )
        return real_create_connection(address, *args, **kwargs)

    monkeypatch.setattr(socket.socket, "connect", _guard_method(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", _guard_method(real_connect_ex))
    monkeypatch.setattr(socket, "create_connection", _guard_create_connection)
    monkeypatch.setattr(service, "get_callback_handlers", lambda: [])
    monkeypatch.setenv("LANGCHAIN_TRACING_V2", "false")
    monkeypatch.setenv("LANGSMITH_TRACING", "false")


@pytest.fixture(autouse=True)
def mock_llm(monkeypatch):
    """Mock LLM instead of calling real models.

    The modules do `from hackathon1.llm import llm`, so each one holds its own
    reference to the same object and there is no `get_llm()` accessor to patch.
    An earlier version patched `graph.get_llm`, which does not exist -- and
    because this fixture is autouse, that single AttributeError failed every
    test in the suite at setup.

    Patching the name in each importing module (rather than
    `hackathon1.llm.llm`) is what actually works: the modules already bound the
    original object at import time, so rebinding the source module afterwards
    would not reach them.
    """
    model = MagicMock(spec=["ainvoke", "with_structured_output"])
    model.ainvoke = AsyncMock(side_effect=AssertionError("Configure the LLM response"))
    model.with_structured_output.return_value.ainvoke = AsyncMock(
        side_effect=AssertionError("Configure the structured LLM response")
    )
    for module in (graph, service, adapters):
        if hasattr(module, "llm"):
            monkeypatch.setattr(module, "llm", model)
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
    """Scenario fixtures, keyed by name ("simple", "complex").

    incidents.json is a mapping, and the tests index it as one
    (`scenarios["simple"]`), so the raw dicts are what get returned. An earlier
    version did `[Scenario(**item) for item in raw_data]`, which iterates a dict
    and yields its *keys* -- hence "argument after ** must be a mapping, not str".

    Each entry is still validated through the Scenario model, so a malformed
    fixture fails here with a clear pydantic error rather than as a confusing
    KeyError somewhere inside a workflow test.
    """
    raw_data = json.loads((Path(__file__).parent / "fixtures" / "incidents.json").read_text())
    for name, item in raw_data.items():
        Scenario(**item)  # validation only; tests want plain dicts
    return raw_data


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
