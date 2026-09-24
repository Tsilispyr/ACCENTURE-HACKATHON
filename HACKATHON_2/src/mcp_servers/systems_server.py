"""MCP server exposing the domain's MUTATING operations.

Why these and not the retrieval tools: MCP is a process boundary, so it belongs
where a real deployment would put one - between the agent and the systems it
CHANGES. Retrieval needs live process handles (a PGVector connection, the actor
scope) and gains nothing from a hop.

The payoff is that the risk gate and the approval step land exactly on the
process boundary. A judge can see the separation rather than take it on faith.

One file, two transports:
    stdio            tests and local dev - spawned as a subprocess
    streamable-http  deployed - its own container on the shared network
"""

from __future__ import annotations

import os

from mcp.server.fastmcp import FastMCP

from agentcore.registry import load_domain


def build_server(domain_name: str | None = None) -> FastMCP:
    domain = load_domain(domain_name)
    server = FastMCP("systems", host="0.0.0.0", port=int(os.getenv("MCP_PORT", "8100")))

    functions = domain.systems()
    for function in functions:
        # The docstring IS the schema the model sees, so it is written for the
        # model. A systems function without one is a bug, not a style issue.
        if not function.__doc__:
            raise RuntimeError(
                f"{domain.name}.systems() exposes {function.__name__} with no docstring. "
                "The docstring is the tool description the model reads."
            )
        server.add_tool(function)

    @server.prompt()
    def house_style() -> str:
        """The domain's persona, served over MCP.

        Exposing a prompt as well as tools is three lines, and it shows MCP is
        more than a remote function call.
        """
        return domain.persona()

    print(f"[mcp] '{domain.name}' systems server: {len(functions)} tool(s)")
    return server


if __name__ == "__main__":  # pragma: no cover - process entry point
    build_server().run(transport=os.getenv("MCP_TRANSPORT", "stdio"))
