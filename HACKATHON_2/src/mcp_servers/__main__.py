"""python -m mcp_servers --server systems|knowledge [--transport stdio|streamable-http]

The knowledge server is stdio only; asking it for a network transport fails.
"""

from __future__ import annotations

import argparse
import os

from mcp_servers.knowledge_server import build_server as build_knowledge
from mcp_servers.knowledge_server import require_stdio
from mcp_servers.systems_server import build_server

SERVERS = {"systems": build_server, "knowledge": build_knowledge}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--server", default="systems", choices=sorted(SERVERS))
parser.add_argument("--domain", default=None)
parser.add_argument("--transport", default=os.getenv("MCP_TRANSPORT", "stdio"),
                    choices=["stdio", "sse", "streamable-http"])
args = parser.parse_args()

# Fail before loading a domain or reading a corpus. MCP_TRANSPORT is set to
# streamable-http inside the mcp-systems container, so this is not hypothetical.
if args.server == "knowledge":
    require_stdio(args.transport)

SERVERS[args.server](args.domain).run(transport=args.transport)
