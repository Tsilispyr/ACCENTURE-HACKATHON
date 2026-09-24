"""BM25 lexical search, and fusion with the vector results.

WHY THIS EXISTS, given that embeddings already do "semantic" search.

BM25 is not a worse embedding. It is a DIFFERENT failure mode, and the two fail
on opposite things:

  embeddings   strong on paraphrase ("can we say no to a refund" -> the refund
               clause), weak on exact tokens. An identifier like INC-1042, an
               error code, a rare proper noun or a section number is a nearly
               meaningless direction in embedding space.
  BM25         strong on exact tokens and rare words - it is scoring term
               overlap. Blind to paraphrase: "terminate an employee" and
               "dismissal procedure" share no words and score zero.

So the question "is BM25 needed when the model already does semantics" has a
concrete answer: yes, because the model's semantics cannot retrieve a document
whose only link to the question is a literal string the embedding flattened.
Anthropic's own contextual-retrieval numbers make the same point - contextual
embeddings alone cut retrieval failures 35%, and adding contextual BM25 took it
to 49%. The lexical half was worth more than the whole first technique.

FUSION. Results are combined with Reciprocal Rank Fusion rather than by
comparing scores, because BM25 scores and cosine distances are not on the same
scale and normalising them is guesswork. RRF only uses RANK:

    score(d) = sum over retrievers of 1 / (k + rank(d))

with k=60, the value from the original RRF paper. A document ranked well by
both retrievers beats one ranked brilliantly by only one - which is exactly
the behaviour wanted.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from langchain_core.documents import Document
from rank_bm25 import BM25Okapi

from agentcore.contracts import Evidence

RRF_K = 60

# Words carrying no discriminating power. Kept deliberately short: an
# aggressive stoplist removes terms that matter in a specific corpus.
STOPWORDS = frozenset(
    """a an the of to in for on at by is are was were be been being and or but if
    then than that this these those with without from as it its we you they i do
    does did how what when where which who whom why can could should would may
    might must shall will have has had our your their""".split()
)


def tokenise(text: str) -> list[str]:
    """Lowercase alphanumeric words, stopwords dropped, identifiers preserved.

    'INC-1042' becomes 'inc' and '1042' rather than being discarded: the digits
    are usually the discriminating part, and they are exactly what embeddings
    cannot represent.
    """
    words = re.findall(r"[a-z0-9]+", text.lower())
    return [w for w in words if w not in STOPWORDS and len(w) > 1]


@dataclass
class LexicalIndex:
    """An in-memory BM25 index over the same chunks the vector store holds.

    In memory on purpose. The corpora here are hundreds of chunks, the index
    builds in well under a second, and a second persistent store is one more
    thing to keep in sync and one more thing to be stale at 16:30.
    """

    documents: list[Document] = field(default_factory=list)
    _bm25: BM25Okapi | None = None

    def build(self, documents: list[Document]) -> "LexicalIndex":
        self.documents = documents
        corpus = [tokenise(d.page_content) for d in documents]
        # BM25Okapi divides by the average document length, so an empty corpus
        # raises rather than returning nothing useful.
        self._bm25 = BM25Okapi(corpus) if corpus else None
        return self

    def search(self, query: str, k: int = 4) -> list[tuple[Document, float]]:
        if self._bm25 is None or not self.documents:
            return []
        tokens = tokenise(query)
        if not tokens:
            return []
        scores = self._bm25.get_scores(tokens)
        ranked = sorted(zip(self.documents, scores), key=lambda p: p[1], reverse=True)
        # A zero score means no query term appears at all - not a weak match,
        # no match. Returning those would pad the result list with noise.
        return [(doc, float(score)) for doc, score in ranked[:k] if score > 0]


def default_identity(doc: Document) -> str:
    index = doc.metadata.get("index")
    return f"i{index}" if index is not None else f"t{hash(doc.page_content[:200])}"


def reciprocal_rank_fusion(
    rankings: list[list[Document]],
    *,
    k: int = RRF_K,
    limit: int = 4,
    key=default_identity,
) -> list[tuple[Document, float]]:
    """Combine ranked lists by rank, never by score.

    `key` must return the SAME string for the same chunk whichever arm found
    it. Get that wrong and fusion silently degrades into concatenation - no
    error, just a worse ranking.
    """
    scores: dict[str, float] = {}
    seen: dict[str, Document] = {}

    for ranking in rankings:
        for rank, document in enumerate(ranking, start=1):
            key_value = key(document)
            scores[key_value] = scores.get(key_value, 0.0) + 1.0 / (k + rank)
            seen.setdefault(key_value, document)

    ordered = sorted(scores.items(), key=lambda pair: pair[1], reverse=True)
    return [(seen[identifier], score) for identifier, score in ordered[:limit]]


def as_evidence(pairs: list[tuple[Document, float]], *, retriever: str) -> list[Evidence]:
    """Convert scored documents into Evidence, tagged with which arm found them.

    WATCH THE LOCATOR. The `| via {retriever}` suffix means this function and
    `vector.to_evidence` emit DIFFERENT `cite()` strings for the same chunk.
    That is deliberate, because when hybrid retrieval is on, knowing whether a
    passage came from the vector arm or from BM25 is most of the diagnostic
    value. But it is a trap for anything that matches citations BY STRING,
    which is exactly what `contracts.match_citation` and `citation_correctness`
    both do.

    So if a citation ever fails to resolve only when hybrid is enabled, this
    suffix is the first place to look, not the matcher.

    `trusted=False` on every item, always: retrieved text is attacker
    influenced, and `safety/untrusted.py` wraps it in an envelope before it
    reaches any prompt.
    """
    out = []
    for doc, score in pairs:
        meta = doc.metadata
        page = meta.get("page")
        out.append(
            Evidence(
                text=doc.page_content,
                source=str(meta.get("source", "corpus")),
                locator=(
                    f"{meta.get('section', '')}{f' | page {page}' if page else ''}"
                    f" | via {retriever}"
                ).strip(" |"),
                score=score,
                trusted=False,
            )
        )
    return out
