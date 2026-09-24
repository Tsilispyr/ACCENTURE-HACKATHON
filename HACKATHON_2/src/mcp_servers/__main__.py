"""python -m mcp_servers --server systems [--transport stdio|streamable-http]"""

from __future__ import annotations

import argparse
import os

from mcp_servers.systems_server import build_server

SERVERS = {"systems": build_server}

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--server", default="systems", choices=sorted(SERVERS))
parser.add_argument("--domain", default=None)
parser.add_argument("--transport", default=os.getenv("MCP_TRANSPORT", "stdio"),
                    choices=["stdio", "sse", "streamable-http"])
args = parser.parse_args()

SERVERS[args.server](args.domain).run(transport=args.transport)
