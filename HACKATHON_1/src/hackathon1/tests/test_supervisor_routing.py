from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from langgraph.types import Command

from hackathon1.graph import HandoffCheck, Route, billing_agent, supervisor


# POSITIVE TEST: supervisor routes to the specialist the (mocked) LLM picks
async def test_supervisor_routes_to_billing():
    fake_route = Route(agent="billing_agent")
    with patch("hackathon1.graph.llm") as mock_llm:
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=fake_route)
        result = await supervisor({"ticket_text": "double charge"})
    assert isinstance(result, Command)
    assert result.goto == "billing_agent"


# NEGATIVE TEST / safety: handoff_count already at MAX_HANDOFFS blocks a
# further handoff even if the mocked LLM says one is needed -- the
# regression test for graph.py's cycle guard (see its module docstring on
# why day04/sessionb/swarm.py's unguarded pattern is avoided here).
async def test_specialist_does_not_handoff_past_the_cap():
    fake_check = HandoffCheck(needs_other_specialist=True, other_specialist="tech_agent")
    with patch("hackathon1.graph.llm") as mock_llm:
        mock_llm.ainvoke = AsyncMock(return_value=SimpleNamespace(content="Here's your billing answer."))
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=fake_check)
        state = {"ticket_text": "double charge and app crash", "handoff_count": 1, "resolution": None}
        result = await billing_agent(state)
    assert result.goto == "__end__"
