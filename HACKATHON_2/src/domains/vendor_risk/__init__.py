"""vendor_risk - procurement, vendor risk and AI governance.

The scenario: a vendor (Asteria AI Systems) is under assessment against an
enterprise knowledge pack (Northstar). The system must plan the assessment,
research it against the corpus, consolidate evidence, cover several risk
domains, and reach a decision that a human can approve, approve with
conditions, or reject.

WHAT IS DELIBERATELY NOT HERE: the corpus. The knowledge pack is supplied on
the day and drops into `docs/`. Nothing in this file assumes its structure --
`corpus.py` globs whatever is there and the generic markdown section finder
handles it. If the pack turns out to have real structure worth keeping,
override `find_sections` there and nothing else changes.

Mapping to the stated requirements, numbered from the OFFICIAL handout, which
has 14. The preliminary text had A2A as FR07; the official one deletes that row
and every requirement below it shifts up by one, so a comment elsewhere that
says FR15 predates the correction.
    FR01  parse_request           structured vendor assessment request
    FR02  s4_plan + s7_replan     a multi-step plan that is maintained
    FR03  corpus + s3_ground      RAG over the supplied pack
    FR04  Answer.citations        cited evidence
    FR05  Claim.basis             evidence / inference / missing
    FR06  systems() + MCP         four enterprise capabilities
    FR07  risk_domains()          security, commercial, AI governance, legal compliance
    FR10  contradictions          detected in s8_compose
    FR11  conditional approval    s5_gate three-way decision
    FR12  action_risk()           the risk floor drives the HITL gate
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agentcore.contracts import (
    Corpus,
    EvalCase,
    GraphQuery,
    Request,
    RetrievalPolicy,
    RiskLevel,
)
from agentcore.domain import BaseDomain
from agentcore.rag.chunking import SectionFinder

from . import corpus as corpus_module
from . import policy as policy_module
from . import systems as systems_module
from . import tools as tools_module
from . import vocab
from .evalset import CASES
from .report import VendorAssessment

DOCS = Path(__file__).parent / "docs"


class VendorRiskDomain(BaseDomain):
    name = "vendor_risk"

    # --- identity ---------------------------------------------------------
    def persona(self) -> str:
        return vocab.PERSONA

    def glossary(self) -> str:
        return vocab.GLOSSARY

    # --- knowledge --------------------------------------------------------
    def corpus(self) -> Corpus:
        return corpus_module.CORPUS

    def retrieval_policy(self) -> RetrievalPolicy:
        return corpus_module.POLICY

    def section_finder(self) -> SectionFinder:
        return corpus_module.find_sections

    def is_heading(self, line: str) -> bool:
        return corpus_module.is_heading(line)

    def graph_queries(self) -> dict[str, GraphQuery]:
        return corpus_module.GRAPH_QUERIES

    # --- assessment shape -------------------------------------------------
    def risk_domains(self) -> list[str]:
        """FR08 wants security, commercial, and at least one more. Four here.

        Four rather than three because AI governance is the whole point of this
        scenario and data privacy is where a vendor assessment usually breaks.
        Each one becomes a RiskFinding, so coverage is measurable rather than
        asserted.
        """
        return vocab.RISK_DOMAINS

    def specialists(self) -> list[dict[str, Any]]:
        return vocab.SPECIALISTS

    # --- action -----------------------------------------------------------
    def local_tools(self) -> list[BaseTool]:
        return tools_module.TOOLS

    def systems(self) -> list[Callable[..., Any]]:
        return systems_module.SYSTEMS

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {"systems": {"server": "systems"}}

    def action_risk(self) -> dict[str, RiskLevel]:
        return policy_module.ACTION_RISK

    def request_risk(self, request: Request) -> RiskLevel:
        """FR12. A consequential verdict needs a human, whatever tools produced it."""
        return policy_module.request_risk(request.raw_text)

    # --- safety -----------------------------------------------------------
    def blocked_patterns(self) -> list[str]:
        return policy_module.BLOCKED_PATTERNS

    def pii_rules(self) -> list[tuple[str, str]]:
        return policy_module.PII_RULES

    # --- output -----------------------------------------------------------
    def report_schema(self) -> type[BaseModel]:
        return VendorAssessment

    def eval_cases(self) -> list[EvalCase]:
        return CASES


DOMAIN = VendorRiskDomain()
