"""Assemble the tools a step is allowed to use.

The per-step allowlist is the strongest guardrail in the system, and the
cheapest: a tool that is not bound cannot be called, however persuasive the
text asking for it. Everything else - pattern matching, prompts, envelopes --
discourages. This prevents.
"""

from __future__ import annotations

from functools import lru_cache

from langchain_core.tools import BaseTool

from agentcore.contracts import PlanStep


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
    """
    available = {t.name: t for t in _all_tools(domain.name)}

    if step.tool_hint and step.tool_hint in available:
        allowed = [available[step.tool_hint]]
        # Retrieval is always safe to include alongside a named tool.
        allowed += [t for n, t in available.items()
                    if n in {"search_corpus", "get_by_locator"} and n != step.tool_hint]
        return allowed

    return [t for n, t in available.items() if n in {"search_corpus", "get_by_locator"}]


def all_tool_names(domain) -> list[str]:
    return [t.name for t in _all_tools(domain.name)]
