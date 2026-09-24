"""Stage 3 - agentic RAG. Retrieve, grade what came back, retry if it is thin.

Three things make this "agentic" rather than a single similarity search:

  1. ROUTE      a locator ("article 33", "page 7") is a metadata lookup, not a
                similarity question, and is answered without embedding anything
                meaningful. Similarity search cannot answer "what is on page 7".
  2. SELF-GRADE a structured call decides whether the evidence actually answers
                the question, and says what is missing if not.
  3. REWRITE    the missing piece drives one rewritten query. Capped at
                RETRIEVAL_RETRIES, because an unbounded retrieval loop is one of
                the two ways a live demo hangs.

All evidence is marked trusted=False. It came from a document, and documents
are attacker-influenced.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentcore.contracts import Evidence
from agentcore.llm import chat_model
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.rag.hybrid import retrieve as hybrid_retrieve
from agentcore.rag.vector import fetch_locator, open_store, parse_locator
from agentcore.registry import load_domain
from agentcore.safety.patterns import compile_patterns, scan
from agentcore.safety.untrusted import summarise

RETRIEVAL_RETRIES = 2


class Sufficiency(BaseModel):
    """The self-grade."""

    enough: bool = Field(description="True if the extracts answer the question.")
    missing: str = Field(
        default="", description="If not enough, what specifically is absent. One line."
    )


def _grade(question: str, evidence: list[Evidence]) -> Sufficiency:
    """Ask whether we have what we need. Failure is not fatal - assume enough.

    A grading call that errors should not block an answer we may already be
    able to give; the output guard at s9 is what catches an ungrounded answer.
    """
    if not evidence:
        return Sufficiency(enough=False, missing="nothing was retrieved")
    try:
        model = chat_model().with_structured_output(Sufficiency, method="function_calling")
        return model.invoke(
            "Question:\n"
            f"{question}\n\n"
            "Extract summaries (provenance and opening text only):\n"
            f"{summarise(evidence)}\n\n"
            "Do these extracts contain the answer? If not, name what is missing."
        )
    except Exception:  # noqa: BLE001 - grading is advisory
        return Sufficiency(enough=True)


def _rewrite(question: str, missing: str) -> str:
    """Turn 'what is missing' into a better query. Falls back to the original."""
    try:
        reply = chat_model().invoke(
            f"Original question: {question}\n"
            f"Missing from the first search: {missing}\n\n"
            "Write ONE short search query likely to find the missing part. "
            "Use the vocabulary a document would use, not the user's. "
            "Answer with the query only."
        )
        return (reply.content or question).strip().strip('"')[:200]
    except Exception:  # noqa: BLE001
        return question


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    request = state["request"]
    policy = domain.retrieval_policy()
    store = open_store(domain.corpus().collection)

    audit: list[dict[str, Any]] = []
    collected: list[Evidence] = []

    # 1. Route: an explicit locator short-circuits similarity entirely.
    locator = parse_locator(request.raw_text, policy.locators)
    if locator:
        kind, number = locator
        collected = fetch_locator(store, kind, number, policy)
        audit.append(audit_event("s3_ground", "locator_lookup", kind=kind, number=number,
                                 found=len(collected)))
    else:
        # 2. Retrieve, grade, rewrite - bounded.
        query = request.raw_text
        collection = domain.corpus().collection
        for attempt in range(RETRIEVAL_RETRIES + 1):
            found, trace = hybrid_retrieve(store, query, policy, collection)
            collected.extend(f for f in found if f.text not in {c.text for c in collected})
            audit.append(audit_event("s3_ground", "retrieved", attempt=attempt + 1,
                                     query=query[:80], got=len(found), arms=trace.arms,
                                     vector_top=trace.vector[:1], lexical_top=trace.lexical[:1]))

            verdict = _grade(request.raw_text, collected)
            if verdict.enough:
                audit.append(audit_event("s3_ground", "sufficient", attempt=attempt + 1))
                break
            audit.append(audit_event("s3_ground", "insufficient", missing=verdict.missing[:120]))
            if attempt < RETRIEVAL_RETRIES:
                query = _rewrite(request.raw_text, verdict.missing)
            else:
                # If after all retries the self-grader says insufficient and no vector hit passed the distance gate, discard false lexical hits
                no_vector = all(not e.get("vector_top") for e in audit if e.get("event") == "retrieved")
                if no_vector:
                    collected = []

    # 3. Screen what came back. A document telling us to ignore our rules is
    #    recorded, never obeyed - and never fatal, or any document could shut
    #    the system down by containing the right sentence.
    patterns = compile_patterns(domain.blocked_patterns())
    for item in collected:
        hit = scan(item.text, patterns)
        if hit:
            audit.append(audit_event("s3_ground", "injection_in_retrieved_content",
                                     source=item.cite(), matched=hit))

    if not collected:
        audit.append(audit_event("s3_ground", "no_evidence"))

    return {"evidence": collected, "retrieval_attempts": len(audit), "audit": audit}

