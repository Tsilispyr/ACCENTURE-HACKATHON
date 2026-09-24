"""Stage 6 - execute ONE approved step through a deep agent.

This is the only place `create_deep_agent` appears. It earns its place here:
todos, files, subagents and skills come free, and a deep agent is genuinely
better than a hand-rolled loop at "use these tools to do this one thing".

THE BOUNDARY IS THE POINT. The agent receives:
  - one step's description
  - ONLY the tools that step declared (a per-step allowlist)
  - the specialist that step was assigned to, if any
  - the evidence, in an untrusted envelope

It does not receive the plan or the other steps. Injected text saying "call
issue_refund" fails because that tool is not bound - not merely discouraged.

ONE HONEST CAVEAT, and it used to be a bigger one. `create_deep_agent` binds a
built-in filesystem suite additively, and passing `tools=` never removes it, so
for most of this project's life `write_file`, `edit_file`, `grep` and friends
were bound to every executor despite the claim above. `_no_filesystem()` now
strips all of them except `read_file`, which the library refuses to drop. That
one reads an ephemeral state backend nothing here ever writes to, so it returns
nothing. No shell tool is ever bound: `execute` appears only for a sandbox
backend, and the default StateBackend is not one.

THE TRIPWIRE. s5_gate has already approved everything this step may do, so an
interrupt surfacing from inside the agent means it tried something outside the
approved step. That is auto-rejected, the step fails, and a safety event is
written. This turns the riskiest integration unknown (nested interrupts) into a
security feature: if nesting misbehaves, the posture is unchanged.
"""

from __future__ import annotations

from typing import Any

from agentcore.contracts import StepResult
from agentcore import aio
from agentcore.llm import chat_model
from agentcore.pipeline.state import AgentState, audit_event
from agentcore.registry import load_domain
from agentcore.safety.untrusted import envelope
from agentcore.tools.registry import tools_for_step


def _instruction(domain, step, evidence, specialists=None) -> str:
    """The executor's system prompt.

    THE ORDER IS THE DESIGN. It used to read: persona, "do exactly this step and
    nothing else, do not attempt other actions", STEP, evidence, and only then a
    hedged "you may delegate to a specialist". An absolute prohibition that
    arrives first outranks a permission that arrives last, and the file's own
    tripwire frames acting outside the step as a failure - so the model was told
    delegation was simultaneously allowed and punishable, and it declined. Every
    time, in every domain.

    Now the step and its assignment come BEFORE the prohibition, so "this step"
    already includes the delegation by the time it is forbidden to do anything
    else. The prohibition sentence itself is unchanged, and the carve-out that
    follows it names only this step's own owner.

    The evidence envelope moved to the end. Untrusted extracts no longer sit
    BETWEEN two instruction blocks, so injected text cannot appear to continue
    a rule.

    `specialists` is in the signature because the call site passes it. It once
    was not, and every step execution raised TypeError - the try/except turned
    that into a plausible "step failed" the pipeline replanned around.
    """
    assignment, carve_out = "", ""
    owner = next(
        (s for s in (specialists or []) if s["name"] == (step.owner or "")), None
    )

    if owner:
        assignment = (
            f"\n\nTHIS STEP IS ASSIGNED TO {owner['name']}, who judges: "
            f"{owner['description']}\n"
            f"Executing this step MEANS calling `task` with "
            f"subagent_type=\"{owner['name']}\" and a description carrying "
            "everything it needs: the subject, what to judge, and what to return. "
            "It cannot see this conversation. Then relay its report."
        )
        carve_out = (
            f"\nHanding this step to {owner['name']} is not another action - it IS "
            "this step, and it was approved as this step."
        )
    elif specialists:
        listing = "\n".join(f"  {s['name']}: {s['description']}" for s in specialists)
        assignment = (
            "\n\nThis step has no assigned specialist, so it is yours to do.\n"
            "If a PART of it needs a different standard of judgement, hand that "
            f"part to one of these with `task`:\n{listing}"
        )
        carve_out = (
            "\nHanding part of THIS step to one of those specialists is inside "
            "this step. Anything else is outside it."
        )

    return (
        f"{domain.persona()}\n\n"
        "You are executing ONE step of a plan that has already been approved.\n\n"
        f"STEP: {step.description}"
        f"{assignment}\n\n"
        "Do exactly this step and nothing else. Do not attempt other actions."
        f"{carve_out}\n\n"
        f"{envelope(evidence)}\n\n"
        "Report what you did and what you found, concisely."
    )


