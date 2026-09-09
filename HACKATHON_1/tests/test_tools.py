"""Same positive/negative shape as docerz/test_agent.py and
day03/tests/test_ticket_service.py."""

import pytest

from hackathon1.tools import check_known_issue, lookup_sla_hours


# POSITIVE TEST: known priority returns its SLA hours
def test_lookup_sla_hours_known_priority():
    assert lookup_sla_hours.invoke({"priority": "critical"}) == 1


# POSITIVE TEST: lookup is case-insensitive
def test_lookup_sla_hours_case_insensitive():
    assert lookup_sla_hours.invoke({"priority": "HIGH"}) == 4


# NEGATIVE TEST: unknown priority raises ValueError
def test_lookup_sla_hours_unknown_priority():
    with pytest.raises(ValueError, match="not a known priority"):
        lookup_sla_hours.invoke({"priority": "urgent"})


# POSITIVE TEST: matching keyword returns the known-issue note
def test_check_known_issue_match():
    result = check_known_issue.invoke({"keyword": "double charge"})
    assert "INC-4821" in result


# NEGATIVE TEST: no match returns the fallback message, not an error
def test_check_known_issue_no_match():
    result = check_known_issue.invoke({"keyword": "unrelated topic"})
    assert "No matching known issue" in result
