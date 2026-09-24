"""Offline test fixtures.

Everything here exists so `pytest -m workflow` runs with NO network, NO
database and NO API keys - on a laptop, in CI, on a fork. A test suite that
needs credentials is a test suite that stops being run.

Two substitutions do the work:

  ScriptedLLM  queues structured outputs per schema, so a graph that makes four
               different structured calls can be driven deterministically.
  FakeStore    keyword-overlap "retrieval" over an in-memory corpus, with the
               same method signatures PGVector exposes.
"""

from __future__ import annotations

from typing import Any

import pytest

from agentcore.contracts import Actor

# --------------------------------------------------------------- the LLM ---


class ScriptedLLM:
    """A stand-in for AzureChatOpenAI that returns queued answers.

    Queue by schema CLASS for structured calls, and a list of strings for plain
    ones. A single queued value is reused for later calls of the same schema,
    so a test only has to script what it cares about.
    """

    def __init__(self) -> None:
        self.structured: dict[type, list[Any]] = {}
        self.plain: list[str] = []
        self.calls: list[tuple[str, str]] = []

    def queue(self, schema: type, *values: Any) -> "ScriptedLLM":
        self.structured.setdefault(schema, []).extend(values)
        return self

    def queue_text(self, *values: str) -> "ScriptedLLM":
        self.plain.extend(values)
        return self

    # --- the AzureChatOpenAI surface the pipeline actually uses ------------
    def with_structured_output(self, schema: type, **_: Any) -> "._Bound":  # noqa: F821
        return _Bound(self, schema)

    def invoke(self, prompt: Any, **_: Any) -> Any:
        self.calls.append(("plain", str(prompt)[:200]))
        text = self.plain.pop(0) if self.plain else "scripted reply"

        class _Message:
            content = text

        return _Message()


class _Bound:
    def __init__(self, parent: ScriptedLLM, schema: type) -> None:
        self.parent, self.schema = parent, schema

    def invoke(self, prompt: Any, **_: Any) -> Any:
        self.parent.calls.append((self.schema.__name__, str(prompt)[:200]))
        queued = self.parent.structured.get(self.schema)
        if not queued:
            raise AssertionError(
                f"ScriptedLLM has no queued {self.schema.__name__}. "
                f"Add: llm.queue({self.schema.__name__}, ...)"
            )
        # Keep the last value so repeated calls of one schema need one entry.
        return queued.pop(0) if len(queued) > 1 else queued[0]


# ------------------------------------------------------------- the store ---


# FakeStore lives in domains/deterministic/store.py, not here: the CI eval gate
# imports it too, and a test-only copy would drift from the one CI exercises.
from domains.deterministic.store import FakeStore  # noqa: E402


# ------------------------------------------------------------- fixtures ----


@pytest.fixture
def llm(monkeypatch) -> ScriptedLLM:
    """Replace every model handle with one scripted instance."""
    import agentcore.llm as llm_module

    scripted = ScriptedLLM()
    for name in ("chat_model", "judge_model"):
        getattr(llm_module, name).cache_clear()
        monkeypatch.setattr(llm_module, name, lambda _s=scripted: _s)

    # The stages import the function by name, so patch it there too.
    for module in ("s2_guard_in", "s3_ground", "s4_plan", "s6_act", "s7_replan", "s8_compose"):
        target = f"agentcore.pipeline.{module}"
        monkeypatch.setattr(f"{target}.chat_model", lambda _s=scripted: _s, raising=False)
    return scripted


