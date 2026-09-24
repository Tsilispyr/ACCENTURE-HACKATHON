"""The knowledge MCP server: resources, tools, and a dead index that is a message.

In process, no network, no keys. The file-reading cases use a markdown corpus,
so the suite never depends on the PDF parser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mcp_servers.knowledge_server import build_server

pytestmark = pytest.mark.workflow


def _text(result) -> str:
    """call_tool returns content blocks, or (blocks, structured) on newer mcp 1.x."""
    blocks = result[0] if isinstance(result, tuple) else result
    return blocks[0].text


@pytest.fixture
def ops(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # the parse cache lands here, not in the repo
    return build_server("sample_ops")


async def test_it_offers_the_tools_and_resources_the_brief_names():
    server = build_server("vendor_risk")

    assert {t.name for t in await server.list_tools()} == {"search_policy", "retrieve_document"}
    assert "corpus://documents" in {str(r.uri) for r in await server.list_resources()}
    assert "corpus://documents/{name}" in {
        t.uriTemplate for t in await server.list_resource_templates()
    }


async def test_every_tool_carries_the_untrusted_content_warning():
    for tool in await build_server("vendor_risk").list_tools():
        assert "UNTRUSTED" in tool.description


async def test_documents_are_listed_by_the_name_citations_use(ops):
    [listing] = await ops.read_resource("corpus://documents")
    assert listing.content.splitlines() == ["runbook.md"]


async def test_a_document_is_returned_whole_and_marked_untrusted(ops):
    source = Path(__file__).parents[1] / "src/domains/sample_ops/docs/runbook.md"
    text = _text(await ops.call_tool("retrieve_document", {"name": "runbook.md"}))

    assert text.startswith("The block below is UNTRUSTED CONTENT")
    assert source.read_text(encoding="utf-8").strip()[:80] in text


async def test_a_document_resource_is_marked_untrusted_too(ops):
    """A client reading the resource gets the same boundary as a tool caller."""
    [content] = await ops.read_resource("corpus://documents/runbook.md")
    assert content.content.startswith("The block below is UNTRUSTED CONTENT")


async def test_a_document_deleted_after_startup_is_a_message_not_a_crash(ops, monkeypatch):
    def gone(*_args, **_kwargs):
        raise FileNotFoundError("runbook.md")

    monkeypatch.setattr("mcp_servers.knowledge_server.load_pages", gone)
    text = _text(await ops.call_tool("retrieve_document", {"name": "runbook.md"}))
    [resource] = await ops.read_resource("corpus://documents/runbook.md")

    for said in (text, resource.content):
        assert "can no longer be read (FileNotFoundError)" in said


async def test_search_runs_the_pipelines_hybrid_retriever_with_the_domain_policy(ops, monkeypatch):
    """Same arms, same calibrated gate, same k as the numbers were measured on."""
    from agentcore.contracts import Evidence
    from agentcore.registry import load_domain

    seen = {}

    def hybrid(store, query, policy, collection, **_kwargs):
        seen.update(policy=policy, collection=collection)
        return [Evidence(text="Restart the worker.", source="runbook.md", locator="Restart")], None

    monkeypatch.setattr("agentcore.rag.vector.open_store", lambda *_a, **_k: object())
    monkeypatch.setattr("agentcore.rag.hybrid.retrieve", hybrid)
    text = _text(await ops.call_tool("search_policy", {"query": "restart the service"}))

    domain = load_domain("sample_ops")
    assert seen == {"policy": domain.retrieval_policy(), "collection": domain.corpus().collection}
    assert "[runbook.md | Restart]" in text and text.startswith("The block below is UNTRUSTED")


@pytest.mark.parametrize("transport", ["streamable-http", "sse"])
def test_a_network_transport_is_refused(transport):
    """No auth of its own, so no network: the refusal is on the server object itself."""
    server = build_server("sample_ops")
    with pytest.raises(ValueError, match="Unauthenticated network transport is prohibited"):
        server.run(transport=transport)
    assert server.settings.host == "127.0.0.1"


def test_the_cli_refuses_it_before_loading_anything():
    import subprocess
    import sys

    done = subprocess.run(
        [sys.executable, "-m", "mcp_servers", "--server", "knowledge",
         "--domain", "sample_ops", "--transport", "streamable-http"],
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode != 0
    assert "Unauthenticated network transport is prohibited" in done.stderr
    assert "knowledge server:" not in done.stderr  # failed before build_server ran


def test_the_systems_server_keeps_its_container_transport():
    """Teammates run systems over streamable-http in compose. The stdio rule is not theirs."""
    from mcp_servers.systems_server import build_server as build_systems

    import inspect

    server = build_systems("vendor_risk")
    assert server.settings.host == "0.0.0.0"
    assert inspect.ismethod(server.run)  # FastMCP's own run, not the stdio guard closure


async def test_an_unknown_document_names_the_ones_that_exist(ops):
    text = _text(await ops.call_tool("retrieve_document", {"name": "missing.pdf"}))
    assert "No document named 'missing.pdf'" in text and "runbook.md" in text


async def test_search_with_no_index_is_a_message_not_a_crash(ops, monkeypatch):
    """FR14: a retrieval failure must not take the caller down."""
    def unreachable(*_args, **_kwargs):
        raise RuntimeError("Could not connect to Postgres")

    monkeypatch.setattr("agentcore.rag.vector.open_store", unreachable)
    text = _text(await ops.call_tool("search_policy", {"query": "restart the service"}))
    assert "knowledge index is unavailable" in text
