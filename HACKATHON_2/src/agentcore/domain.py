"""THE SEAM.

Everything scenario-specific in this system reaches the core through this one
Protocol. A new scenario is a new package under src/domains/ that satisfies it.

The contract is deliberately small - 19 methods - and `BaseDomain` provides a
working default for every one, so a minimal domain overrides only `name`,
`persona()`, `corpus()` and `eval_cases()` and already runs end to end.

Why these nineteen and not fewer: each covers an axis a hidden scenario can
move on, and none is derivable from the others.

    different corpus    corpus() + retrieval_policy() + section_finder() + is_heading()
    different store     store_factory()
    different entities  glossary() + parse_request() + graph_queries()
    different tools     local_tools() + mcp_servers() + systems() + action_risk()
    different verdict   risk_domains() + specialists()
    different prompts   persona() + skills
    different truth     eval_cases()
"""

from __future__ import annotations

import uuid
from typing import Any, Callable, Protocol, runtime_checkable

from langchain_core.tools import BaseTool
from pydantic import BaseModel

from agentcore.rag.chunking import MARKDOWN_HEADING, SectionFinder, markdown_sections

from agentcore.contracts import (
    Actor,
    Corpus,
    EvalCase,
    GraphQuery,
    Request,
    RetrievalPolicy,
    RiskLevel,
)


class DefaultReport(BaseModel):
    """What a domain gets if it does not define its own answer schema."""

    summary: str
    findings: list[str] = []
    next_steps: list[str] = []


@runtime_checkable
class Domain(Protocol):
    """What the core requires of a scenario. See BaseDomain for defaults."""

    name: str

    # --- identity and prompts -------------------------------------------
    def persona(self) -> str: ...
    def glossary(self) -> str: ...

    # --- intake ----------------------------------------------------------
    def parse_request(self, raw: str | dict[str, Any], actor: Actor) -> Request: ...

    # --- knowledge -------------------------------------------------------
    def corpus(self) -> Corpus: ...
    def retrieval_policy(self) -> RetrievalPolicy: ...
    def graph_queries(self) -> dict[str, GraphQuery]: ...
    def section_finder(self) -> SectionFinder: ...
    def is_heading(self, line: str) -> bool: ...
    def store_factory(self) -> Callable[..., Any] | None: ...

    # --- action ----------------------------------------------------------
    def risk_domains(self) -> list[str]: ...
    def specialists(self) -> list[dict[str, Any]]: ...
    def local_tools(self) -> list[BaseTool]: ...
    def mcp_servers(self) -> dict[str, dict[str, Any]]: ...
    def systems(self) -> list[Callable[..., Any]]: ...
    def action_risk(self) -> dict[str, RiskLevel]: ...
    def request_risk(self, request: Request) -> RiskLevel: ...

    # --- safety ----------------------------------------------------------
    def blocked_patterns(self) -> list[str]: ...
    def pii_rules(self) -> list[tuple[str, str]]: ...

    # --- output and ground truth -----------------------------------------
    def report_schema(self) -> type[BaseModel]: ...
    def eval_cases(self) -> list[EvalCase]: ...


