"""Chainlit frontend.

    DOMAIN=sample_policy uv run chainlit run src/agentcore/api/chainlit_app.py -w

Why Chainlit rather than the FastAPI endpoints alone: the pipeline PAUSES for
human approval, and an approval gate is only convincing when someone can see
the plan and press the button. A curl command proves the mechanism; a UI proves
the product.

What it shows, deliberately, is the WORKING rather than just the answer: which
stages ran, the plan with its risk levels, the approval prompt when one is
needed, and - for a domain that declares risk domains - the risk findings,
the claims split by what each one rests on, any contradictions, and the
conditions attached to the decision. A chat box that emits a paragraph would
hide exactly the parts worth grading.

The decision line names WHO decided. A model recommendation and a human
sign-off look identical once flattened into prose, and that is precisely the
distinction an approval workflow exists to preserve.
"""

from __future__ import annotations

import chainlit as cl
from langgraph.types import Command

from agentcore.contracts import Actor
from agentcore.pipeline.graph import build_app
from agentcore.registry import load_domain
from agentcore.world import bound

STAGE_LABELS = {
    "s1_intake": "Parsed the request",
    "s2_guard_in": "Input guardrails",
    "s3_ground": "Retrieved evidence",
    "s4_plan": "Planned the work",
    "s5_gate": "Assessed risk",
    "s6_act": "Executed a step",
    "s7_replan": "Reviewed progress",
    "s8_compose": "Composed the answer",
    "s9_guard_out": "Output guardrails",
}


# Two tiers, matching what the handout asks for and nothing more: section 9
# says "restrict sensitive MCP tools according to role/authorization" and names
# no roles. Sensitive means the tools that change something.
#
# `user` is the default and can do the whole job - ask, retrieve, assess,
# calculate a total. What it cannot do is WRITE: recording or filing an
# assessment is skipped, with the reason shown, while every other step still
# runs. A default that could not assess would teach people to pick admin,
# which hands them the write tools they never needed.
ROLE_PROFILES = {
    "user": "Ask and assess. Retrieval, history and totals; cannot record or file anything.",
    "admin": "Every tool, including the ones that write. High risk work still pauses for approval.",
}


@cl.set_chat_profiles
async def role_profiles() -> list[cl.ChatProfile]:
    """The role is chosen per chat and does not change inside one.

    A demo device, not authentication: the API takes the role from the account
    that logged in. Least privilege comes first in the list, so forgetting to
    choose fails closed rather than open.
    """
    return [cl.ChatProfile(name=role, markdown_description=text)
            for role, text in ROLE_PROFILES.items()]


@cl.on_chat_start
async def start() -> None:
    domain = load_domain()
    cl.user_session.set("graph", build_app())
    cl.user_session.set("domain", domain)
    # A demo identity. The scope is real: it is ANDed into every retrieval
    # filter, so switching it here genuinely changes what can be retrieved. The
    # role is real too: tools above its ceiling are swapped for a denial.
    role = cl.user_session.get("chat_profile") or "user"
    if role not in ROLE_PROFILES:
        role = "user"
    cl.user_session.set("actor", Actor(id=f"ui-{role}", role=role, scope="public"))

    await cl.Message(
        content=(
            f"**{domain.name}** is loaded. You are acting as **{role}**: {ROLE_PROFILES[role]}\n\n"
            f"{domain.persona().splitlines()[0]}\n\n"
            "Ask a question. High risk work will pause for your approval."
        )
    ).send()


async def _show_progress(audit: list[dict]) -> None:
    """The stages that ran, in order, once each."""
    seen, lines = set(), []
    for event in audit:
        stage = event["stage"]
        if stage in seen:
            continue
        seen.add(stage)
        lines.append(f"- {STAGE_LABELS.get(stage, stage)}")
    if lines:
        await cl.Message(content="**Steps taken**\n" + "\n".join(lines),
                         author="pipeline").send()


LEVEL_MARK = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "none": "-"}
BASIS_MARK = {"evidence": "evidence", "inference": "inferred", "missing": "MISSING"}


