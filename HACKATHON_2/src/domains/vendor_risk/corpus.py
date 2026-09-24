"""Where the knowledge pack lives, and how it is cut up.

THE REAL PACK ARRIVES ON THE DAY. Nothing here assumes its structure: the glob
takes whatever is in docs/, and the generic markdown section finder handles
headings. If the pack turns out to have structure worth keeping (numbered
clauses, policy ids), override find_sections and nothing else changes.

What IS here is a stand in pack, so the whole pipeline is provably working
before the real one exists. Delete the stand in files and drop the real ones in
the same directory.

CALIBRATION IS STILL REQUIRED. The distance numbers below are inherited and
will be wrong for the real pack:

    DOMAIN=vendor_risk uv run python -m agentcore.rag.index --reset
    DOMAIN=vendor_risk uv run python -m evaluation.calibrate
"""

from __future__ import annotations

from pathlib import Path

from agentcore.contracts import Corpus, GraphQuery, RetrievalPolicy
from agentcore.rag.chunking import MARKDOWN_HEADING, Section, markdown_sections

DOCS = Path(__file__).parent / "docs"

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
    # OFF, and the prediction that said otherwise was WRONG. This was True on
    # the reasoning that a vendor pack is full of exact tokens an embedding
    # flattens - SOC 2, ISO 27001, EUR 100,000 - which is the shape BM25 exists
    # for. Measured against the real pack on 2026-09-24:
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
