"""A multi-file corpus must still cite the file each chunk came from.

Every chunk once carried the DOMAIN name as its source, because all files were
joined into one stream first. With an eleven-PDF pack that makes a citation
unable to say which document it points at, and "page 3" ambiguous eleven ways.
"""

from __future__ import annotations

import pytest

from agentcore.rag.chunking import chunk_corpus

pytestmark = pytest.mark.workflow


@pytest.fixture
def two_files(tmp_path):
    first = tmp_path / "a-policy.md"
    first.write_text("# Policy\n\n## Scope\n\nApplies to all suppliers.\n\n"
                     "## Controls\n\nEncrypt data at rest.\n", encoding="utf-8")
    second = tmp_path / "b-proposal.md"
    second.write_text("# Proposal\n\n## Pricing\n\nEUR 120,000 per year.\n", encoding="utf-8")
    return [first, second]


def test_every_chunk_cites_its_own_file(two_files):
    chunks = chunk_corpus(two_files, source="some_domain")

    by_text = {c.page_content.split("\n\n", 1)[1]: c.metadata["source"] for c in chunks}
    assert by_text["Applies to all suppliers."] == "a-policy.md"
    assert by_text["Encrypt data at rest."] == "a-policy.md"
    assert by_text["EUR 120,000 per year."] == "b-proposal.md"
    assert "some_domain" not in by_text.values()


def test_bold_pdf_headings_give_clean_section_paths(tmp_path):
    """pymupdf4llm renders PDF headings as '## **1. Scope**'. Citations must not."""
    doc = tmp_path / "policy.md"
    doc.write_text("# **Security Policy**\n\n## **1. Scope**\n\nApplies to snake_case_names.\n",
                   encoding="utf-8")
    [chunk] = chunk_corpus([doc], source="some_domain")

    assert chunk.metadata["section"] == "Security Policy > 1. Scope"
    assert "snake_case_names" in chunk.page_content


def test_a_footer_on_every_one_page_file_is_stripped(tmp_path):
    """One-page files never have three pages each, so only a corpus-wide pass sees the footer."""
    paths = []
    for name in ("a", "b", "c", "d"):
        path = tmp_path / f"{name}.md"
        path.write_text(f"# Doc {name}\n\n## Rule\n\nRule text {name}.\n\n"
                        "Northstar - Fictional Material\n\nPage 1\n", encoding="utf-8")
        paths.append(path)
    chunks = chunk_corpus(paths, source="some_domain")

    assert chunks
    assert not any("Fictional Material" in c.page_content for c in chunks)
    assert not any("Page 1" in c.page_content for c in chunks)
    assert {c.metadata["source"] for c in chunks} == {"a.md", "b.md", "c.md", "d.md"}


def test_every_chunk_is_public_so_a_scoped_actor_can_retrieve_it(two_files):
    """scoped_filter keeps scope == actor OR scope == public. A chunk with no
    scope key matches neither, and alice (scope "payments") retrieved nothing."""
    from agentcore.contracts import Actor
    from agentcore.contracts import RetrievalPolicy
    from agentcore.rag.vector import scoped_filter
    from agentcore.world import bound

    chunks = chunk_corpus(two_files, source="some_domain")
    assert {c.metadata.get("scope") for c in chunks} == {"public"}

    with bound(Actor(id="alice", role="engineer", scope="payments")):
        where = scoped_filter(RetrievalPolicy())
    allowed = {term["scope"]["$eq"] for term in where["$or"]}
    assert all(c.metadata["scope"] in allowed for c in chunks)


def test_positional_section_numbers_and_order_stay_corpus_wide(two_files):
    """'section 3' must still be ONE place, or eval labels and locators break."""
    chunks = chunk_corpus(two_files, source="some_domain")

    assert [c.metadata["number"] for c in chunks] == [1, 2, 3]
    assert [c.metadata["index"] for c in chunks] == [0, 1, 2]


def test_every_chunk_is_public_so_a_scoped_actor_can_retrieve_it(two_files):
    """scoped_filter keeps scope == actor OR scope == public. A chunk with no
    scope key matches neither, and alice (scope 'payments') retrieved nothing."""
    from agentcore.contracts import Actor
    from agentcore.contracts import RetrievalPolicy
    from agentcore.rag.vector import scoped_filter
    from agentcore.world import bound

    chunks = chunk_corpus(two_files, source="some_domain")
    assert {c.metadata.get("scope") for c in chunks} == {"public"}

    with bound(Actor(id="alice", role="engineer", scope="payments")):
        where = scoped_filter(RetrievalPolicy())
    allowed = {term["scope"]["$eq"] for term in where["$or"]}
    assert all(c.metadata["scope"] in allowed for c in chunks)
