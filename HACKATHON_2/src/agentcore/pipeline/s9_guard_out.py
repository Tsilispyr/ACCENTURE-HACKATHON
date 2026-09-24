"""Stage 9 - output guardrails. The last thing before anyone sees an answer.

Three checks, cheapest first:
  1. a refusal from s2 becomes the answer (nothing else ran)
  2. PII scrub - deterministic regex, no LLM
  3. groundedness - are the claims actually supported by the retrieved text?

An ungrounded answer is DOWNGRADED, not deleted: the citations and the honest
"I could not ground this" are more useful than silence, and far more useful
than a confident fabrication.
"""

from __future__ import annotations

import re
from typing import Any

from agentcore.contracts import Answer, match_citation
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain

EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]+\b")
CREDIT_CARD = re.compile(r"\b(?:\d[ -]?){13,16}\b")
# Country code + 2 check digits + up to 30 alphanumeric, spaces optional (as
# IBANs are commonly printed in 4-character groups).
IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{1,4}){2,7}\b")
IP_ADDRESS = re.compile(
    r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d?\d)\b"
)
# A phone number is recognised by SHAPE, not by being a long run of digits.
#
# THE PERMISSIVE VERSION WAS MEASURED AND REVERSED. `\b\+?(?:\d[-.\s()]?){7,15}\b`
# matches any 7 to 15 digits with optional separators, and in a vendor
# assessment that is most of the report. Across ten real report sentences it
# destroyed five:
#
#     "issued 2026-03-14 and expires 2027-03-14"  -> both dates redacted
#     "Contract value is EUR 1 200 000"           -> the value redacted
#     "certificate 2024 118 942"                  -> the number redacted
#
# The trade is not symmetrical. Dates, reference numbers and monetary values
# are WHY a report exists; an unlabelled local phone number in a document
# corpus is rare. A scrubber that removes the date something was reported has
# done more damage than one that misses a phone number, so this favours
# precision. PROBLEMS P57.
#
# The discriminator is grouping. A bare phone is 3-3-4; an ISO date is 4-2-2;
# money is 1-3-3. Anything else needs a positive signal: an international
# prefix, a parenthesised area code, or an explicit label.
PHONE = re.compile(
    r"\+\d{1,3}[-.\s]?\(?\d{1,4}\)?(?:[-.\s]?\d){4,12}"   # +1 (555) 123-4567
    r"|\(\d{2,5}\)[-.\s]?\d{3,4}[-.\s]?\d{3,4}"           # (0210) 123 4567
    r"|\b\d{3}[-.\s]\d{3}[-.\s]\d{4}\b"                   # 210-123-4567, not 2026-03-14
    r"|(?:tel|phone|mobile|fax)[:.]?\s*\+?\d[-.\s()\d]{5,17}\d",
    re.IGNORECASE,
)
# The weakest of these five: only the common English "NUMBER + words + street
# type" shape. No fixed format exists across countries, so this is a tripwire
# for the one pattern common corpora actually use, not general coverage - the
# same admission patterns.py makes about paraphrased injection.
ADDRESS = re.compile(
    r"\b\d{1,5}\s+[A-Z][\w'-]*(?:\s+[A-Z][\w'-]*){0,3}\s+"
    r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|"
    r"Place|Pl|Court|Ct|Way|Square|Sq)\.?\b"
)
# Internal identifiers that should never reach a user.
INTERNAL_ID = re.compile(r"\bINTERNAL-[A-Z0-9-]+\b")

GROUNDEDNESS_FLOOR = 0.5