class ScriptedDeepAgent:
    """A stand-in for `create_deep_agent`, recording how it was BUILT.

    WHY THIS EXISTS. Until it did, no offline test ever reached a deep agent.
    `s6_act` calls `create_deep_agent(chat_model(), ...)`, and in tests
    `chat_model()` is a ScriptedLLM, which is not a BaseChatModel - so
    `resolve_model` raised, the `except` in `s6_act` caught it, and EVERY step
    recorded `step_failed`. 234 tests passed with that, and one of them relied
    on it. The consequence: the code path that delegates to a specialist had
    never executed under test, which is the real reason delegation shipped
    measuring 0.00.

    Two halves:
      .built    one dict per construction - tools, system_prompt, subagents.
                Assertions about what a specialist may REACH read this.
      queue_*   what the agent returns. Messages are real AIMessage objects
                with real tool_calls, so the fixture cannot drift from the
                shape `s6_act` actually parses.
    """

    def __init__(self) -> None:
        self.built: list[dict[str, Any]] = []
        self.results: list[Any] = []

    # --- scripting ---------------------------------------------------------
    def queue_result(self, *results: Any) -> "ScriptedDeepAgent":
        self.results.extend(results)
        return self

    def queue_delegation(self, subagent_type: str, text: str = "Reviewed.",
                         **extra: Any) -> "ScriptedDeepAgent":
        """The common case: the agent hands the step to one specialist."""
        return self.queue_result({
            "messages": [
                _ai_message(text, [{"name": "task", "id": "call-1", "args": {
                    "subagent_type": subagent_type, "description": text,
                }}]),
            ],
            **extra,
        })

    def queue_tool_calls(self, *names: str, text: str = "Done.",
                         **extra: Any) -> "ScriptedDeepAgent":
        calls = [{"name": n, "id": f"call-{i}", "args": {}} for i, n in enumerate(names)]
        return self.queue_result({"messages": [_ai_message(text, calls)], **extra})

    def raise_next(self, error: Exception) -> "ScriptedDeepAgent":
        """The specialist failure path: a subagent that blows up mid-step."""
        return self.queue_result(error)

    # --- the create_deep_agent surface -------------------------------------
    def __call__(self, model: Any = None, tools: Any = None, **kwargs: Any) -> Any:
        self.built.append({
            "model": model,
            "tools": list(tools or []),
            "system_prompt": kwargs.get("system_prompt", ""),
            "subagents": kwargs.get("subagents") or [],
        })
        return _BoundAgent(self)

    @property
    def last(self) -> dict[str, Any]:
        assert self.built, "no deep agent was constructed"
        return self.built[-1]


class _BoundAgent:
    def __init__(self, parent: ScriptedDeepAgent) -> None:
        self.parent = parent

    async def ainvoke(self, _payload: Any, **_kwargs: Any) -> Any:
        # async because `_run_agent` goes through `aio.run`.
        if not self.parent.results:
            return {"messages": [_ai_message("scripted reply", [])]}
        result = self.parent.results.pop(0) if len(self.parent.results) > 1 \
            else self.parent.results[0]
        if isinstance(result, Exception):
            raise result
        return result


def _ai_message(text: str, tool_calls: list[dict[str, Any]]):
    """A REAL AIMessage, so the tool_call shape is validated by langchain."""
    from langchain_core.messages import AIMessage

    return AIMessage(content=text, tool_calls=tool_calls)


@pytest.fixture
def deep_agent(monkeypatch) -> ScriptedDeepAgent:
    """Opt-in. Without it, tests keep today's behaviour (every step fails)."""
    import deepagents

    scripted = ScriptedDeepAgent()
    monkeypatch.setattr(deepagents, "create_deep_agent", scripted)
    return scripted


@pytest.fixture
def store(monkeypatch) -> FakeStore:
    """Replace the vector store everywhere it is opened."""
    fake = FakeStore()
    monkeypatch.setattr("agentcore.rag.vector.open_store", lambda *a, **k: fake)
    monkeypatch.setattr("agentcore.pipeline.s3_ground.open_store", lambda *a, **k: fake)
    monkeypatch.setattr("agentcore.tools.retrieval_tools.open_store", lambda *a, **k: fake)
    return fake


@pytest.fixture
def domain(monkeypatch):
    """Force the deterministic domain and reset its fake estate."""
    import domains.deterministic as det
    from agentcore.registry import load_domain

    det.reset()
    load_domain.cache_clear()
    monkeypatch.setenv("DOMAIN", "deterministic")

    from agentcore.tools.registry import _all_tools

    _all_tools.cache_clear()
    return det.DOMAIN


@pytest.fixture
def actor() -> Actor:
    return Actor(id="tester", email="tester@example.com", role="engineer", scope="public")


@pytest.fixture(autouse=True)
def _tests_run_as_admin_unless_they_say_otherwise():
    """Tools are gated by the bound actor's ROLE (tools/registry.py).

    Most tests predate that and exercise write tools like restart_service, so
    they run as admin. Tests about the role gate itself bind their own actor
    with `world.bound(...)`, which nests inside this one and wins.
    """
    from agentcore.world import bound

    with bound(Actor(id="test-default", role="admin", scope="public")):
        yield
