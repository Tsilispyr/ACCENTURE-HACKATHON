"""An in-memory stand-in for PGVector, so this domain needs no database.

Why it lives in src/ and not in tests/: the CI eval gate uses it. A gate that
cannot run without Postgres and an embedding key is a gate that does not run on
a fork, and a gate that does not run is not a gate.

Distance is 1 - word overlap, so "more words in common" means "closer" - the
same direction as cosine distance. That is all the fidelity a workflow test
needs. Retrieval QUALITY against real embeddings is measured separately, by
running evaluation/retrieval_eval.py against a real domain.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.documents import Document


class FakeStore:
    """The subset of PGVector's surface the pipeline actually calls."""

    def __init__(self, docs: list[Document] | None = None) -> None:
        self.docs = docs if docs is not None else default_docs()

    # --- the PGVector surface ---------------------------------------------

    def similarity_search_with_score(self, query, k=4, filter=None, **_):  # noqa: A002
        scored = sorted(
            ((d, self._distance(query, d)) for d in self._filtered(filter)),
            key=lambda pair: pair[1],
        )
        return scored[:k]

    def similarity_search(self, query, k=4, filter=None, **_):  # noqa: A002
        return [doc for doc, _ in self.similarity_search_with_score(query, k, filter)]

    def add_documents(self, docs, **_):
        self.docs.extend(docs)
        return [str(i) for i in range(len(docs))]

    # --- internals ---------------------------------------------------------

    @staticmethod
    def _words(text: str) -> set[str]:
        return set(re.findall(r"[a-z]{4,}", text.lower()))

    def _distance(self, query: str, doc: Document) -> float:
        """Word overlap, normalised by query length.

        KNOWN LIMIT, and it matters when writing tests against this fake: a
        LEXICAL score cannot stand in for a SEMANTIC one when the question
        paraphrases the document. The real flagship request scores 0.313
        against a real embedding store and 0.833 here, purely because it and
        the policy share few literal words. So a test that needs evidence to
        flow must phrase its question in the fixture's own vocabulary, and a
        test that needs real retrieval quality belongs in `evaluation/`, not
        here.
        """
        query_words = self._words(query)
        if not query_words:
            return 1.0
        return 1.0 - len(query_words & self._words(doc.page_content)) / len(query_words)

    def _filtered(self, where: dict | None) -> list[Document]:
        if not where:
            return self.docs
        return [d for d in self.docs if matches(d.metadata, where)]


def matches(meta: dict[str, Any], where: dict[str, Any]) -> bool:
    """Enough of PGVector's jsonb filter syntax for the tests to be honest.

    Supports $and, $or and $eq - which is exactly what the pipeline generates.
    Anything else silently passing would make a filter test meaningless.
    """
    if "$and" in where:
        return all(matches(meta, term) for term in where["$and"])
    if "$or" in where:
        return any(matches(meta, term) for term in where["$or"])
    for field, condition in where.items():
        if isinstance(condition, dict) and "$eq" in condition:
            if meta.get(field) != condition["$eq"]:
                return False
    return True


def default_docs() -> list[Document]:
    """The handbook, one Document per section, with the real metadata shape."""
    from domains.deterministic import CORPUS_TEXT

    docs = []
    for i, block in enumerate(CORPUS_TEXT.split("## ")[1:], start=1):
        title, _, body = block.partition("\n")
        docs.append(
            Document(
                page_content=f"Section {i}: {title.strip()}\n\n{body.strip()}",
                metadata={
                    "source": "handbook.md", "page": i, "section": f"Section {i}",
                    "kind": "section", "number": i, "index": i - 1, "scope": "public",
                },
            )
        )
    return docs
