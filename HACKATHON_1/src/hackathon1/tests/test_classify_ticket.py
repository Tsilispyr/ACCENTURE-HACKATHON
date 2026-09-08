"""LLM always mocked -- never call a real API in a unit test, per
notes/02-METHODOLOGY.md section 5. asyncio_mode = "auto" (pyproject.toml)
means these `async def test_...` functions run with no
@pytest.mark.asyncio decorator needed."""

from unittest.mock import AsyncMock, patch

import pytest

from hackathon1.graph import Classification, classify_ticket


# POSITIVE TEST: valid ticket text gets classified from the (mocked) LLM
async def test_classify_ticket_returns_category_and_complexity():
    fake_result = Classification(category="billing", complexity="simple")
    with patch("hackathon1.graph.llm") as mock_llm:
        mock_llm.with_structured_output.return_value.ainvoke = AsyncMock(return_value=fake_result)
        result = await classify_ticket({"ticket_id": "T-1", "ticket_text": "I was charged twice"})
    assert result == {"category": "billing", "complexity": "simple"}


# NEGATIVE TEST: empty ticket_text is rejected before any LLM call
async def test_classify_ticket_rejects_empty_text():
    with pytest.raises(ValueError, match="ticket_text must not be empty"):
        await classify_ticket({"ticket_id": "T-1", "ticket_text": "   "})
