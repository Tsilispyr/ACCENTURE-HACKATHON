"""Where the knowledge pack lives, and how it is cut up.

THE PACK IS THE ONE THE HANDOUT SUPPLIES, read from knowledge-base/knowledge/
at the repo root - the folder the team committed, so there is one copy of it.
Nothing here assumes its structure: the glob takes whatever is in that folder,
and the generic markdown section finder handles headings. A new vendor's files
(the hidden case) are dropped in the same folder and picked up by a reindex.

docs/ still holds the old two-file stand in. It is no longer read.

CALIBRATION IS STILL REQUIRED whenever the pack changes. The ceiling belongs to
a corpus, an embedding model and a distance metric:

    DOMAIN=vendor_risk uv run python -m agentcore.rag.index --reset
    DOMAIN=vendor_risk uv run python -m evaluation.calibrate
"""

from __future__ import annotations

from pathlib import Path

from agentcore.contracts import Corpus, GraphQuery, RetrievalPolicy
from agentcore.rag.chunking import MARKDOWN_HEADING, Section, markdown_sections

# parents[3] is the repo root: vendor_risk -> domains -> src -> root. Same in
# the container, where the Dockerfile's COPY . . puts the repo at /app.
DOCS = Path(__file__).resolve().parents[3] / "knowledge-base" / "knowledge"

CORPUS = Corpus(
    # rglob: the handout's pack keeps historical-vendor-assessments/ in a
    # subfolder, and glob("*") would skip it without a word. Dotfiles are
    # skipped too - .keep, and the .DS_Store macOS drops in every folder.
    paths=[str(p) for p in sorted(DOCS.rglob("*")) if p.is_file() and not p.name.startswith(".")],
    collection="vendor_risk",
    # A policy pack is dense reference text rather than flowing prose, so
    # chunks are smaller than the 1200 used for the regulation corpus: a
    # requirement and its threshold should land in one chunk.
    chunk_size=900,
    chunk_overlap=120,
)

POLICY = RetrievalPolicy(
    k=5,
<<<<<<< HEAD:HACKATHON_2/src/domains/vendor_risk/corpus.py
    # OFF, and the prediction that said otherwise was WRONG. This was True on
    # the reasoning that a vendor pack is full of exact tokens an embedding
    # flattens - SOC 2, ISO 27001, EUR 100,000 - which is the shape BM25 exists
    # for. Measured against the real pack on 2026-09-24:
=======
    # ON for this corpus, unlike sample_policy. A vendor pack is full of exact
    # tokens an embedding flattens: SOC 2, ISO 27001, clause numbers, vendor
    # names, euro thresholds. This is the shape BM25 exists for.
    # MEASURED on the real pack (2026-09-24, 16 cases): a tie with vector alone
    # on recall (81% / 100% / 100% at 1/3/5), MRR 0.906 against 0.896. Kept ON
    # as the lexical safety net for identifiers in a vendor pack nobody has
    # seen yet - the hidden case.
    hybrid=True,
    # CALIBRATED, not guessed. It was 0.70, copied from sample_policy, and at
    # that value the vector arm returned NOTHING for this corpus: the correct
    # top hit for "what security certification does a tier 1 supplier need"
    # scores 0.7007, losing to a 0.70 ceiling by four thousandths.
>>>>>>> 69d9d9c (mcp security issues fixed, rag and mcp working, default is hybrid rag, and ran locally. remains to be ran with live LLM and embeddings model after merge):src/domains/vendor_risk/corpus.py
    #
    #     vector only        recall@1 92%   MRR 0.944
    #     hybrid (v+bm25)    recall@1 83%   MRR 0.917
    #
    # BM25 pulls in sections that SHARE those tokens without answering the
    # question: the policy and the vendor's answer both say "retention" and
    # "24 hours", so lexical overlap is highest exactly where the corpus was
    # designed to have two sides. The same finding as the GDPR corpus, reached
    # for a different reason. Measured, not assumed - DECISIONS D21.
    hybrid=False,
    # CALIBRATED against the REAL pack on 2026-09-24 by `evaluation.calibrate`,
    # never guessed and never inherited:
    #
<<<<<<< HEAD:HACKATHON_2/src/domains/vendor_risk/corpus.py
    #     worst real question   0.429   ("is there precedent for accepting...")
    #     best nonsense         0.799   ("asdfgh qwerty zxcvbn")
    #     gap                   0.370   -> midpoint 0.61
    #
    # A 0.370 gap is a healthy corpus. The previous value was 0.70, inherited
    # from sample_policy, and against the stand-in corpus it rejected
    # EVERYTHING: the correct top hit scored 0.7007 and lost to the ceiling by
    # four thousandths. That failed almost invisibly, because hybrid=True meant
    # BM25 carried every query alone. Two things hid it. The audit said so on
    # every retrieval - `vector_top: []` - and nothing read it. And
    # `retrieval_eval` calls `similarity_search` directly, so it measures
    # RANKING and never sees the gate: it reported 100% recall while the
    # pipeline received zero vector hits. PROBLEMS P52.
    #
    # This number belongs to a corpus, an embedding model AND a distance
    # metric, and is portable across none of them. Re-run `evaluation.calibrate`
    # after any change to docs/.
    max_distance=0.61,
=======
    # RECALIBRATED on the real pack (2026-09-24, cosine, text-embedding-3-small):
    # worst real question 0.500, best nonsense 0.803, suggested 0.65 - the
    # midpoint of a 0.30 gap. The gate costs nothing: "through the gate" equals
    # the raw ranking and no question comes back empty.
    #
    # This number belongs to a corpus, an embedding model AND a distance
    # metric, and is not portable across any of the three. Re-run
    # evaluation.calibrate whenever the pack changes.
    max_distance=0.65,
>>>>>>> 69d9d9c (mcp security issues fixed, rag and mcp working, default is hybrid rag, and ran locally. remains to be ran with live LLM and embeddings model after merge):src/domains/vendor_risk/corpus.py
    relative_margin=0.15,
    # No filter yet: the shape of the real pack is unknown. If it separates
    # binding policy from guidance, filter to the binding half and re measure.
    metadata_filter=None,
    locators=["section", "clause", "policy"],
)

# Off until the real pack is in hand: an ontology invented against a stand in
# corpus would be wrong twice over. Returning {} disables the arm cleanly.
GRAPH_QUERIES: dict[str, GraphQuery] = {}


def is_heading(line: str) -> bool:
    return bool(MARKDOWN_HEADING.match(line.strip()))


def find_sections(markdown: str) -> list[Section]:
    return markdown_sections(markdown)
