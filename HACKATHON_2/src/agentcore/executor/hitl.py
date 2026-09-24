"""Human-in-the-loop on a terminal.

The API resumes an interrupt over HTTP; this does the same thing from a shell,
which is what makes the whole pipeline demonstrable without a browser.

Two properties carried over from the reference implementation:

  * a WHILE loop, not an if. A run can pause more than once - every replan
    that raises the risk level pauses again - and an `if` silently drops the
    second pause, leaving the run half-finished and looking successful.
  * it FAILS CLOSED on EOFError. With piped or redirected stdin there is nobody
    to ask, and "nobody answered" must mean "no", never "yes".
"""

from __future__ import annotations

from typing import Any

from langgraph.types import Command

MAX_PAUSES = 10


def ask(prompt: str) -> str | None:
    """Read a line, or None on Ctrl-C and on stdin running out."""
    try:
        return input(prompt).strip()
    except (EOFError, KeyboardInterrupt):
        return None


def describe(pending: Any) -> None:
    payload = getattr(pending, "value", pending)
    if not isinstance(payload, dict):
        print(f"\nApproval needed: {payload}")
        return

    print("\n" + "-" * 70)
    print(f"APPROVAL NEEDED - {payload.get('reason', '')}")
    for step in payload.get("steps", []):
        tool = f"  [{step['tool']}]" if step.get("tool") else ""
        print(f"   {step['id']}  risk={step['risk']:<6}{tool}  {step['description']}")
    print("-" * 70)


def resume_until_done(app, state: dict, config: dict) -> dict:
    """Run, and keep answering interrupts until the graph finishes."""
    result = app.invoke(state, config)
    pauses = 0

    while "__interrupt__" in result and pauses < MAX_PAUSES:
        describe(result["__interrupt__"][0])

        answer = None
        while answer not in {"approve", "reject"}:
            raw = ask("approve / reject ? ")
            if raw is None:
                # No interactive stdin. Fail closed.
                print("(no input available - rejecting)")
                answer = "reject"
                break
            lowered = raw.lower()
            if lowered in {"a", "y", "yes", "approve"}:
                answer = "approve"
            elif lowered in {"r", "n", "no", "reject"}:
                answer = "reject"
            else:
                print("Please answer approve or reject.")

        reason = ""
        if answer == "reject":
            reason = ask("Reason (optional): ") or ""

        result = app.invoke(Command(resume={"decision": answer, "reason": reason}), config)
        pauses += 1

    if pauses >= MAX_PAUSES:
        print(f"\nStopped after {MAX_PAUSES} approval rounds - something is looping.")
    return result
