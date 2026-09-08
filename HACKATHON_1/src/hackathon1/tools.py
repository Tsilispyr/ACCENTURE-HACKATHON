"""CodeHub domain tools, bound to the lightweight GET /chat agent in
service.py. Same @tool + docstring-as-schema shape as docerz/day21's
calculate_calculator, but tied to this project's own business domain
instead of a generic calculator."""

from langchain_core.tools import tool

_SLA_HOURS = {"critical": 1, "high": 4, "medium": 24, "low": 72}

_KNOWN_ISSUES = {
    "double charge": "Known billing sync bug (INC-4821) -- a fix is scheduled; "
    "a manual refund can be issued in the meantime.",
    "app crash": "Known crash on the billing settings page (INC-4790) on iOS 18 "
    "-- a client update is in progress.",
}


@tool
def lookup_sla_hours(priority: str) -> int:
    """Look up the SLA response time (in hours) for a priority level: critical, high, medium, or low."""
    hours = _SLA_HOURS.get(priority.lower())
    if hours is None:
        raise ValueError(f"{priority!r} is not a known priority -- use critical, high, medium, or low")
    return hours


@tool
def check_known_issue(keyword: str) -> str:
    """Check whether a keyword matches a known, already-tracked CodeHub issue."""
    keyword_lower = keyword.lower()
    for key, note in _KNOWN_ISSUES.items():
        if key in keyword_lower or keyword_lower in key:
            return note
    return "No matching known issue found -- treat as new."
