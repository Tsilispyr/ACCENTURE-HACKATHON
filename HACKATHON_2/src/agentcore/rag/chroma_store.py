"""Chroma as an alternative store, for running with no database server.

WHY A SECOND STORE EXISTS, given pgvector already works and is measured.

Not as a replacement. pgvector stays the default: it is already in the compose
stack, it shares one Postgres with the checkpoints, and the 91% recall figure
was measured against it. A second store would be pure duplication if that were
the whole story.

It earns its place on a different axis: **Chroma runs in-process and persists to
a directory**, so it needs no container, no port and no credentials. That makes
three things possible that pgvector cannot do:

  1. a laptop with Docker broken can still run and demo the whole pipeline
  2. CI can measure retrieval against REAL embeddings rather than a keyword stub
  3. `--reset` and re-index is a directory delete, which is a fast iteration loop

The seam already supports this: `Domain.store_factory()` exists precisely so a
domain can choose. Nothing in the pipeline changes.

What it costs: a second index to keep in sync at build time, and embeddings are
still an API call, so "no server" does not mean "no network".
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

CHROMA_DIR = Path(os.getenv("CHROMA_DIR", ".chroma"))


def open_chroma(collection: str, *, reset: bool = False):
    """A persistent Chroma collection with the same surface PGVector exposes."""
    from langchain_chroma import Chroma

    from agentcore.llm import embedding_model

    directory = CHROMA_DIR / collection
    if reset and directory.exists():
        import shutil

        # Chroma has no pre_delete_collection, so resetting is a directory
        # delete. Scoped to ONE collection, never the whole CHROMA_DIR.
        shutil.rmtree(directory)

    directory.mkdir(parents=True, exist_ok=True)
    return _Adapter(
        Chroma(
            collection_name=collection,
            embedding_function=embedding_model(),
            persist_directory=str(directory),
            # COSINE, explicitly. Chroma defaults to squared L2 while pgvector
            # uses cosine, and the two put distances on entirely different
            # scales.
            #
            # That difference was invisible and expensive. The backends were
            # measured "identical", and on RANKING they are - recall@k is
            # unaffected by the metric. But `max_distance` is an ABSOLUTE
            # threshold, so a ceiling calibrated on pgvector rejected almost
            # everything on Chroma: sample_policy delivered 32% of its
            # questions to the pipeline while still reporting 91% recall@1,
            # and vendor_risk delivered none at all.
            #
            # Setting it here makes one calibrated ceiling correct on both
            # backends, which is what "swap the store with one env var" has to
            # mean if the claim is going to be true.
            #
            # Set at COLLECTION CREATION. Changing it requires a reindex, not
            # a restart.
            collection_metadata={"hnsw:space": "cosine"},
        )
    )


class _Adapter:
    """Translate between PGVector's filter dialect and Chroma's.

    They express the same thing differently, and the pipeline speaks PGVector:

        PGVector   {"$and": [{"kind": {"$eq": "article"}}]}
        Chroma     {"$and": [{"kind": {"$eq": "article"}}]}   - mostly the same
        but Chroma rejects a single-key $and, and requires the bare form
        {"kind": "article"} or {"kind": {"$eq": "article"}} instead.

    Without this the filter is silently dropped by some versions and raises in
    others - and a dropped metadata filter is worth 36 points of recall@1 on
    the reference corpus, so it must not fail quietly.
    """

    def __init__(self, inner) -> None:
        self._inner = inner

    @staticmethod
    def _translate(where: dict[str, Any] | None) -> dict[str, Any] | None:
        if not where:
            return None
        for key in ("$and", "$or"):
            if key in where:
                terms = [t for t in (_Adapter._translate(t) for t in where[key]) if t]
                if not terms:
                    return None
                # Chroma rejects a one-element $and/$or.
                return terms[0] if len(terms) == 1 else {key: terms}
        return where

    def similarity_search_with_score(self, query, k=4, filter=None, **kwargs):  # noqa: A002
        return self._inner.similarity_search_with_score(
            query, k=k, filter=self._translate(filter), **kwargs
        )

    def similarity_search(self, query, k=4, filter=None, **kwargs):  # noqa: A002
        return self._inner.similarity_search(
            query, k=k, filter=self._translate(filter), **kwargs
        )

    def add_documents(self, documents, **kwargs):
        return self._inner.add_documents(documents, **kwargs)

    def __getattr__(self, name):
        return getattr(self._inner, name)
