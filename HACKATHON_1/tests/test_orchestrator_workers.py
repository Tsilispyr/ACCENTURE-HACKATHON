from types import SimpleNamespace
from unittest.mock import AsyncMock

from langgraph.types import Send

from hackathon1.graph import Plan, dispatch_sections, investigate_section, orchestrator


# POSITIVE TEST: orchestrator writes plan_sections from the (mocked) LLM's plan
async def test_orchestrator_writes_plan_sections(mock_llm):
    fake_plan = Plan(sections=["billing history", "recent charges"])
    mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=fake_plan)
    result = await orchestrator({"ticket_text": "double charge"})
    assert result == {"plan_sections": ["billing history", "recent charges"]}


# POSITIVE TEST: dispatch_sections returns one Send per planned section --
# no LLM call, pure function, testable without a graph compile.
def test_dispatch_sections_returns_one_send_per_section():
    state = {"ticket_text": "double charge", "plan_sections": ["a", "b", "c"]}
    sends = dispatch_sections(state)
    assert len(sends) == 3
    assert all(isinstance(s, Send) and s.node == "investigate_section" for s in sends)
    assert [s.arg["section"] for s in sends] == ["a", "b", "c"]


# POSITIVE TEST: investigate_section produces one finding per worker
async def test_investigate_section_returns_one_finding(mock_llm):
    mock_llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content="Looked normal."))
    result = await investigate_section({"ticket_text": "double charge", "section": "billing history"})
    assert len(result["findings"]) == 1
    assert "billing history" in result["findings"][0]
