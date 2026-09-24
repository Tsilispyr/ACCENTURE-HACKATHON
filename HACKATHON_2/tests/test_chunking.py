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


def test_positional_section_numbers_and_order_stay_corpus_wide(two_files):
    """'section 3' must still be ONE place, or eval labels and locators break."""
    chunks = chunk_corpus(two_files, source="some_domain")

    assert [c.metadata["number"] for c in chunks] == [1, 2, 3]
    assert [c.metadata["index"] for c in chunks] == [0, 1, 2]
