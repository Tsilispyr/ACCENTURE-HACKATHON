"""Vector + BM25, fused by rank. The retriever the pipeline actually calls.

Which arms run is a domain decision (`RetrievalPolicy.hybrid`), because the
answer differs by corpus:

  prose, paraphrased questions        vector alone is usually enough
  identifiers, codes, section numbers lexical matters a lot
  mixed                               hybrid, which is most real corpora

TWO THINGS THIS GETS RIGHT, both of which were wrong in the first version and
both of which silently halved recall rather than raising an error:

  1. THE LEXICAL ARM HONOURS THE SAME METADATA FILTER as the vector arm.
     Without that, BM25 indexes the whole collection while the vector arm is
     filtered - so fusion drags back in exactly the documents the filter was
     removing. On the reference corpus that turned recall@1 of 91% into 36%.

  2. BOTH ARMS CARRY THE SAME IDENTITY KEY. RRF can only reward a document
     found by both arms if it can tell it is the same document. The first
     version compared an int against a string, so no chunk ever matched across
     arms and "fusion" silently became concatenation.

Everything here works on Documents rather than Evidence, so metadata - and
therefore identity and filtering - survives to the point of fusion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from typing import Any

from langchain_core.documents import Document

from agentcore.contracts import Evidence, RetrievalPolicy
from agentcore.rag.lexical import LexicalIndex, reciprocal_rank_fusion


@dataclass
class RetrievalTrace:
    """What each arm found, kept so the inspector can explain a result.

    Retrieval that cannot be explained cannot be debugged: "the answer was
    wrong" and "the right chunk was never retrieved" look identical from the
    outside and need completely different fixes.
    """

    query: str
    arms: list[str] = field(default_factory=list)
    vector: list[str] = field(default_factory=list)
    lexical: list[str] = field(default_factory=list)
    fused: list[str] = field(default_factory=list)
    filtered_by: Any = None


def identity(doc: Document) -> str:
    """One definition of 'the same chunk', used by every arm."""
    index = doc.metadata.get("index")
    return f"i{index}" if index is not None else f"t{hash(doc.page_content[:200])}"


@lru_cache(maxsize=8)
def _lexical_index(collection: str, filter_key: str) -> LexicalIndex:
    """BM25 over the collection, restricted to the same rows the vector arm sees.

    filter_key is a stable string form of the metadata filter purely so this
    cache keys correctly - two different filters must not share an index.
    """
    import json

    from agentcore.rag.vector import open_store

    store = open_store(collection)
    where = json.loads(filter_key) if filter_key else None

    # There is no "list everything" on the vector-store interface, so ask for
    # far more rows than any corpus here holds. Fine at hundreds of chunks; a
    # larger system would read the table directly.
    try:
        docs = store.similarity_search("document", k=2000, filter=where)
    except Exception:  # noqa: BLE001
        docs = store.similarity_search("the", k=2000, filter=where)
    return LexicalIndex().build(docs)


def reset_lexical_cache() -> None:
    """Call after re-indexing, or BM25 keeps serving the previous corpus."""
    _lexical_index.cache_clear()


def retrieve(
    store,
    query: str,
    policy: RetrievalPolicy,
    collection: str,
    *,
    k: int | None = None,
) -> tuple[list[Evidence], RetrievalTrace]:
    """Run the enabled arms and fuse them. Returns evidence AND a trace."""
    import json

    from agentcore.rag.vector import scoped_filter, to_evidence

    k = k or policy.k
    where = scoped_filter(policy)
    trace = RetrievalTrace(query=query, arms=["vector"], filtered_by=where)

    # --- vector arm, with the same gating as a non-hybrid run ---------------
    scored = store.similarity_search_with_score(query, k=k, filter=where)
    ceiling = policy.max_distance
    kept = [(d, s) for d, s in scored if ceiling is None or s <= ceiling]
    if kept and policy.relative_margin is not None:
        cutoff = kept[0][1] + policy.relative_margin
        kept = [(d, s) for d, s in kept if s <= cutoff]

    vector_docs = [doc for doc, _ in kept]
    trace.vector = [_cite(d) for d in vector_docs]

    if not policy.hybrid:
        trace.fused = trace.vector
        return [to_evidence(d, s) for d, s in kept], trace

    # --- lexical arm, SAME filter ------------------------------------------
    filter_key = json.dumps(where, sort_keys=True) if where else ""
    lexical_pairs = _lexical_index(collection, filter_key).search(query, k=k)
    lexical_docs = [doc for doc, _ in lexical_pairs]
    trace.arms.append("bm25")
    trace.lexical = [_cite(d) for d in lexical_docs]

    if not lexical_docs:
        # No shared terms at all. That is normal for a paraphrased question and
        # is not a failure - fall back to the vector ranking rather than
        # letting an empty arm dilute it.
        trace.fused = trace.vector
        return [to_evidence(d, s) for d, s in kept], trace

    fused_pairs = reciprocal_rank_fusion([vector_docs, lexical_docs], limit=k,
                                         key=identity)
    trace.arms.append("rrf")
    trace.fused = [_cite(d) for d, _ in fused_pairs]
    return [to_evidence(d, score) for d, score in fused_pairs], trace


def retrieve_documents(store, query, policy, collection, *, k=None) -> list[Document]:
    """The fused ranking as Documents, metadata intact.

    The evaluator uses this rather than the Evidence form: Evidence flattens
    metadata into a display string, and recovering a label by parsing that
    string is exactly the trap this codebase warns about elsewhere - a path
    like "CHAPTER IV > Section 2 > Article 33" matches "Section 2" first.
    """
    import json

    from agentcore.rag.vector import scoped_filter

    k = k or policy.k
    where = scoped_filter(policy)
    scored = store.similarity_search_with_score(query, k=k, filter=where)
    vector_docs = [doc for doc, _ in scored]

    if not policy.hybrid:
        return vector_docs

    filter_key = json.dumps(where, sort_keys=True) if where else ""
    lexical_docs = [d for d, _ in _lexical_index(collection, filter_key).search(query, k=k)]
    if not lexical_docs:
        return vector_docs
    return [doc for doc, _ in reciprocal_rank_fusion([vector_docs, lexical_docs],
                                                     limit=k, key=identity)]


def _cite(doc: Document) -> str:
    meta = doc.metadata
    page = meta.get("page")
    return f"{meta.get('section', meta.get('source', '?'))}{f' | page {page}' if page else ''}"