def _scrub(text: str, rules: list[tuple[str, str]]) -> tuple[str, list[str]]:
    """ORDER MATTERS. Each pattern runs on what the previous one left behind.

    The specific patterns run before the general ones, so a value is redacted
    as what it IS rather than as whatever it partly resembles. IBAN before
    credit card is the case that made this explicit: an IBAN contains a 14
    digit run, so the card pattern claimed half of "GB29 NWBK 6016 1331 9268
    19" and the IBAN pattern got the rest, producing a correctly redacted but
    unreadable "[iban redacted] [card redacted]". Nothing leaked either way,
    which is why this is a legibility fix and not a security one.
    """
    hits: list[str] = []
    kinds = {kind for kind, _ in rules}

    if "email" in kinds and EMAIL.search(text):
        text, hits = EMAIL.sub("[email redacted]", text), hits + ["email"]
    if "iban" in kinds and IBAN.search(text):
        text, hits = IBAN.sub("[iban redacted]", text), hits + ["iban"]
    if "credit_card" in kinds and CREDIT_CARD.search(text):
        text, hits = CREDIT_CARD.sub("[card redacted]", text), hits + ["credit_card"]
    if "ip_address" in kinds and IP_ADDRESS.search(text):
        text, hits = IP_ADDRESS.sub("[ip redacted]", text), hits + ["ip_address"]
    if "phone" in kinds and PHONE.search(text):
        text, hits = PHONE.sub("[phone redacted]", text), hits + ["phone"]
    if "address" in kinds and ADDRESS.search(text):
        text, hits = ADDRESS.sub("[address redacted]", text), hits + ["address"]
    if INTERNAL_ID.search(text):
        text, hits = INTERNAL_ID.sub("[internal ref]", text), hits + ["internal_id"]
    return text, hits


def _groundedness(answer_text: str, evidence_text: str) -> float:
    """Cheap lexical overlap of content words. No LLM, so it always runs.

    Deliberately crude: it is a tripwire for an answer that shares almost no
    vocabulary with its sources, not a judgement of correctness. The real
    per-claim groundedness judge lives in evaluation/judge.py, where it can
    afford an LLM call.
    """
    if not evidence_text or not answer_text:
        return 0.0
    words = lambda t: {w for w in re.findall(r"[a-z]{5,}", t.lower())}  # noqa: E731
    answer_words, source_words = words(answer_text), words(evidence_text)
    if not answer_words:
        return 1.0
    return len(answer_words & source_words) / len(answer_words)


