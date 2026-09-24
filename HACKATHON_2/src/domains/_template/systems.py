"""TIME BUDGET: 40 minutes. The operations that CHANGE something.

These are served over MCP rather than in-process, so the boundary between the
agent and the systems it mutates is a real process boundary - which is
exactly where the risk gate and the approval step land.

Plain functions with type hints and a docstring. The MCP server turns them into
tools; a function without a docstring raises at startup, because the docstring
is what the model reads.

Every function here MUST also appear in policy.py's ACTION_RISK, or
test_domain_contract fails the build.
"""

from __future__ import annotations

from typing import Any, Callable

ACTIONS_TAKEN: list[str] = []


def do_the_thing(target: str) -> str:
    """One line on what this changes. State the consequence plainly.

    Return an operator RECEIPT of what was attempted - never a claim that the
    problem is solved. Whether it worked is decided by checking, separately,
    and not by the actor that made the change.
    """
    ACTIONS_TAKEN.append(f"did:{target}")
    return f"Action issued against {target}. Verify the result separately."


# Every mutating operation goes in this list. Empty list = no MCP server.
SYSTEMS: list[Callable[..., Any]] = [do_the_thing]
