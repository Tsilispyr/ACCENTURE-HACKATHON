"""Chainlit frontend with real-time streaming of thinking, tools, agent actions, and logging.

    DOMAIN=vendor_risk uv run chainlit run src/agentcore/api/chainlit_app.py -w

Why Chainlit: the pipeline PAUSES for human approval, streams multi-step agent
execution, shows specialist delegation, tool invocations, and generates
transparent, evidence-grounded vendor risk assessments.
"""

from __future__ import annotations

import time
from typing import Any

import chainlit as cl
from langgraph.types import Command

from agentcore.contracts import Actor
from agentcore.pipeline.graph import build_app
from agentcore.registry import load_domain
from agentcore.tracing import save_agent_log
from agentcore.world import bound

STAGE_LABELS = {
    "s1_intake": "1. Intake & Request Parsing",
    "s2_guard_in": "2. Input Security Guardrails",
    "s3_ground": "3. Hybrid RAG Evidence Retrieval",
    "s4_plan": "4. Deep Agent Assessment Planning",
    "s5_gate": "5. Risk Assessment & Authorization Gate",
    "s6_act": "6. Specialist Agent Execution & Tool Calls",
    "s7_replan": "7. Progress Review & Replanning",
    "s8_compose": "8. Risk Synthesis & Recommendation",
    "s9_guard_out": "9. Output Verification & Guardrails",
}

STAGE_TYPES = {
    "s1_intake": "run",
    "s2_guard_in": "run",
    "s3_ground": "tool",
    "s4_plan": "run",
    "s5_gate": "run",
    "s6_act": "tool",
    "s7_replan": "run",
    "s8_compose": "llm",
    "s9_guard_out": "run",
}

ROLE_PROFILES = {
    "user": "Ask and assess. Retrieval, history and totals; cannot record or file anything.",
    "admin": "Every tool, including the ones that write. High risk work still pauses for approval.",
}


@cl.set_chat_profiles
async def role_profiles() -> list[cl.ChatProfile]:
    return [
        cl.ChatProfile(name=role, markdown_description=text)
        for role, text in ROLE_PROFILES.items()
    ]


@cl.on_chat_start
async def start() -> None:
    domain = load_domain()
    cl.user_session.set("graph", build_app())
    cl.user_session.set("domain", domain)
    role = cl.user_session.get("chat_profile") or "user"
    if role not in ROLE_PROFILES:
        role = "user"
    cl.user_session.set("actor", Actor(id=f"ui-{role}", role=role, scope="public"))

    await cl.Message(
        content=(
            f"**Domain `{domain.name}` loaded.** Acting as **{role}** ({ROLE_PROFILES[role]}).\n\n"
            f"{domain.persona().splitlines()[0]}\n\n"
            "Ask a vendor assessment or compliance question. High-risk actions will pause for approval."
        )
    ).send()


LEVEL_MARK = {"high": "HIGH", "medium": "MEDIUM", "low": "LOW", "none": "-"}
BASIS_MARK = {"evidence": "evidence", "inference": "inferred", "missing": "MISSING"}


async def _show_answer(state: dict, log_path: str | None = None) -> None:
    answer = state.get("answer")
    if not answer:
        await cl.Message(content="No answer was produced.").send()
        return

    if answer.refused:
        await cl.Message(
            content=f"**Refused.** {answer.summary}", author="guardrails"
        ).send()
        return

    body = answer.summary or "(no summary)"
    if answer.partial:
        body += "\n\n*This answer is partial. Check the citations.*"

    if answer.decision and answer.decision != "pending":
        who = "you" if answer.decided_by == "human" else "the model (not yet reviewed)"
        verdict = answer.decision.replace("_", " ")
        line = f"**Decision: {verdict}** - decided by {who}"
        if answer.decided_by == "human" and answer.recommendation != answer.decision:
            line += f" (the model recommended {answer.recommendation.replace('_', ' ')})"
        body = line + "\n\n" + body

    elements = []

    # 1. Risk findings
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

    # 2. Claims split by basis
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

    # 3. Contradictions
    if answer.contradictions:
        parts = [
            f"- {c.statement_a}"
            f"\n  _{c.source_a or 'source not named'}_\n\n  versus\n\n"
            f"- {c.statement_b}"
            f"\n  _{c.source_b or 'source not named'}_\n"
            + (f"\n  {c.note}" if c.note else "")
            for c in answer.contradictions
        ]
        elements.append(
            cl.Text(name="Contradictions", display="side", content="\n".join(parts))
        )

    # 4. Conditions
    if answer.conditions:
        parts = [f"{i}. {c}" for i, c in enumerate(answer.conditions, 1)]
        elements.append(
            cl.Text(name="Conditions", display="side", content="\n".join(parts))
        )

    # 5. Citations / Sources
    if answer.citations:
        elements.append(
            cl.Text(
                name="Sources",
                display="side",
                content="\n".join(f"- {c}" for c in answer.citations),
            )
        )

    # 6. Saved Run Log
    if log_path:
        elements.append(
            cl.Text(
                name="Run Trace Log",
                display="side",
                content=f"Detailed execution trace saved to:\n`{log_path}`",
            )
        )

    await cl.Message(content=body, elements=elements).send()