def check_claims(answer, required_domains: list[str]) -> list[dict[str, Any]]:
    """Enforce the claim contract. Recording a basis is not the same as honouring it.

    Three checks, all deterministic:

      UNSUPPORTED   an 'evidence' claim with no citation, or an 'inference'
                    claim with no reasoning. Both are the failure the basis
                    field exists to prevent: a conclusion dressed as a finding.

      CITED SOURCE  a citation that names a source not in the answer's own
                    citation list. That is a fabricated reference - the most
                    damaging error an assessment can make, because it is the
                    one a reader is least likely to check.

      COVERAGE      a declared risk domain with no finding. Silence about a
                    domain reads as "nothing to report" and usually means
                    "never looked".

      APPROVE ON GAPS  a clean "approve" alongside a claim whose basis is
                    "missing", or a finding with unresolved gaps. Section 7 of
                    the handout says it directly: "do not treat missing
                    evidence as PASS". Nothing upstream enforced this on the
                    RECOMMENDATION itself - check_claims already caught an
                    unsupported claim or a fabricated citation, but a clean
                    approve sitting next to an admitted gap slipped through
                    both, because coverage only checks that a domain was
                    ASSESSED, not that assessing it found nothing missing.

    These downgrade the answer rather than deleting it: a partial assessment
    that says which parts are weak is worth more than no assessment.
    """
    problems: list[dict[str, Any]] = []

    unsupported = answer.unsupported_claims
    if unsupported:
        problems.append({
            "kind": "unsupported_claims",
            "count": len(unsupported),
            "examples": [c.statement[:90] for c in unsupported[:3]],
        })

    # A citation must name a source retrieval actually returned. Compared as a
    # WHOLE citation: `cite()` is `source | locator`, and the source is the
    # collection name, identical for every chunk. Splitting on the pipe made
    # this check pass every citation in the corpus, including invented ones.
    known = {c: "" for c in answer.citations}
    if known:
        invented = [
            cite for claim in answer.claims for cite in claim.citations
            if match_citation(cite, known) is None
        ]
        if invented:
            problems.append({
                "kind": "citation_not_in_evidence",
                "count": len(invented),
                "examples": invented[:3],
            })

    if required_domains:
        missing = [d for d in required_domains if d not in answer.assessed_domains]
        if missing:
            problems.append({"kind": "unassessed_domains", "domains": missing})

    # A clean approve must not coexist with an admitted gap. Checked on the
    # RECOMMENDATION, not the (possibly still-pending) human decision: this is
    # about the model's own conclusion being internally consistent, which a
    # human reviewer should never have to catch by hand.
    if answer.recommendation == "approve":
        missing_claims = [c for c in answer.claims if c.basis == "missing"]
        gapped_findings = [f for f in answer.findings if f.gaps]
        if missing_claims or gapped_findings:
            problems.append({
                "kind": "approve_despite_gaps",
                "missing_claims": len(missing_claims),
                "gapped_findings": [f.domain for f in gapped_findings],
            })

    return problems


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    audit: list[dict[str, Any]] = []

    if state.get("refusal"):
        return {
            "answer": Answer(summary=state["refusal"], refused=True,
                             refusal_reason="input guardrail"),
            "audit": [audit_event("s9_guard_out", "refusal_returned")],
        }

    answer = state.get("answer") or Answer(summary="No answer was produced.", partial=True)

    scrubbed, hits = _scrub(answer.summary, domain.pii_rules())
    if hits:
        answer.summary = scrubbed
        audit.append(audit_event("s9_guard_out", "pii_scrubbed", kinds=hits))

    # Claim level checks first. These are specific enough to name what is
    # wrong; the lexical groundedness check below only says that something is.
    problems = check_claims(answer, domain.risk_domains())
    for problem in problems:
        audit.append(
            audit_event("s9_guard_out", problem["kind"],
                        **{k: v for k, v in problem.items() if k != "kind"})
        )
    if problems:
        answer.partial = True
        notes = []
        for problem in problems:
            if problem["kind"] == "unsupported_claims":
                notes.append(
                    f"{problem['count']} claim(s) lack the citation or reasoning their "
                    f"stated basis requires"
                )
            elif problem["kind"] == "citation_not_in_evidence":
                notes.append(
                    f"{problem['count']} citation(s) name a source that was not retrieved"
                )
            elif problem["kind"] == "unassessed_domains":
                notes.append(f"not assessed: {', '.join(problem['domains'])}")
            elif problem["kind"] == "approve_despite_gaps":
                parts = []
                if problem["missing_claims"]:
                    parts.append(f"{problem['missing_claims']} claim(s) marked missing evidence")
                if problem["gapped_findings"]:
                    parts.append(f"open gaps in: {', '.join(problem['gapped_findings'])}")
                notes.append(
                    f"recommended approve despite {' and '.join(parts)} - "
                    f"missing evidence must never read as a pass"
                )
        answer.summary = (
            f"{answer.summary}\n\n[Assessment quality: {'; '.join(notes)}. "
            f"Treat the affected statements as unverified.]"
        )

    evidence_text = "\n".join(e.text for e in state.get("evidence", []))
    score = _groundedness(answer.summary, evidence_text)
    audit.append(audit_event("s9_guard_out", "groundedness", score=round(score, 2)))

    if evidence_text and score < GROUNDEDNESS_FLOOR:
        answer.partial = True
        answer.summary = (
            f"{answer.summary}\n\n"
            "[Note: this answer could not be fully grounded in the retrieved sources. "
            "Treat it as provisional and check the citations.]"
        )
        audit.append(audit_event("s9_guard_out", "ungrounded_downgraded", score=round(score, 2)))

    return {"answer": answer, "audit": audit}
