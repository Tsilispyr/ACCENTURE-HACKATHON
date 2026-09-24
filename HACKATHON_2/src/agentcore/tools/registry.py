"""Assemble the tools a step is allowed to use.

The per-step allowlist is the strongest guardrail in the system, and the
cheapest: a tool that is not bound cannot be called, however persuasive the
text asking for it. Everything else - pattern matching, prompts, envelopes --
discourages. This prevents.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any

from langchain_core.tools import BaseTool, StructuredTool
from pydantic import BaseModel, ConfigDict

from agentcore.contracts import RISK_ORDER, PlanStep, RiskLevel
from agentcore.safety.risk import risk_of
from agentcore.world import current_actor

# Handout section 9: "Restrict sensitive MCP tools according to role/authorization".
# Each role may call tools up to a ceiling on the SAME low/medium/high scale the
# risk gate already uses, so there is no second per-tool table to keep in step
# with the first. A role this table does not name - "anonymous", a typo, a role
# added later and forgotten here - gets the lowest ceiling: an unknown caller
# must never gain access by being unlisted.
#
# WHAT THE HANDOUT ACTUALLY REQUIRES. Section 9 says one thing about this:
# "Restrict sensitive MCP tools according to role/authorization." It names no
# roles and defines no per-role permissions, so the tiers below are our design,
# and "sensitive" has to be read for what it plainly means: the tools that
# CHANGE something. record_assessment, submit_for_signoff and raise_exception
# write; search_policy, get_vendor_history and calculate_tco do not.
#
# So a user may USE the system fully and may not MODIFY anything. Everything
# up to medium - retrieval, history, totals, budget - is theirs. The writes are
# not, and a plan that needs one has that step skipped with the reason shown
# while every other step still runs (see s5_gate).
#
# AN EARLIER TABLE PUT USERS AT LOW, and it was measured wrong twice over.
# calculate_tco and get_budget are medium, so the commercial reviewer received
# a denial stand-in and its finding rested on nothing - on the console, the
# chat UI and the demo accounts alike, while agent_eval ran as admin and no
# number moved (PROBLEMS P60). It also taught the wrong lesson: a default that
# cannot do the job trains people to pick admin, which hands them the write
# tools they never needed.
#
# engineer and procurement are the same tier. They are labels this system
# already issues - the console, the chat UI, alice and bob - not separate
# privilege levels, and listing them stops a silent demotion to the floor.
ROLE_MAX_RISK: dict[str, RiskLevel] = {
    "user": "medium",
    "engineer": "medium",
    "procurement": "medium",
    "admin": "high",
}

# The floor for a role nobody listed: read-only. Being unrecognised must never
# be a way in, and it must never be a way to a WRITE.
LOWEST_CEILING: RiskLevel = "low"


class _AnyArgs(BaseModel):
    model_config = ConfigDict(extra="allow")


def role_ceiling(role: str) -> RiskLevel:
    return ROLE_MAX_RISK.get(role, LOWEST_CEILING)


def steps_above_role(plan: Any, floors: dict[str, RiskLevel], role: str) -> list[tuple[PlanStep, RiskLevel]]:
    """The plan's steps whose tool this role may not use, with each tool's risk.

    Judged on the tool's own floor, exactly as `restrict_by_role` does, so the
    gate and the executor can never disagree about what a role may run.
    """
    ceiling = role_ceiling(role)
    out: list[tuple[PlanStep, RiskLevel]] = []
    for step in plan.steps:
        if not step.tool_hint:
            continue
        risk = risk_of(step.tool_hint, floors)
        if RISK_ORDER[risk] > RISK_ORDER[ceiling]:
            out.append((step, risk))
    return out


def _denied(tool: BaseTool, role: str, risk: RiskLevel, ceiling: RiskLevel) -> BaseTool:
    """Same name and arguments, but calling it does nothing and says why.

    Removing the tool instead would leave the executor to fail with "unknown
    tool" and the reader to guess at the cause. A stand-in keeps the run alive
    and puts the reason in the trace, where a person watching can see it.
    """
    message = (
        f"Denied: role '{role}' may use tools up to {ceiling} risk, and "
        f"'{tool.name}' is {risk} risk. Nothing was executed."
    )

    def refuse(**_arguments: Any) -> str:
        return message

    async def refuse_async(**_arguments: Any) -> str:
        return message

    return StructuredTool(
        name=tool.name,
        description=f"{tool.description}\n\n(Not available to role '{role}': calling it returns a denial.)",
        args_schema=tool.args_schema or _AnyArgs,
        func=refuse,
        coroutine=refuse_async,
        metadata={"denied_for_role": role, "risk": risk},
    )


def restrict_by_role(
    tools: list[BaseTool], floors: dict[str, RiskLevel], role: str
) -> list[BaseTool]:
    """Swap every tool above this role's ceiling for a denial."""
    ceiling = role_ceiling(role)
    out: list[BaseTool] = []
    for tool in tools:
        risk = risk_of(tool.name, floors)
        if RISK_ORDER[risk] > RISK_ORDER[ceiling]:
            out.append(_denied(tool, role, risk, ceiling))
        else:
            out.append(tool)
    return out


@lru_cache(maxsize=8)
def _all_tools(domain_name: str) -> tuple[BaseTool, ...]:
    """Every tool this domain offers: retrieval, local, and MCP."""
    from agentcore.registry import load_domain
    from agentcore.tools.retrieval_tools import build_retrieval_tools

    domain = load_domain(domain_name)
    tools: list[BaseTool] = list(build_retrieval_tools(domain))
    tools.extend(domain.local_tools())

    # MCP tools are fetched once and cached: each call spawns a subprocess or
    # opens an HTTP session, which is far too expensive per step.
    try:
        from agentcore.tools.mcp_client import load_mcp_tools

        tools.extend(load_mcp_tools(domain))
    except Exception as error:  # noqa: BLE001 - MCP is optional, never fatal
        print(f"  [tools] MCP unavailable, continuing without it: {type(error).__name__}: {error}")

    return tuple(tools)


def tools_for_step(domain, step: PlanStep) -> list[BaseTool]:
    """Only what this step declared.

    A step with no tool_hint gets the read-only retrieval tools, so it can still
    look something up without being able to change anything.

    Then the caller's ROLE is applied on top: the step declaring a tool is not
    enough if the person who started the run may not use it. The role comes from
    the actor bound for this run (world.bound), the same source that scopes
    retrieval, so every entry point - API, Chainlit, evaluation - supplies it.
    """
    available = {t.name: t for t in _all_tools(domain.name)}

    if step.tool_hint and step.tool_hint in available:
        allowed = [available[step.tool_hint]]
        # Retrieval is always safe to include alongside a named tool.
        allowed += [t for n, t in available.items()
                    if n in {"search_corpus", "get_by_locator"} and n != step.tool_hint]
    else:
        allowed = [t for n, t in available.items() if n in {"search_corpus", "get_by_locator"}]

    return restrict_by_role(allowed, domain.action_risk(), current_actor().role)


def all_tool_names(domain) -> list[str]:
    return [t.name for t in _all_tools(domain.name)]
