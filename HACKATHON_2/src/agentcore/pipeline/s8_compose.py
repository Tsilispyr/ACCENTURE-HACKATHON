"""Stage 8 - turn everything into the domain's own answer schema.

The schema comes from the domain, so what a good answer LOOKS like is a
scenario decision, not a core one.

When the domain declares `risk_domains()`, this stage does three extra things
that an assessment needs and a plain answer does not:

  CLAIMS WITH A BASIS   every statement is tagged evidence / inference /
                        missing. That distinction is the difference between an
                        assessment and an opinion: a reader must be able to see
                        which conclusions a document carries and which the
                        model reasoned its way to.

  A FINDING PER DOMAIN  one RiskFinding per declared domain, so coverage is
                        checkable. A domain with no findings is visibly
                        unassessed rather than quietly absent.

  CONTRADICTIONS        two sources that cannot both be right. Worth surfacing
                        rather than silently picking one: in an assessment, a
                        contradiction between what a vendor claims and what a
                        policy requires is often the finding itself.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from agentcore.contracts import Answer, Claim, Contradiction, RiskFinding
from agentcore.llm import chat_model
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.untrusted import envelope


class AssessmentDraft(BaseModel):
    """What the model returns when the domain declares risk domains."""

    summary: str = Field(description="The overall verdict in two or three sentences.")
    claims: list[Claim] = Field(
        description=(
            "Every material statement. Use basis='evidence' and cite the extract "
            "when a document says it. Use basis='inference' and give your reasoning "
            "when you concluded it. Use basis='missing' when something that should "
            "be checkable is absent from the extracts."
        )
    )
    findings: list[RiskFinding] = Field(
        description="One entry per required risk domain, even if the evidence is thin."
    )
    contradictions: list[Contradiction] = Field(
        default_factory=list,
        description="Pairs of statements from the extracts that cannot both be true.",
    )
    recommendation: str = Field(
        default="",
        description=(
            "approve, approve_with_conditions or reject, with one line of why. "
            "LEAVE THIS EMPTY unless the request actually asks for a decision about "
            "a named subject. A question about what a policy says is not a request "
            "to decide anything."
        ),
    )
    conditions: list[str] = Field(
        default_factory=list,
        description="If recommending conditions, what must be true. Each one testable.",
    )


def _describe(error: Exception) -> str:
    """Say WHICH value was rejected, not just that something was.

    A pydantic ValidationError puts the offending value in `input_value=...` at
    the END of each error, so truncating the string to fit an audit event cut
    off the only part worth reading. Three runs failed on `findings.N.level`
    and the audit could not say what the level had been (PROBLEMS P55).
    """
    errors = getattr(error, "errors", None)
    if not callable(errors):
        return f"{type(error).__name__}: {error}"[:240]
    parts = []
    for item in errors()[:3]:
        where = ".".join(str(piece) for piece in item.get("loc", ()))
        parts.append(f"{where}={item.get('input')!r} ({item.get('msg', '')[:50]})")
    return "; ".join(parts)[:240] or str(error)[:240]


def _key(name: str) -> str:
    """Strip case and punctuation so `ai_governance` and "AI Governance" agree."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", name.strip().lower()).split())


def _match_findings(required: list[str], returned: list[RiskFinding]) -> list[RiskFinding]:
    """Line the model's findings up with the domains that were required.

    `risk_domains()` returns IDENTIFIERS - `ai_governance`, `legal_compliance` -
    and the model writes the `domain` field in prose: "AI Governance",
    "Legal & Compliance", "Commercial Risk". Comparing them raw drops the
    finding and substitutes a "not assessed" placeholder, so a domain the model
    DID assess reads as a coverage gap. `ai_governance` could never match at
    all unless the model happened to echo the underscore.

    Measured on the flagship case: four findings returned, ONE matched, and the
    run reported 1/4 with a full set of claims and a decision beside it. That
    combination is the signature - real analysis, zero credit (PROBLEMS P56).

    Two passes, both CONSUMING so no finding is claimed twice:

      1. exact, once case and punctuation are normalised away
      2. word subset either way, which catches "Commercial Risk" for
         `commercial` and "Information Security" for `security`

    Pass 2 runs only after every exact match is settled, so a loose match can
    never steal a finding that an exact one wanted.

    A domain with no match still gets its placeholder: the gap has to stay
    visible. The point is only that it should be a REAL gap.
    """
    pool = [(set(_key(f.domain).split()), f) for f in returned]
    pool = [(words, f) for words, f in pool if words]
    matched: dict[str, RiskFinding] = {}

    for exact in (True, False):
        for name in required:
            if name in matched:
                continue
            want = set(_key(name).split())
            for index, (words, finding) in enumerate(pool):
                hit = words == want if exact else (want <= words or words <= want)
                if hit:
                    matched[name] = finding
                    pool.pop(index)
                    break

    return [
        matched.get(name)
        or RiskFinding(
            domain=name, level="none", assessed=False,
            summary="Not assessed: no finding was returned for this domain.",
            gaps=["no finding returned"],
        )
        for name in required
    ]


