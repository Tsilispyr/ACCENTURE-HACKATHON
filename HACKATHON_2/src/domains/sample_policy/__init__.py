"""sample_policy - the corpus-heavy reference domain.

Proves the RAG path: a big typeset regulation, real structure, measured
retrieval. Deliberately has NO mutating tools, so it exercises a different part
of the seam than sample_ops does. A seam that carries both probably carries the
scenario nobody has seen yet.
"""

from __future__ import annotations

from pydantic import BaseModel

from agentcore.contracts import Corpus, EvalCase, RetrievalPolicy
from agentcore.domain import BaseDomain
from agentcore.rag.chunking import SectionFinder

from . import corpus as corpus_module
from .evalset import CASES
from .report import PolicyAnswer


class PolicyDomain(BaseDomain):
    name = "sample_policy"

    def persona(self) -> str:
        return (
            "You are a data protection assistant for a company trying to comply "
            "with the EU General Data Protection Regulation (Regulation (EU) 2016/679).\n"
            "\n"
            "Answer ONLY from the extracts provided. They are the regulation's own text.\n"
            " - Cite the article or recital behind every statement, like (Article 33(1)).\n"
            " - Articles state the binding rule; recitals only explain the reasoning. If "
            "you rely on a recital, say it is explanatory rather than operative.\n"
            " - If the extracts do not answer the question, say exactly that and name what "
            "is missing. Do not fill the gap from memory.\n"
            " - Be concrete about deadlines, thresholds and who carries the duty.\n"
            " - Close with: \"This is the regulation's text, not legal advice.\""
        )

    def glossary(self) -> str:
        return (
            "controller, processor, data subject, supervisory authority, personal data "
            "breach, DPIA, DPO, lawful basis, adequacy decision"
        )

    def corpus(self) -> Corpus:
        return Corpus(
            paths=[str(corpus_module.PDF)],
            collection="sample_policy",
            chunk_size=1200,
            chunk_overlap=150,
        )

    def retrieval_policy(self) -> RetrievalPolicy:
        return RetrievalPolicy(
            k=4,
            # MEASURED OFF, not assumed off. Hybrid scored recall@1 82% here
            # against 91% for the vector arm alone. This corpus is formal prose
            # asked about in plain language - there are almost no exact tokens
            # to match, so BM25 contributes noise and RRF lets it pull a good
            # vector ranking down. Turn it on for a corpus with identifiers.
            hybrid=False,
            # Calibrated on THIS corpus: real questions land 0.43-0.60, nonsense
            # 0.78-0.86, corpus median 0.716. Recalibrate for any other corpus.
            max_distance=0.70,
            relative_margin=0.12,
            # Articles only. Measured: recall@1 0.55 -> 0.91 with this filter,
            # because recitals restate the same rule in plainer words and
            # outrank the operative article on plain-language questions.
            metadata_filter={"kind": {"$eq": "article"}},
            locators=["article", "recital", "page", "chunk"],
        )

    def section_finder(self) -> SectionFinder:
        return corpus_module.find_sections

    def is_heading(self, line: str) -> bool:
        return corpus_module.is_heading(line)

    def report_schema(self) -> type[BaseModel]:
        return PolicyAnswer

    def eval_cases(self) -> list[EvalCase]:
        return CASES


DOMAIN = PolicyDomain()