class BaseDomain:
    """Working defaults for every method on the Protocol.

    Subclass this rather than implementing Domain from scratch: it means adding
    a method to the Protocol later does not break every existing domain.
    """

    name: str = "base"

    # --- identity and prompts -------------------------------------------
    def persona(self) -> str:
        return (
            "You are a careful assistant. Answer ONLY from the extracts provided. "
            "Cite the source of every statement. If the extracts do not answer the "
            "question, say exactly that and name what is missing - do not fill the "
            "gap from memory."
        )

    def glossary(self) -> str:
        """Entity vocabulary handed to the planner.

        Keep it short. This exists so the planner writes steps in the domain's
        own terms rather than inventing generic ones.
        """
        return ""

    # --- intake ----------------------------------------------------------
    def parse_request(self, raw: str | dict[str, Any], actor: Actor) -> Request:
        """Turn whatever arrived into a Request.

        The default handles both a bare string and a flat dict of fields, which
        covers most structured-record scenarios without any domain code.
        """
        if isinstance(raw, str):
            return Request(id=f"req-{uuid.uuid4().hex[:8]}", raw_text=raw, actor=actor)

        entities = {str(k): str(v) for k, v in raw.items() if v is not None}
        text = raw.get("description") or raw.get("text") or raw.get("question") or ""
        if not text:
            text = "\n".join(f"{k}: {v}" for k, v in entities.items())
        return Request(
            id=str(raw.get("id") or f"req-{uuid.uuid4().hex[:8]}"),
            raw_text=str(text),
            actor=actor,
            entities=entities,
            priority=raw.get("priority"),
        )

    # --- knowledge -------------------------------------------------------
    def corpus(self) -> Corpus:
        return Corpus(collection=self.name)

    def retrieval_policy(self) -> RetrievalPolicy:
        return RetrievalPolicy()

    def section_finder(self) -> SectionFinder:
        """How this corpus is cut into structural units.

        The default finds markdown headings, which is right for most documents.
        A corpus with real domain structure (articles, clauses, SOP steps)
        overrides this - see domains/sample_policy/corpus.py.
        """
        return markdown_sections

    def is_heading(self, line: str) -> bool:
        """Used by boilerplate removal to protect structure.

        Must agree with section_finder(): a heading this misses can be deleted
        as repeated furniture, which silently collapses the document.
        """
        return bool(MARKDOWN_HEADING.match(line.strip()))

    def store_factory(self) -> Callable[..., Any] | None:
        """Override to supply a store other than pgvector.

        Returning None means "use the real one". The deterministic domain
        returns an in-memory stand-in, which is what lets the CI eval gate run
        with no database and no embedding key - a gate that cannot run on a
        fork is not a gate.
        """
        return None

    def graph_queries(self) -> dict[str, GraphQuery]:
        """Returning {} disables the graph arm entirely.

        The router cannot offer a tool that was never registered, so a domain
        with no graph simply has no graph - no flags, no dead code path.
        """
        return {}

    # --- assessment shape -------------------------------------------------
    def risk_domains(self) -> list[str]:
        """The dimensions every assessment must cover.

        Returning a non-empty list changes three things: the planner is told to
        cover each one, `s8_compose` emits a RiskFinding per domain, and the
        evaluator can measure COVERAGE - a domain with no findings is visibly
        unassessed rather than quietly absent.

        Empty means "this is a question, not an assessment", which is the right
        answer for a domain like a policy lookup.
        """
        return []

    def specialists(self) -> list[dict[str, Any]]:
        """Deep-agent subagents, as plain dicts.

            name           what the planner writes in a step's `owner`
            description    WHEN to pick this one. End it with a routing clause
                           ("Use for anything about X"): the planner reads this
                           and nothing else to decide.
            system_prompt  the standard of judgement it applies

        The key is `system_prompt`, NOT `prompt`. The library reads it with
        `spec.get("system_prompt", "")`, so a domain using the wrong key gets a
        specialist with an EMPTY prompt, no error, and no clue why its reviews
        read exactly like the main agent's.

        Delegation is worth it when a sub-task needs a DIFFERENT standard of
        judgement rather than just more steps - a security reviewer and a
        commercial reviewer read the same document and care about different
        things. It is not worth it merely to split a long task in two.

        Empty means one executor handles everything, which is usually right.
        """
        return []

    # --- action ----------------------------------------------------------
    def local_tools(self) -> list[BaseTool]:
        return []

    def mcp_servers(self) -> dict[str, dict[str, Any]]:
        """Which MCP servers this domain exposes. {} means none."""
        return {}

    def systems(self) -> list[Callable[..., Any]]:
        """The MUTATING operations, served over MCP rather than in-process.

        Plain functions with type hints and a docstring - the MCP server turns
        them into tools. They live behind a process boundary because they are
        the calls that change things, which is exactly what the risk floor and
        the approval gate exist to control.
        """
        return []

    def action_risk(self) -> dict[str, RiskLevel]:
        """The RISK FLOOR: tool name -> the lowest risk it can ever be.

        The model may raise a level. It can never lower one. Anything not
        listed here defaults to "low" - so a mutating tool that is forgotten
        here is a real gap, which is why test_domain_contract checks that every
        tool name appears.
        """
        return {}

    def request_risk(self, request: Request) -> RiskLevel:
        """How consequential is the REQUEST, before any tool is chosen?

        `action_risk()` answers "how dangerous is this tool". This answers "how
        dangerous is what is being asked", and the two are not the same. A
        vendor assessment reads policies and adds up numbers - every tool low
        or medium - and then produces a verdict somebody acts on. Scoring it
        from the tool list alone rated it medium and no human was ever asked,
        on exactly the decision a human is required to make.

        Returning "high" makes the plan reach the approval gate. Default
        "none", so a domain that says nothing behaves exactly as before.
        """
        return "none"

    # --- safety ----------------------------------------------------------
    def blocked_patterns(self) -> list[str]:
        """Extra refusal patterns on top of safety/patterns.py's base list."""
        return []

    def pii_rules(self) -> list[tuple[str, str]]:
        """(pii_type, "redact" | "block") pairs."""
        return [("email", "redact")]

    # --- output and ground truth -----------------------------------------
    def report_schema(self) -> type[BaseModel]:
        return DefaultReport

    def eval_cases(self) -> list[EvalCase]:
        return []

    # --- convenience ------------------------------------------------------
    def tool_vocabulary(self) -> list[str]:
        """Every tool name the planner is allowed to reference.

        Used to constrain the planner and to validate its output: a plan step
        naming a tool that does not exist is rejected and reprompted once.
        """
        names = [t.name for t in self.local_tools()]
        # MCP-served operations count: the planner must be able to name them,
        # or it can never plan the only steps that actually change anything.
        names += [f.__name__ for f in self.systems()]
        names += [f"graph:{k}" for k in self.graph_queries()]
        names += ["search_corpus", "get_by_locator"]
        return names

    def specialist_vocabulary(self) -> list[str]:
        """Every specialist name a plan step may name as its `owner`.

        Same contract as `tool_vocabulary`, for the same reason: a step naming
        a specialist that does not exist is dropped and reprompted once, rather
        than reaching the executor as an instruction to hand work to nobody.
        """
        return [s["name"] for s in self.specialists()]

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Domain {self.name}>"