def _normalise_decision(text: str) -> str:
    decision = (text or "").strip().lower()
    if "condition" in decision:
        return "approve_with_conditions"
    if decision.startswith("approve"):
        return "approve"
    if decision.startswith("reject"):
        return "reject"
    return "pending"


def _assessment(domain, request, evidence, findings_text: str) -> dict[str, Any]:
    """The assessment path. Returns fields to merge into the Answer."""
    required = domain.risk_domains()
    model = chat_model().with_structured_output(AssessmentDraft, method="function_calling")

    draft = model.invoke(
        f"{domain.persona()}\n\n"
        f"REQUEST:\n{request.raw_text}\n\n"
        f"{envelope(evidence)}\n\n"
        + (f"WHAT WAS DONE:\n{findings_text}\n\n" if findings_text else "")
        + f"Assess EVERY one of these risk domains: {', '.join(required)}.\n"
        "For each, give a level - exactly one of none, low, medium or high, and "
        "no compound like medium-high - and say what it rests on. If the extracts do not "
        "cover a domain, still return a finding for it with assessed=false and say "
        "what is missing. An unassessed domain is a result, not an omission.\n\n"
        "Tag every claim with its basis:\n"
        "- Use basis='evidence' ONLY when a specific document extract directly states it, and put the exact extract citation in `citations` (e.g. 'Information Security Policy > 6. Data retention').\n"
        "- Use basis='inference' for conclusions, findings or judgements reached by specialist reviews. Put the explanation in `reasoning` and leave `citations` empty.\n"
        "- Use basis='missing' when required evidence or reports are absent.\n"
        "NEVER cite specialist names or execution steps (like 'security assessment findings', 's1') in `citations`.\n\n"
        "If this request asks you to assess or decide about a NAMED subject, you "
        "MUST give a recommendation: approve, approve_with_conditions or reject. "
        "An assessment that reaches no verdict has not finished the job it was "
        "asked to do.\n"
        "If it is a question about what the policy says, answer it and leave the "
        "recommendation empty. Inventing a verdict for a question that asked for "
        "none is worse than giving none, because it reads as a judgement somebody "
        "made."
    )

    # Guarantee one finding per required domain. The model usually returns them
    # all; when it does not, the gap must be VISIBLE rather than inferred from
    # a short list.
    findings = _match_findings(required, draft.findings)

    import re
    from agentcore.contracts import match_citation

    def _is_step_or_specialist(cite_str: str) -> bool:
        c = cite_str.strip().lower()
        return (
            bool(re.match(r"^s\d+(\s|$)", c))
            or "reviewer" in c
            or "assessment findings" in c
            or "specialist" in c
            or c in {"s1", "s2", "s3", "s4", "s5", "s6", "s7", "s8", "s9"}
        )

    known_cites = {e.cite(): e.cite() for e in evidence}
    for claim in draft.claims:
        if claim.basis == "evidence":
            cleaned_cites = []
            specialist_refs = []
            for c in claim.citations:
                matched = match_citation(c, known_cites)
                if matched:
                    cleaned_cites.append(matched)
                elif _is_step_or_specialist(c):
                    specialist_refs.append(c)
                else:
                    cleaned_cites.append(c)

            if cleaned_cites:
                claim.citations = cleaned_cites
            elif specialist_refs:
                claim.basis = "inference"
                claim.reasoning = claim.reasoning or specialist_refs[0]
                claim.citations = []

    return {
        "summary": draft.summary,
        "claims": draft.claims,
        "findings": findings,
        "contradictions": draft.contradictions,
        "decision": _normalise_decision(draft.recommendation),
        "conditions": draft.conditions,
    }


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    request = state["request"]
    evidence = state.get("evidence", [])
    results = state.get("past_steps", [])
    partial = state.get("replan_count", 0) > 3 or any(not r.ok for r in results)

    if state.get("rejected"):
        return {
            "answer": Answer(
                summary="The plan was not approved, so nothing was executed.",
                refused=True, refusal_reason="human rejected the plan", decision="reject",
            ),
            "audit": [audit_event("s8_compose", "rejected_plan")],
        }

    # Attribute each result to the specialist that produced it. This is what
    # makes delegation improve the ANSWER rather than only a metric: a finding
    # carrying "who judged this" is worth more than the same sentence unsigned,
    # and it lets the composer weigh a security verdict as a security verdict.
    findings_text = "\n".join(
        f"- {r.step_id}{f' [{r.owner}]' if r.owner else ''}: {r.output[:400]}"
        for r in results if r.ok
    )

    # Plain domains: if no evidence and no step outputs exist, refuse out-of-corpus
    if not evidence and not findings_text and not domain.risk_domains():
        return {
            "answer": Answer(
                summary="The knowledge corpus does not contain information to answer this question.",
                refused=True,
                refusal_reason="out of corpus",
            ),
            "audit": [audit_event("s8_compose", "out_of_corpus_refused")],
        }
    audit: list[dict[str, Any]] = []
    extra: dict[str, Any] = {}

    # Did a HUMAN answer the gate, and with what? Only a conditional approval
    # carries information the model did not already have; a plain approval of
    # the model's own plan does not override its recommendation.
    human_conditions = state.get("approval_conditions") or []
    human_decision = "approve_with_conditions" if human_conditions else ""

    try:
        if domain.risk_domains():
            extra = _assessment(domain, request, evidence, findings_text)
            # If no evidence was retrieved and no domain could be assessed, refuse as out-of-corpus
            if not evidence and not any(f.assessed for f in extra.get("findings", [])):
                return {
                    "answer": Answer(
                        summary="The knowledge corpus does not contain information to answer this question.",
                        refused=True,
                        refusal_reason="out of corpus",
                    ),
                    "audit": [audit_event("s8_compose", "out_of_corpus_refused")],
                }
            audit.append(
                audit_event("s8_compose", "assessed",
                            domains=[f.domain for f in extra["findings"]],
                            assessed=[f.domain for f in extra["findings"] if f.assessed],
                            claims=len(extra["claims"]),
                            contradictions=len(extra["contradictions"]),
                            decision=extra["decision"])
            )
            # Derive the report from the assessment we ALREADY have, rather
            # than sending the model back over the evidence a second time. Two
            # independent passes on the same extracts produced a report whose
            # recommendation could differ from the findings printed above it --
            # two answers wearing one cover. This pass is a TRANSCRIPTION: it
            # receives the structured assessment and is told not to revisit it.
            findings_block = "\n".join(
                f"- {f.domain}: level={f.level} assessed={f.assessed} {f.summary}"
                for f in extra["findings"]
            )
            claims_block = "\n".join(
                f"- [{c.basis}] {c.statement}" for c in extra["claims"][:25]
            )
            prompt = (
                f"{domain.persona()}\n\nREQUEST:\n{request.raw_text}\n\n"
                f"ASSESSMENT SUMMARY:\n{extra['summary']}\n\n"
                f"RISK FINDINGS:\n{findings_block}\n\n"
                f"CLAIMS:\n{claims_block}\n\n"
                f"RECOMMENDATION: {extra['decision']}\n"
                f"CONDITIONS: {extra['conditions']}\n\n"
                "Transcribe the assessment above into the report schema. Do NOT "
                "re-evaluate it and do NOT change the recommendation: the findings "
                "are already decided, and a report that disagrees with its own "
                "findings is worse than no report."
            )
        else:
            prompt = (
                f"{domain.persona()}\n\nREQUEST:\n{request.raw_text}\n\n"
                f"{envelope(evidence)}\n\n"
                + (f"WHAT WAS DONE:\n{findings_text}\n\n" if findings_text else "")
                + "Produce the structured answer. Ground every claim in the extracts above."
            )
            audit.append(audit_event("s8_compose", "composed", partial=partial))

        body = chat_model().with_structured_output(
            domain.report_schema(), method="function_calling"
        ).invoke(prompt)
        payload = body.model_dump()

    except Exception as error:  # noqa: BLE001
        return {
            "answer": Answer(summary="Could not compose an answer.", partial=True,
                             citations=[e.cite() for e in evidence]),
            "audit": [audit_event("s8_compose", "compose_failed", error=_describe(error))],
        }

    summary = extra.get("summary") or str(
        payload.get("answer") or payload.get("summary") or ""
    )

    answer = Answer(
        body=payload,
        summary=summary,
        citations=sorted({e.cite() for e in evidence}),
        confidence=0.0 if not evidence else round(min(1.0, len(evidence) / 4), 2),
        partial=partial,
        claims=extra.get("claims", []),
        findings=extra.get("findings", []),
        contradictions=extra.get("contradictions", []),
        # The model's view, always recorded.
        recommendation=extra.get("decision", "pending"),
        # The decision of record. A human at the gate outranks the model; if
        # nobody was asked, the model's recommendation stands in and is
        # labelled as such, so a reader never mistakes one for the other.
        decision=(human_decision or extra.get("decision", "pending")),
        decided_by=("human" if human_decision else "model"),
        # Conditions the HUMAN attached at the gate come first and outrank the
        # model's: a reviewer's condition is a requirement, the model's is a
        # suggestion.
        conditions=(state.get("approval_conditions") or []) + extra.get("conditions", []),
    )
    return {"answer": answer, "audit": audit}
