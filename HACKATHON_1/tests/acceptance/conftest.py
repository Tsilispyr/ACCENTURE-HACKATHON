"""Harness for the acceptance tests.

The design constraint these tests set for themselves is the important part:
*assert actual tool calls and real evidence, not a prefilled final state*. So
this runs the real compiled graph through the real `LiveIncidentAdapter` against
the real tools and the real simulated estate. Only two things are substituted:

* **the LLM**, scripted per output schema, because a model's wording is not what
  is under test and a live call would make the result non-deterministic;
* **nothing else** -- tools are wrapped by a spy that records the call and then
  delegates to the genuine implementation, so evidence is real and every
  assertion about "was this tool called" is about a call that actually happened.

That split is what keeps these tests honest: they can prove data flowed from a
tool into a diagnosis, which is precisely what a prefilled state cannot.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from hackathon1 import adapters
from hackathon1.adapters import LiveIncidentAdapter
from hackathon1.graph import create_incident_app
from hackathon1.world import reset_world

#: Tools the adapter reaches for, by the name it holds them under.
SPIED_TOOLS = (
    "search_logs",
    "get_service_metrics",
    "search_knowledge_base",
    "get_incident_history",
    "scale_connection_pool",
    "restart_service",
    "rollback_change",
    "check_service_health",
)


class ToolCall:
    """One recorded tool invocation."""

    def __init__(self, name: str, payload: dict):
        self.name = name
        self.payload = payload

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"ToolCall({self.name}, {self.payload})"


class _SpyTool:
    """Records the call, then delegates to the genuine tool.

    Deliberately not a Mock with a return_value: the real tool runs, so the
    evidence flowing into the graph is the estate's actual data. A Mock here
    would let a test pass while asserting nothing about real behaviour.
    """

    def __init__(self, name, real, log, failures):
        self._name = name
        self._real = real
        self._log = log
        self._failures = failures

    def invoke(self, payload):
        self._log.append(ToolCall(self._name, payload))
        if self._name in self._failures:
            raise self._failures[self._name]
        return self._real.invoke(payload)


class ToolSpy:
    """Call log plus a switch for injecting failures at the tool boundary."""

    def __init__(self):
        self.calls: list[ToolCall] = []
        self.failures: dict[str, Exception] = {}

    def names(self) -> list[str]:
        return [call.name for call in self.calls]

    def called(self, name: str) -> bool:
        return name in self.names()

    def count(self, name: str) -> int:
        return self.names().count(name)

    def fail(self, name: str, error: Exception) -> None:
        """Make one tool raise, at the real boundary, for the rest of the test."""
        self.failures[name] = error


@pytest.fixture
def tool_spy(monkeypatch):
    spy = ToolSpy()
    for name in SPIED_TOOLS:
        real = getattr(adapters, name)
        monkeypatch.setattr(adapters, name, _SpyTool(name, real, spy.calls, spy.failures))
    return spy


class ScriptedLLM:
    """Scripts structured outputs per schema, consumed in order.

    A single queued value is reused for every subsequent call, so a test only
    has to enumerate the steps where the answer actually changes -- a replan,
    for instance, needs two plans and one of everything else.
    """

    def __init__(self, model):
        self._model = model
        self._queues: dict[type, list] = {}

    def set(self, schema: type, *values) -> None:
        self._queues[schema] = list(values)

    def _structured(self, schema, **_):
        queue = self._queues.get(schema)
        if not queue:
            raise AssertionError(
                f"unscripted structured LLM call for {schema.__name__}; "
                "script it in the test so the run stays deterministic"
            )
        value = queue.pop(0) if len(queue) > 1 else queue[0]
        return MagicMock(ainvoke=AsyncMock(return_value=value))

    def activate(self) -> None:
        self._model.with_structured_output.side_effect = self._structured


@pytest.fixture
def scripted_llm(mock_llm):
    """Scripted structured outputs. `mock_llm` already patched `llm` everywhere."""
    script = ScriptedLLM(mock_llm)
    yield script
    script.activate()


@pytest.fixture(autouse=True)
def clean_world():
    """Reset the simulated estate; remediation mutates it."""
    reset_world()
    yield
    reset_world()


@pytest.fixture
def run_incident(scripted_llm):
    """Compile a real incident app and run one incident through it.

    Returns (result, app, config) so a test can inspect the outcome and then
    resume the same thread -- which is how the approval tests continue a run
    that paused, rather than starting a second one.
    """

    async def run(incident_id="INC-TEST", service="payment-service", severity="high",
                  description="Payments failing; database connections at 100%.",
                  app=None, config=None):
        scripted_llm.activate()
        app = app or create_incident_app(LiveIncidentAdapter())
        config = config or {"configurable": {"thread_id": incident_id}, "callbacks": []}
        state = {
            "incident_id": incident_id,
            "service": service,
            "severity": severity,
            "description": description,
        }
        result = await app.ainvoke(state, config=config)
        return result, app, config

    return run