async def _show_answer(state: dict) -> None:
    answer = state.get("answer")
    if not answer:
        await cl.Message(content="No answer was produced.").send()
        return

    if answer.refused:
        await cl.Message(content=f"**Refused.** {answer.summary}", author="guardrails").send()
        return

    body = answer.summary or "(no summary)"
    if answer.partial:
        body += "\n\n*This answer is partial. Check the citations.*"

    # The decision line comes FIRST and says who decided. A reader who sees
    # only one line must still be able to tell a model's recommendation from a
    # human's sign-off - conflating them is how an assessment acquires an
    # authority nobody granted it.
    if answer.decision and answer.decision != "pending":
        who = "you" if answer.decided_by == "human" else "the model (not yet reviewed)"
        verdict = answer.decision.replace("_", " ")
        line = f"**Decision: {verdict}** - decided by {who}"
        if answer.decided_by == "human" and answer.recommendation != answer.decision:
            line += f" (the model recommended {answer.recommendation.replace('_', ' ')})"
        body = line + "\n\n" + body

    elements = []

    # Risk findings, one row per domain, including the ones with nothing in
    # them. An unassessed domain is a result and has to be visible as one.
    if answer.findings:
        rows = ["| Domain | Risk | Assessed | Summary |", "|---|---|---|---|"]
        for finding in answer.findings:
            mark = LEVEL_MARK.get(finding.level, finding.level)
            seen = "yes" if finding.assessed else "**no**"
            rows.append(
                f"| {finding.domain} | {mark} | {seen} | "
                f"{(finding.summary or '')[:120]} |"
            )
            for gap in finding.gaps[:3]:
                rows.append(f"|  | | | missing: {gap[:100]} |")
        elements.append(
            cl.Text(name="Risk findings", display="side", content="\n".join(rows))
        )

    # Claims, grouped by basis. The grouping IS the point: a reader should be
    # able to see at a glance how much of this rests on a document and how
    # much on the model's reasoning.
    if answer.claims:
        grouped: dict[str, list] = {}
        for claim in answer.claims:
            grouped.setdefault(claim.basis, []).append(claim)
        parts = []
        for basis in ("evidence", "inference", "missing"):
            claims = grouped.get(basis, [])
            if not claims:
                continue
            parts.append(f"### {BASIS_MARK[basis]} ({len(claims)})")
            for claim in claims:
                # A 'missing' claim has nothing to support it BY DEFINITION --
                # that is what it is reporting. Labelling it unsupported would
                # read as a defect in the assessment rather than a gap in the
                # evidence, which is the opposite of what it says.
                if claim.basis == "missing":
                    support = claim.reasoning[:120] if claim.reasoning else "not in the corpus"
                else:
                    support = (
                        ", ".join(claim.citations) if claim.citations
                        else (claim.reasoning[:120] if claim.reasoning else "no support given")
                    )
                flag = "" if claim.is_supported else "  **UNSUPPORTED**"
                parts.append(f"- {claim.statement}{flag}\n  _{support}_")
        elements.append(
            cl.Text(name="Claims", display="side", content="\n".join(parts))
        )

    if answer.contradictions:
        parts = [
            f"- {c.statement_a}"
            f"\n  _{c.source_a or 'source not named'}_"
            f"\n\n  versus\n\n"
            f"- {c.statement_b}"
            f"\n  _{c.source_b or 'source not named'}_"
            + (f"\n\n  {c.note}" if c.note else "")
            for c in answer.contradictions
        ]
        elements.append(
            cl.Text(name="Contradictions", display="side", content="\n".join(parts))
        )

    if answer.conditions:
        parts = [f"{i}. {c}" for i, c in enumerate(answer.conditions, 1)]
        elements.append(
            cl.Text(name="Conditions", display="side", content="\n".join(parts))
        )

    if answer.citations:
        elements.append(
            cl.Text(name="Sources", display="side",
                    content="\n".join(f"- {c}" for c in answer.citations))
        )

    await cl.Message(content=body, elements=elements).send()


async def _ask_approval(payload: dict) -> dict:
    """Render the plan and wait for a decision. This is the HITL gate.

    Returns the resume payload the gate reads, not a bare word, so a reviewer
    can approve WITH conditions - which the gate has always supported and this
    UI previously could not express. An approval flow that offers only yes and
    no forces a reviewer to say yes to things they meant to qualify.
    """
    steps = "\n".join(
        f"- **{s['id']}** risk `{s['risk']}` "
        f"{('via `' + s['tool'] + '`') if s.get('tool') else ''}\n"
        f"  {s['description']}"
        for s in payload.get("steps", [])
    )
    action = await cl.AskActionMessage(
        content=f"**Approval needed**\n\n{payload.get('reason','')}\n\n{steps}",
        actions=[
            cl.Action(name="approve", label="Approve", payload={"decision": "approve"}),
            cl.Action(name="conditional", label="Approve with conditions",
                      payload={"decision": "approve_with_conditions"}),
            cl.Action(name="reject", label="Reject", payload={"decision": "reject"}),
        ],
        timeout=300,
    ).send()

    # Fail closed: a timeout or a dismissed dialog is a rejection, never an
    # approval. Same rule as the CLI and the API.
    if not action:
        return {"decision": "reject", "by": "ui-user"}

    decision = action.get("payload", {}).get("decision", "reject")
    conditions: list[str] = []

    if decision == "approve_with_conditions":
        reply = await cl.AskUserMessage(
            content="What must be true? One condition per line.", timeout=300
        ).send()
        text = (reply or {}).get("output", "") if isinstance(reply, dict) else ""
        conditions = [line.strip(" -") for line in text.splitlines() if line.strip()]
        # The gate degrades a conditional approval with no conditions to a
        # plain one. Say so here rather than letting the reviewer believe they
        # attached something they did not.
        if not conditions:
            await cl.Message(
                content="No conditions given, so this is recorded as a plain approval.",
                author="gate",
            ).send()

    return {"decision": decision, "conditions": conditions, "by": "ui-user"}



@cl.on_message
async def on_message(message: cl.Message) -> None:
    graph = cl.user_session.get("graph")
    domain = cl.user_session.get("domain")
    actor = cl.user_session.get("actor")

    request = domain.parse_request(message.content, actor)
    config = {"configurable": {"thread_id": request.id}}

    async with cl.Step(name="Running the pipeline", type="run"):
        with bound(actor, request.id):
            state = graph.invoke({"request": request, "actor": actor}, config)

            rounds = 0
            while "__interrupt__" in state and rounds < 5:
                resume = await _ask_approval(state["__interrupt__"][0].value)
                state = graph.invoke(Command(resume=resume), config)
                rounds += 1

    await _show_progress(state.get("audit", []))
    await _show_answer(state)