def _specialist_specs(specialists, tools) -> list[dict[str, Any]]:
    """Bind every specialist to THIS STEP'S tools and nothing else.

    deepagents already inherits the parent's tools when a spec omits `tools`,
    so this changes nothing about what a specialist can reach today. What it
    changes is WHO OWNS THE INVARIANT: the per-step allowlist is a safety
    property that s5_gate approved, and it should hold because we declare it
    here, not because of a line in a third-party library that could change.

    Derived only from `tools_for_step`, never from `_all_tools` - a specialist
    must never become a way around the allowlist.
    """
    return [{**spec, "tools": list(tools)} for spec in specialists]


def _no_filesystem() -> list[Any]:
    """Strip the built-in filesystem tools, so the allowlist means what it says.

    `create_deep_agent` binds `ls`, `read_file`, `write_file`, `edit_file`,
    `glob` and `grep` to every agent, and passing `tools=` is ADDITIVE - it
    never removes a built-in. This file's own docstring claimed no tool outside
    the per-step allowlist was bound. That was not true.

    The exposure was limited: the default StateBackend is not a sandbox, so no
    `execute` tool is bound and no shell runs, and the file tools operate on
    LangGraph state rather than the host disk. But "limited" is not the claim
    the guardrail makes, and the gap had a visible cost - a live assessment
    spent eight tool calls running `grep` and `read_file` against an EMPTY
    virtual filesystem instead of calling the retrieval and TCO tools its step
    actually declared.

    `read_file` cannot be removed - FilesystemMiddleware rejects a set without
    it - so it is the one built-in that stays. That is the harmless one: it
    reads the ephemeral state backend, which nothing in this pipeline ever
    writes to, so it returns nothing. Everything that WRITES (`write_file`,
    `edit_file`, `delete`) and everything that burned turns searching an empty
    store (`grep`, `glob`, `ls`) is gone.
    """
    try:
        from deepagents.middleware.filesystem import FilesystemMiddleware

        return [FilesystemMiddleware(tools=["read_file"])]
    except Exception:  # noqa: BLE001 - a library change must not break execution
        # Failing open here is deliberate: losing the narrowing is bad, losing
        # the ability to execute a step is worse, and the real guardrail is
        # still the per-step allowlist on the tools we pass.
        return []


def _tally(result: Any) -> tuple[list[str], list[str]]:
    """(every tool name called, every subagent delegated to). Safe on a partial result.

    Recording WHO was delegated to, not just that `task` was called, is what
    keeps the metric honest: deepagents auto-injects a `general-purpose`
    subagent, so counting `task` calls alone would let a hand-off to that
    satisfy a measurement about specialists.

    Tolerates a result that never arrived, because the failure exits call this
    too - a run that delegated and then crashed should still report that it
    delegated.
    """
    called: list[str] = []
    delegated_to: list[str] = []
    for message in (result or {}).get("messages", []):
        for call in (getattr(message, "tool_calls", None) or []):
            name = call.get("name", "")
            called.append(name)
            if name == "task":
                delegated_to.append((call.get("args") or {}).get("subagent_type", ""))
    return called, delegated_to


def _run_agent(agent, message: str) -> dict[str, Any]:
    """Invoke the agent ASYNCHRONOUSLY, on the process's shared loop.

    Async for two reasons. Tools fetched over MCP are async-only --
    `langchain_mcp_adapters` returns StructuredTools with `coroutine` set and
    `func` None, so a synchronous `.invoke()` raises "StructuredTool does not
    support sync invocation" the moment the agent reaches for one. Local @tool
    functions work either way, so async is the only option covering both.

    On the SHARED loop because the MCP session those tools hold was opened on
    it. Running each step under its own `asyncio.run` closed the loop between
    steps and killed the session, which surfaced as alternating steps failing
    with "Event loop is closed" - a bug that looks like a flaky tool and is
    actually a dead bridge. See `agentcore/aio.py`.
    """
    payload = {"messages": [{"role": "user", "content": message}]}
    return aio.run(agent.ainvoke(payload))