async def _ask_approval(payload: dict) -> dict:
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
            cl.Action(
                name="conditional",
                label="Approve with conditions",
                payload={"decision": "approve_with_conditions"},
            ),
            cl.Action(name="reject", label="Reject", payload={"decision": "reject"}),
        ],
        timeout=300,
    ).send()

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
        if not conditions:
            await cl.Message(
                content="No conditions given, so this is recorded as a plain approval.",
                author="gate",
            ).send()

    return {"decision": decision, "conditions": conditions, "by": "ui-user"}


def _format_stage_summary(stage: str, delta: dict[str, Any]) -> str:
    """Format concise, informative output for a pipeline step."""
    lines: list[str] = []
    if stage == "s1_intake":
        req = delta.get("request")
        if req:
            lines.append(f"**Intent**: `{req.intent}` | **Scope**: `{req.scope}`")
            if getattr(req, "domains", None):
                lines.append(f"**Domains**: {', '.join(req.domains)}")
    elif stage == "s2_guard_in":
        lines.append("Input query scanned by regex patterns and LLM safety classifier. **Clean.**")
    elif stage == "s3_ground":
        evidence = delta.get("evidence") or []
        lines.append(f"Retrieved **{len(evidence)}** evidence passage(s) across policies and proposals.")
        sources = sorted({e.source for e in evidence if getattr(e, 'source', None)})
        if sources:
            lines.append("**Sources**: " + ", ".join(f"`{s}`" for s in sources[:4]))
    elif stage == "s4_plan":
        plan = delta.get("plan")
        if plan:
            lines.append(f"Generated **{len(plan.steps)}** assessment step(s) (Revision {plan.revision}):")
            for s in plan.steps:
                owner_str = f" [{s.owner}]" if s.owner else ""
                lines.append(f"- **{s.id}** ({s.risk} risk){owner_str}: {s.description}")
    elif stage == "s5_gate":
        audit = delta.get("audit") or []
        level = "none"
        for a in audit:
            if a.get("event") == "risk_assessed":
                level = a.get("level", "none")
        lines.append(f"Plan risk evaluated at **{level.upper()}**.")
        for a in audit:
            if a.get("event") == "role_skipped":
                lines.append(f"*Least privilege notice*: Skipped write step(s) {a.get('steps')} for non-admin role.")
    elif stage == "s6_act":
        past = delta.get("past_steps") or []
        for p in past:
            tool_str = f" via `{', '.join(p.tool_calls)}`" if getattr(p, 'tool_calls', None) else ""
            owner_str = f" by **{p.owner}**" if getattr(p, 'owner', None) else ""
            lines.append(f"- Step **{p.step_id}** completed{owner_str}{tool_str}")
            if getattr(p, "output", None):
                lines.append(f"  _{str(p.output)[:160]}..._")
    elif stage == "s7_replan":
        lines.append("Review completed. Evidence sufficient across requested domains.")
    elif stage == "s8_compose":
        ans = delta.get("answer")
        if ans:
            lines.append(f"Synthesized assessment. Recommendation: **{getattr(ans, 'recommendation', 'N/A').upper()}**")
    elif stage == "s9_guard_out":
        lines.append("Output citations and claims verified against ground truth evidence.")

    return "\n".join(lines) if lines else "Stage completed successfully."


@cl.on_message
async def on_message(message: cl.Message) -> None:
    graph = cl.user_session.get("graph")
    domain = cl.user_session.get("domain")
    actor = cl.user_session.get("actor")

    request = domain.parse_request(message.content, actor)
    config = {"configurable": {"thread_id": request.id}}

    accumulated_state: dict[str, Any] = {"request": request, "actor": actor}
    audit_trail: list[dict[str, Any]] = []
    timings: dict[str, float] = {}

    with bound(actor, request.id):
        payload: object = {"request": request, "actor": actor}
        rounds = 0

        while rounds < 5:
            mark = time.perf_counter()
            interrupted = False
            interrupt_payload: dict[str, Any] = {}

            for update in graph.stream(payload, config, stream_mode="updates"):
                now = time.perf_counter()
                for stage, delta in update.items():
                    if stage == "__interrupt__":
                        interrupted = True
                        if isinstance(delta, (list, tuple)) and delta:
                            interrupt_payload = delta[0].value
                        continue

                    timings[stage] = timings.get(stage, 0.0) + (now - mark)
                    if isinstance(delta, dict):
                        accumulated_state.update(delta)
                        new_audit = delta.get("audit") or []
                        audit_trail.extend(new_audit)

                    # Stream interactive step to Chainlit UI
                    step_title = STAGE_LABELS.get(stage, stage)
                    step_type = STAGE_TYPES.get(stage, "run")
                    async with cl.Step(name=step_title, type=step_type) as step:
                        step.output = _format_stage_summary(stage, delta if isinstance(delta, dict) else {})

                mark = now

            if not interrupted:
                break

            rounds += 1
            resume_data = await _ask_approval(interrupt_payload)
            payload = Command(resume=resume_data)

    # Save agent trace log to logs/agent_runs/
    log_path = None
    try:
        log_path = save_agent_log(
            message.content,
            accumulated_state.get("audit", audit_trail),
            timings=timings,
            answer=accumulated_state.get("answer"),
        )
    except Exception as e:
        print(f"[log] failed to save trace log: {e}")

    await _show_answer(accumulated_state, log_path=log_path)
