"""COPY THIS DIRECTORY on the day. Do not edit it in place.

    cp -r src/domains/_template src/domains/<name>
    # fill in the files below, then:
    DOMAIN=<name> uv run pytest tests/test_domain_contract.py
    DOMAIN=<name> uv run python -m agentcore.rag.index --reset
    DOMAIN=<name> uv run python -m evaluation.retrieval_eval

Every file states a TIME BUDGET in its docstring. They add up to roughly three
hours across five people working in parallel, because nobody shares a file.

Fill them in this order - each one unblocks the next:

    1. vocab.py     10 min   what things are called
    2. corpus.py    20 min   where the documents are      -> unblocks indexing
    3. tools.py     40 min   read-only lookups            -> unblocks planning
    4. systems.py   40 min   the mutating operations      -> unblocks MCP
    5. policy.py    15 min   the risk floor               -> unblocks the gate
    6. report.py    15 min   the answer shape
    7. evalset.py   30 min   ground truth                 -> unblocks the gate

WHAT YOU DO NOT HAVE TO TOUCH: anything under src/agentcore/. If you find
yourself editing the core to make a scenario work, that is a bug in the seam --
say so out loud, and work around it here.
"""

from __future__ import annotations

from typing import Any, Callable

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agentcore.contracts import Corpus, EvalCase, GraphQuery, RetrievalPolicy, RiskLevel
from agentcore.domain import BaseDomain

from . import corpus as corpus_module
from . import policy as policy_module
from . import systems as systems_module
from . import tools as tools_module
from . import vocab
from .evalset import CASES
from .report import TemplateReport


class TemplateDomain(BaseDomain):
    name = "_template"  # CHANGE THIS to your package directory name

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

    def section_finder(self):
        return corpus_module.find_sections

    def is_heading(self, line: str) -> bool:
        return corpus_module.is_heading(line)

    def graph_queries(self) -> dict[str, GraphQuery]:
        return corpus_module.GRAPH_QUERIES

    # --- action -----------------------------------------------------------
    def local_tools(self) -> list[BaseTool]:
        return tools_module.TOOLS

    def systems(self) -> list[Callable[..., Any]]:
        return systems_module.SYSTEMS

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        return {"systems": {"server": "systems"}} if systems_module.SYSTEMS else {}

    def action_risk(self) -> dict[str, RiskLevel]:
        return policy_module.ACTION_RISK

    # --- safety -----------------------------------------------------------
    def blocked_patterns(self) -> list[str]:
        return policy_module.BLOCKED_PATTERNS

    def pii_rules(self) -> list[tuple[str, str]]:
        return policy_module.PII_RULES

    # --- output -----------------------------------------------------------
    def report_schema(self) -> type[BaseModel]:
        return TemplateReport

    def eval_cases(self) -> list[EvalCase]:
        return CASES


DOMAIN = TemplateDomain()
