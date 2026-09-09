"""Shared offline fixtures.

The CodeHub scenario fixtures (Scenario, scenarios, run_workflow) were removed
with the CodeHub graph itself. The incident workflow is covered directly by
tests/test_incident_workflow.py, which drives the real compiled graph with a
deterministic adapter and needs no scenario replay harness.
"""

import socket
from unittest.mock import AsyncMock, MagicMock, create_autospec

import pytest

from hackathon1 import adapters, graph, service, storage
from tests.tool_interfaces import ToolInterfaces


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
