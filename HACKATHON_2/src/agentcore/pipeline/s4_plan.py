"""Stage 4 - the planning agent. Produce a revision-numbered to-do list.

Two things worth knowing about this stage.

THE PLANNER NEVER SEES RAW RETRIEVED TEXT. It gets `summarise(evidence)` --
provenance plus an opening snippet. That is the structural half of prompt-
injection defence: instructions hidden inside a retrieved document cannot reach
the component that decides what the system will DO, because that component is
never shown the document.

THE PLANNER IS CONSTRAINED TO REAL TOOLS. Its prompt lists the exact tool
vocabulary, and any step naming something outside it is rejected and reprompted
once. An unconstrained planner inventing `check_the_database` is the most
common silent failure in this pattern - the plan looks fine and the executor
can do nothing with it.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from agentcore.contracts import Plan, PlanStep
from agentcore.llm import chat_model
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.risk import apply_floor
from agentcore.safety.untrusted import summarise

MAX_STEPS = 6


class DraftStep(BaseModel):
    description: str = Field(description="One concrete action, imperative mood.")
    tool_hint: str = Field(
        default="", description="Exactly one tool name from the allowed list, or empty."
    )
    risk: str = Field(default="low", description="none | low | medium | high")
    owner: str = Field(
        default="",
        description=(
            "Exactly one specialist name from the allowed list, or empty. Set it "
            "when the step needs that specialist's standard of judgement, not "
            "merely more work."
        ),
    )


class Draft(BaseModel):
    steps: list[DraftStep] = Field(description="Ordered. Fewest steps that answer the request.")


def _prompt(domain, request, evidence_summary: str, retry_note: str = "") -> str:
    vocabulary = ", ".join(domain.tool_vocabulary()) or "(no tools available)"
    glossary = domain.glossary()
    specialists = domain.specialists()
    dimensions = domain.risk_domains()

    # Descriptions go in UNTRUNCATED. Each one ends with the clause that says
    # WHEN to pick that specialist, and the planner reads nothing else to
    # decide. An earlier version truncated them at 60 characters, which cut off
    # exactly that clause.
    specialist_block = ""
    if specialists:
        listing = "\n".join(f"  {s['name']}: {s['description']}" for s in specialists)
        specialist_block = (
            "SPECIALISTS YOU MAY ASSIGN A STEP TO (exact names, nothing else):\n"
            f"{listing}\n\n"
        )

    dimension_block = ""
    if dimensions:
        dimension_block = (
            f"DIMENSIONS AN ASSESSMENT MUST COVER: {', '.join(dimensions)}\n\n"
        )

    # The shape rule. When a domain declares dimensions, an assessment is a
    # multi-judgement job and collapsing it to one step is wrong. But a policy
    # LOOKUP in the same domain is still one step, so the distinction is made
    # on the request, not the domain - fanning every one-line question into
    # four executor runs would be worse than the problem.
    if dimensions:
        shape_rules = (
            "- If this request asks you to ASSESS or DECIDE about a named subject, "
            "plan one step per dimension above and set `owner` to the specialist "
            "whose judgement that dimension needs. Retrieved evidence starts an "
            "assessment; it does not finish one.\n"
            "- If it is a question about what a document says, return ONE step that "
            "composes the answer from the evidence.\n"
        )
    else:
        shape_rules = (
            "- If the evidence already answers the request, return ONE step that "
            "composes the answer.\n"
        )

    owner_rule = (
        "- owner must be exactly one name from the specialist list, or empty.\n"
        if specialists else ""
    )

    return (
        f"{domain.persona()}\n\n"
        f"Plan how to answer this request. At most {MAX_STEPS} steps.\n\n"
        f"REQUEST:\n{request.raw_text}\n\n"
        + (f"KNOWN ENTITIES: {request.entities}\n\n" if request.entities else "")
        + (f"DOMAIN VOCABULARY: {glossary}\n\n" if glossary else "")
        + f"EVIDENCE ALREADY RETRIEVED (summaries only):\n{evidence_summary}\n\n"
        + f"TOOLS YOU MAY NAME (exact names, nothing else): {vocabulary}\n\n"
        + specialist_block
        + dimension_block
        + "Rules:\n"
        + shape_rules
        + "- tool_hint must be exactly one name from the list above, or empty.\n"
        + owner_rule
        + "- Mark a step 'high' risk if it changes, deletes or moves anything.\n"
        + f"{retry_note}"
    )


def _validate(draft: Draft, vocabulary: list[str],
              specialists: list[str] = ()) -> tuple[list[PlanStep], list[str], list[str]]:
    """Drop anything the planner invented. Same contract for tools and owners.

    An owner naming a specialist that does not exist is worse than no owner: it
    reaches the executor as an instruction to hand the step to nobody.
    """
    steps, bad_tools, bad_owners = [], [], []
    for i, raw in enumerate(draft.steps[:MAX_STEPS], 1):
        hint = (raw.tool_hint or "").strip()
        if hint and hint not in vocabulary:
            bad_tools.append(hint)
            hint = ""

        owner = (getattr(raw, "owner", "") or "").strip()
        if owner and owner not in specialists:
            bad_owners.append(owner)
            owner = ""

        steps.append(
            PlanStep(
                id=f"s{i}",
                description=raw.description.strip(),
                tool_hint=hint or None,
                risk=raw.risk if raw.risk in {"none", "low", "medium", "high"} else "low",
                owner=owner or None,
            )
        )
    return steps, bad_tools, bad_owners


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    request = state["request"]
    vocabulary = domain.tool_vocabulary()
    specialists = domain.specialist_vocabulary()
    summary = summarise(state.get("evidence", []))

    audit: list[dict[str, Any]] = []
    model = chat_model().with_structured_output(Draft, method="function_calling")

    try:
        draft = model.invoke(_prompt(domain, request, summary))
        steps, bad_tools, bad_owners = _validate(draft, vocabulary, specialists)

        if bad_tools or bad_owners:
            # One reprompt naming what it got wrong. Only one: a planner that
            # invents tools twice will invent them a third time, and the
            # fallback below is a perfectly serviceable answer.
            if bad_tools:
                audit.append(audit_event("s4_plan", "invented_tools", tools=bad_tools))
            if bad_owners:
                audit.append(
                    audit_event("s4_plan", "invented_specialists", specialists=bad_owners)
                )
            note = "\nYour previous attempt named things that do not exist."
            if bad_tools:
                note += f" Tools: {bad_tools}. Use ONLY: {vocabulary}."
            if bad_owners:
                note += f" Specialists: {bad_owners}. Use ONLY: {specialists}."
            draft = model.invoke(_prompt(domain, request, summary, retry_note=note + "\n"))
            steps, bad_tools, bad_owners = _validate(draft, vocabulary, specialists)

    except Exception as error:  # noqa: BLE001 - a failed planner must not kill the request
        audit.append(audit_event("s4_plan", "planner_failed", error=str(error)[:120]))
        steps = []

    if not steps:
        # The always-available fallback: answer from what we already have.
        steps = [PlanStep(id="s1", description="Answer the request from the retrieved evidence.")]
        audit.append(audit_event("s4_plan", "fallback_plan"))

    previous = state.get("plan")
    plan = apply_floor(
        Plan(revision=(previous.revision if previous else 1), steps=steps),
        domain.action_risk(),
        # Risk that lives in the QUESTION, not in the tools that answer it.
        # Without this an assessment plans only reads, scores medium, and the
        # approval gate never fires on the one decision that needs it.
        baseline=domain.request_risk(request),
    )

    audit.append(
        audit_event("s4_plan", "planned", revision=plan.revision,
                    steps=len(plan.steps), max_risk=plan.max_risk,
                    # The evidence that the planner actually learned specialists
                    # exist. Empty here means nothing downstream can delegate.
                    owners=[s.owner for s in plan.steps if s.owner])
    )
    return {"plan": plan, "audit": audit}
