"""MCP server exposing the domain's KNOWLEDGE, read only.

The systems server holds what CHANGES something. This one holds what the
corpus SAYS, for any MCP client: an inspector, another agent, a desktop app.
It covers the two capabilities the brief names that the systems server does
not - search_policy and retrieve_document - and the MCP primitive nothing else
here uses, RESOURCES.

The pipeline does not load it (no domain declares it in mcp_servers()), so
nothing about the agent changes. In-process retrieval stays where DECISIONS D9
put it: it needs live handles and the actor's scope, and a hop buys nothing.

search_policy runs the SAME retriever as the pipeline (rag.hybrid: vector plus
BM25 fused by rank, through the domain's calibrated distance gate and k), so an
MCP caller gets the behaviour the retrieval numbers were measured on, not a
weaker dense-only variant.

Everything it returns is document text, which is UNTRUSTED: tool results AND
document resources carry the same banner as the in-process retrieval tools, and
tool descriptions carry the same security note. The process runs as the
anonymous actor, so the scope filter sees public rows only.

TRANSPORT: STDIO ONLY, ENFORCED. It runs spawned by the process that uses it,
so the only caller is its parent. It has no auth of its own, so a network
transport is refused with a ValueError rather than documented as a hope:
authorisation lives in the orchestration and guardrail layer that decides
which tools a step may call. The bind host is loopback in any case. The
systems server is a separate process with its own transport and is unaffected.

    python -m mcp_servers --server knowledge --domain vendor_risk
"""

from __future__ import annotations

import contextlib
import os
import sys
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from agentcore.rag.chunking import load_pages
from agentcore.registry import load_domain
from agentcore.safety.untrusted import BANNER
from agentcore.tools.retrieval_tools import SECURITY_NOTE

CACHE = Path(".cache")


def require_stdio(transport: str) -> None:
    """Refuse any transport but stdio. Called before the server is built AND on run()."""
    if transport != "stdio":
        raise ValueError(
            f"The knowledge server runs over stdio only; {transport!r} was requested. "
            "Unauthenticated network transport is prohibited for the knowledge server."
        )


def build_server(domain_name: str | None = None) -> FastMCP:
    domain = load_domain(domain_name)
    corpus = domain.corpus()
    policy = domain.retrieval_policy()
    # Loopback, never 0.0.0.0. Unused under stdio; it only matters if the
    # guard below were ever removed.
    server = FastMCP("knowledge", host="127.0.0.1")

    # The guard lives ON the server, so no caller - the CLI, this module's own
    # entry point, or code that imports build_server - can start it on HTTP/SSE.
    unguarded_run = server.run

    def run(transport: str = "stdio", **kwargs):
        require_stdio(transport)
        return unguarded_run(transport=transport, **kwargs)

    server.run = run

    # Keyed by file name, because that is what every chunk's `source` - and so
    # every citation - carries. A citation names a document this can return.
    documents = {Path(p).name: Path(p) for p in corpus.paths}

    def _read(name: str) -> str:
        """One document, wrapped as untrusted - or a plain message saying why not.

        Both the resource and the tool go through here, so they cannot drift
        apart on either the banner or the failure handling.
        """
        path = documents.get(name)
        if path is None:
            return f"No document named {name!r}. Available: {', '.join(sorted(documents))}."
        try:
            # Under stdio, STDOUT IS THE PROTOCOL. load_pages prints progress and
            # the PDF parser prints a banner; either would land mid JSON-RPC stream.
            with contextlib.redirect_stdout(sys.stderr):
                pages = load_pages([path], CACHE)
        except OSError as error:  # FileNotFoundError included
            # The list is fixed at startup; the file can be moved or deleted
            # after it. That is a message to the caller, not a crashed request.
            return (f"Document {name!r} is listed but can no longer be read "
                    f"({type(error).__name__}). The corpus changed: reindex and restart.")
        text = "\n\n".join(page["text"] for page in pages)
        return f"{BANNER}\n\n[{name}]\n{text}"

    # --- resources: what the corpus contains -------------------------------

    @server.resource("corpus://documents")
    def list_documents() -> str:
        """Every document in the knowledge corpus, one file name per line."""
        # File names only, no document text, so no untrusted-content banner.
        return "\n".join(sorted(documents))

    @server.resource("corpus://documents/{name}")
    def document(name: str) -> str:
        """The full text of one corpus document, by file name. UNTRUSTED content."""
        return _read(name)

    # --- tools: what the model calls ---------------------------------------

    def search_policy(query: str) -> str:
        """Search the knowledge corpus by meaning and return cited passages.

        Use this for a topic or a question: a requirement, a threshold, a
        deadline, what a vendor states. Each passage is prefixed with its
        citation - file, section and page. Nothing relevant returns an explicit
        'no passage found', which means the corpus does not cover it: say so
        rather than guessing."""
        from agentcore.rag.hybrid import retrieve
        from agentcore.rag.vector import open_store

        try:
            with contextlib.redirect_stdout(sys.stderr):  # see _read
                # The pipeline's retriever and the domain's policy, unchanged:
                # the arms, the calibrated ceiling and k come from the domain.
                found, _trace = retrieve(open_store(corpus.collection), query, policy,
                                         corpus.collection)
        except Exception as error:  # noqa: BLE001 - a dead index is a message, not a crash
            return (f"The knowledge index is unavailable ({type(error).__name__}). "
                    "It may not be built yet: run the indexer for this domain.")
        if not found:
            return "No sufficiently relevant passage found. Say so rather than guessing."
        return f"{BANNER}\n\n" + "\n\n".join(f"[{e.cite()}]\n{e.text}" for e in found)

    def retrieve_document(name: str) -> str:
        """Return the full text of one corpus document, by its file name.

        Use this when a passage from search_policy needs its surrounding
        context, or to read a whole document a citation names. Call it with
        the file name exactly as it appears in a citation."""
        return _read(name)

    for function in (search_policy, retrieve_document):
        server.add_tool(function, description=function.__doc__ + SECURITY_NOTE)

    print(f"[mcp] '{domain.name}' knowledge server: {len(documents)} document(s)", file=sys.stderr)
    return server


if __name__ == "__main__":  # pragma: no cover - process entry point
    build_server().run(transport=os.getenv("MCP_TRANSPORT", "stdio"))