def run(state: AgentState) -> dict[str, Any]:
    domain = load_domain()
    plan = state["plan"]
    pending = plan.pending
    if not pending:
        return {"audit": [audit_event("s6_act", "nothing_pending")]}

    step = pending[0]
    audit: list[dict[str, Any]] = []

    # Defence in depth: never execute a step from an unapproved revision, even
    # if routing somehow arrived here. s5_gate is the gate; this is the lock.
    if state.get("approved_plan_revision") != plan.revision:
        step.status = "failed"
        return {
            "plan": plan,
            "past_steps": [StepResult(step_id=step.id, ok=False,
                                      error="plan revision not approved")],
            "audit": [audit_event("s6_act", "blocked_unapproved_revision",
                                  revision=plan.revision)],
        }

    tools = tools_for_step(domain, step)
    audit.append(audit_event("s6_act", "executing", step=step.id,
                             tools=[t.name for t in tools], owner=step.owner))

    # Hoisted above the try so every exit can report what was called, including
    # the ones that fail. When `delegated` was written only on the success path,
    # a run that delegated and THEN tripped the wire measured zero delegations -
    # indistinguishable from one that never delegated at all.
    result: dict[str, Any] = {}

    try:
        import deepagents

        # Specialists are deep-agent SUBAGENTS. They earn their place when a
        # sub-task needs a different standard of judgement rather than just
        # more steps: a security reviewer and a commercial reviewer read the
        # same document and care about different things. A domain with no
        # specialists gets one executor, which is usually right.
        specialists = domain.specialists()
        # Recorded BEFORE the agent is built, so the audit says what was
        # OFFERED rather than what survived construction. When this sat after
        # the constructor, a crash in it meant the delegation metric read "no
        # specialists were offered" and passed - the audit trail agreeing with
        # a broken run because it never got far enough to disagree.
        if specialists:
            audit.append(audit_event("s6_act", "specialists_available",
                                     names=[s["name"] for s in specialists]))
        # Attribute lookup, not `from deepagents import`, so a test can replace
        # the factory on the module and have it take effect here at call time.
        agent = deepagents.create_deep_agent(
            chat_model(),
            tools=tools,
            system_prompt=_instruction(domain, step, state.get("evidence", []), specialists),
            subagents=_specialist_specs(specialists, tools) or None,
            middleware=_no_filesystem(),
        )
        result = _run_agent(agent, step.description)

        # A nested interrupt means the agent reached for something outside the
        # approved step. Fail the step; do not silently approve it.
        if "__interrupt__" in result:
            step.status = "failed"
            called, delegated_to = _tally(result)
            audit.append(audit_event("s6_act", "TRIPWIRE_unapproved_action",
                                     step=step.id, detail="nested interrupt auto-rejected",
                                     tool_calls=called, delegated=len(delegated_to),
                                     delegated_to=delegated_to))
            return {
                "plan": plan,
                "past_steps": [StepResult(step_id=step.id, ok=False, owner=step.owner or "",
                                          error="attempted an action outside the approved step")],
                "audit": audit,
            }

        messages = result.get("messages", [])
        output = getattr(messages[-1], "content", "") if messages else ""
        called, delegated_to = _tally(result)

        step.status = "done"
        audit.append(audit_event("s6_act", "step_done", step=step.id,
                                 tool_calls=called, delegated=len(delegated_to),
                                 delegated_to=delegated_to))
        return {
            "plan": plan,
            "past_steps": [StepResult(step_id=step.id, output=str(output),
                                      tool_calls=called, ok=True, owner=step.owner or "")],
            "audit": audit,
        }

    except Exception as error:  # noqa: BLE001 - a failed step is a replan, not a crash
        step.status = "failed"
        called, delegated_to = _tally(result)
        audit.append(audit_event("s6_act", "step_failed", step=step.id,
                                 error=str(error)[:160], tool_calls=called,
                                 delegated=len(delegated_to), delegated_to=delegated_to))
        return {
            "plan": plan,
            "past_steps": [StepResult(step_id=step.id, ok=False, owner=step.owner or "",
                                      error=str(error)[:300])],
            "audit": audit,
        }
