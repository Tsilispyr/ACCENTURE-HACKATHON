"""Reference of the real tool contract, for building autospec mocks.

This replaces the original `PendingToolInterfaces` placeholder, which was
written before `tools.py` existed and guessed at the shape. Three of its
assumptions turned out to be wrong, and each would have produced mocks that
passed tests the real code could never satisfy:

* **Sync, not async.** Every tool is a synchronous `@tool` function operating on
  the in-memory estate in `world.py`. The placeholder declared them `async def`,
  so an autospec mock would have returned coroutines where the caller expects
  values.
* **`get_service_metrics` returns a mapping, not a list** -- a `ServiceMetrics`
  record whose `readings` list holds the individual measurements.
* **There is no single `execute_remediation`.** Remediation is three separate
  tools with distinct risk levels, which is the whole point: with one action the
  approval router is a constant that always routes the same way. The spread is
  what makes the decision real.

The signatures below mirror `hackathon1.tools` exactly, so `create_autospec`
rejects a call with the wrong name or arity. When a tool changes, this file has
to change with it -- that coupling is deliberate: a mock that silently accepts
a call the real tool would reject is worse than no mock.

Invoke the real tools as `search_logs.invoke({"service": ...})` -- they are
LangChain `@tool` objects, not plain functions.
"""

from typing import Any


class ToolInterfaces:
    """Signature mirror of `hackathon1.tools`. Not executed -- a spec only."""

    # -- Tier 1: investigation (read-only, safe to fan out in parallel) ------

    def search_logs(
        self, service: str, since_minutes: int = 30, level: str = "WARN"
    ) -> list[dict[str, Any]]:
        """Log entries at `level` and above, newest window first.

        Returns records with timestamp, level, message and count -- grouped by
        repeated message rather than one row per line. Defaults to WARN because
        the line identifying a root cause is often a warning; the ERROR entries
        are usually its consequences.
        """
        raise NotImplementedError("spec only -- mock this, do not call it")

    def get_service_metrics(self, service: str, window_minutes: int = 30) -> dict[str, Any]:
        """A `ServiceMetrics` mapping, NOT a list.

        Keys: service, window_minutes, collected_at, readings. Each reading
        carries current value, baseline, deviation and a `breached` flag, so a
        metric that merely looks alarming can be told from a real anomaly.

        Raises `ToolUnavailableError` when the metrics backend is down -- the
        deliberate failure path for the replanning requirement.
        """
        raise NotImplementedError("spec only -- mock this, do not call it")

    def search_knowledge_base(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        """Runbooks matching symptoms. Query on symptoms, not the service name.

        Returns entries with probable cause and recommended actions. The only
        investigation tool that returns anything for a service never seen before,
        which matters for the hidden scenario.
        """
        raise NotImplementedError("spec only -- mock this, do not call it")

    def get_incident_history(self, service: str, limit: int = 5) -> list[dict[str, Any]]:
        """Previously resolved incidents, with the cause confirmed at the time."""
        raise NotImplementedError("spec only -- mock this, do not call it")

    # -- Tier 2: remediation (mutates the estate; carries a risk level) ------
    #
    # Approval is the workflow's job, not the tool's. A test asserting the
    # approval gate should assert these are NOT invoked while approval is
    # pending or rejected -- `tools.requires_approval` decides, and the model
    # can raise that verdict but never lower it.

    def scale_connection_pool(self, service: str, new_size: int) -> dict[str, Any]:
        """Low risk. Returns an execution receipt, not a recovery verdict."""
        raise NotImplementedError("spec only -- mock this, do not call it")

    def restart_service(self, service: str) -> dict[str, Any]:
        """High risk, downgraded to medium only for services on the explicit
        low-impact allowlist. Returns a receipt, not a recovery verdict."""
        raise NotImplementedError("spec only -- mock this, do not call it")

    def rollback_change(self, service: str, change_id: str) -> dict[str, Any]:
        """Always high risk and never downgraded -- a rollback reverts
        everything else that shipped in the same change, so its blast radius is
        set by the change, not by the service."""
        raise NotImplementedError("spec only -- mock this, do not call it")

    # -- Tier 3: verification -----------------------------------------------

    def check_service_health(self, service: str) -> dict[str, Any]:
        """Keys: service, status, healthy, checks, summary.

        This is the only thing that decides whether an incident is resolved.
        Remediation tools return receipts describing what they did; recovery has
        to be observed here, which is what gives the replan loop something real
        to react to instead of a model's own claim of success.
        """
        raise NotImplementedError("spec only -- mock this, do not call it")


#: Kept so existing imports do not break. The old name said "pending"; the tools
#: are no longer pending.
PendingToolInterfaces = ToolInterfaces
