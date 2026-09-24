"""Fetch tools from MCP servers.

Two transports, chosen by MCP_MODE so the same code works in both places:

  stdio  tests and local dev - the server is spawned as a subprocess. No
         container, no port, no startup ordering to get wrong.
  http   deployed - the server is its own compose service on the shared
         network, so the process boundary between the agent and the systems it
         MUTATES is a real one.

MCP is optional everywhere. A domain with no mcp_servers() gets an empty list,
and a server that cannot be reached logs and is skipped rather than taking the
request down with it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from agentcore import aio
from langchain_core.tools import BaseTool

SERVER_MODULE = "mcp_servers"


def server_config(domain) -> dict[str, dict]:
    """What MultiServerMCPClient needs, for whichever transport is active."""
    declared = domain.mcp_servers()
    if not declared:
        return {}

    mode = os.getenv("MCP_MODE", "stdio").lower()
    config: dict[str, dict] = {}

    for name, spec in declared.items():
        if mode == "http":
            url = spec.get("url") or os.getenv("MCP_URL", "http://localhost:8100/mcp")
            config[name] = {"transport": "streamable_http", "url": url}
        else:
            config[name] = {
                "transport": "stdio",
                "command": sys.executable,
                # --domain is passed EXPLICITLY, never left to the inherited
                # environment. The subprocess loads its own domain, so without
                # this a caller that did not export DOMAIN gets a server for the
                # DEFAULT domain - which usually has no systems() at all, and
                # therefore silently returns zero tools rather than an error.
                "args": [
                    "-m", SERVER_MODULE,
                    "--server", spec.get("server", name),
                    "--domain", domain.name,
                ],
                "env": {**os.environ, "MCP_TRANSPORT": "stdio", "DOMAIN": domain.name},
                "cwd": str(Path(__file__).resolve().parents[3]),
            }
    return config


def load_mcp_tools(domain) -> list[BaseTool]:
    """Synchronous wrapper - the graph nodes are sync, the MCP client is not."""
    config = server_config(domain)
    if not config:
        return []

    from langchain_mcp_adapters.client import MultiServerMCPClient

    async def _fetch() -> list[BaseTool]:
        return await MultiServerMCPClient(config).get_tools()

    # The SHARED loop, not a throwaway one. Tools fetched here carry async
    # resources - an MCP session, a stdio subprocess transport - and they are
    # invoked later, from a different call. Fetching them on a loop that then
    # closes leaves those resources dead, and the failure surfaces inside a
    # tool call as "Event loop is closed", which reads as a flaky tool.
    return aio.run(_fetch(), timeout=60)
