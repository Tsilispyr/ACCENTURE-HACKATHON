"""The simulated enterprise systems the tools observe and act on.

Handout section 6 allows mock data; this module is that mock, built as one
*stateful* world rather than a set of canned per-tool responses. That
distinction carries two hard requirements:

* FR12/FR13. ``check_service_health`` reports recovery only when a
  remediation actually mutated this world. With stateless mocks the
  verify-fail -> replan loop is theatre, and it is worth 10 points.
* Section 10's hidden scenario. Everything the world knows lives in
  ``data/scenarios.json``, so a new scenario is a JSON entry, not a code
  change. Set ``SCENARIOS_PATH`` to point somewhere else if the organisers
  hand out a file.

Scope isolation uses a ContextVar rather than a module global: LangGraph runs
investigation nodes concurrently and FastAPI serves incidents concurrently,
and a plain global would let one incident's restart show up in another's
health check. ``incident_world()`` gives each run its own copy; the ContextVar
is copied into every asyncio task, and because the World it points at is a
single mutable object, parallel nodes still share one consistent view.
"""

from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

_DEFAULT_SCENARIOS = Path(__file__).parent / "data" / "scenarios.json"

# Words that carry no signal when matching an incident description against
# runbook symptoms -- without this every KB article matches every query.
_STOPWORDS = frozenset(
    """a an and are as at be been but by for from has have in into is it its of on or
    that the this to was were will with we our you your report reports reported
    increased increase issue issues problem problems customers users approximately
    service""".split()
)


class ToolUnavailableError(RuntimeError):
    """A simulated backend was unreachable.

    FR15 wants one tool or workflow failure handled without crashing. The
    investigation node should catch this, record the gap on ``Evidence.gaps``
    and carry on with reduced confidence -- not swallow it silently, and not
    let it end the run.
    """


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(moment: datetime) -> str:
    return moment.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def now_iso() -> str:
    """Current UTC time in the same format every tool payload uses."""
    return _iso(_now())


def _tokens(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]{3,}", text.lower()) if w not in _STOPWORDS}


class ServiceState:
    """Live state for one simulated service.

    ``metrics`` starts at the scenario's incident values and is mutated by
    ``World.apply_action``. Everything else is read-only reference data.
    """

    def __init__(self, name: str, spec: dict[str, Any]) -> None:
        self.name = name
        self.spec = spec
        self.metric_specs: dict[str, dict[str, Any]] = spec.get("metrics", {})
        self.metrics: dict[str, float] = {
            metric: float(cfg["incident"]) for metric, cfg in self.metric_specs.items()
        }
        self.flaky_metrics: bool = bool(spec.get("flaky_metrics", False))
        self.metric_calls = 0
        self.actions_applied: list[str] = []

    def baseline(self, metric: str) -> float:
        return float(self.metric_specs[metric]["baseline"])

    def reading(self, metric: str) -> dict[str, Any]:
        cfg = self.metric_specs[metric]
        baseline = float(cfg["baseline"])
        current = float(self.metrics[metric])
        unit = cfg.get("unit", "")
        breach_pct = float(cfg.get("breach_pct", 50))
        lower_is_bad = cfg.get("direction") == "lower_bad"

        if baseline:
            deviation = (current - baseline) / baseline * 100.0
        else:
            deviation = 100.0 if current else 0.0

        breached = deviation < -breach_pct if lower_is_bad else deviation > breach_pct
        return {
            "name": metric,
            "current": round(current, 2),
            "baseline": round(baseline, 2),
            "unit": unit,
            "deviation_pct": round(deviation, 1),
            "breached": breached,
        }

    def readings(self) -> list[dict[str, Any]]:
        return [self.reading(metric) for metric in self.metric_specs]


class World:
    """One incident's view of the simulated estate."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data
        self.knowledge_base: list[dict[str, Any]] = data.get("knowledge_base", [])
        self.incident_history: list[dict[str, Any]] = data.get("incident_history", [])
        self.services: dict[str, ServiceState] = {
            name: ServiceState(name, spec) for name, spec in data.get("services", {}).items()
        }
        self.action_log: list[dict[str, Any]] = []

    # -- lookup -----------------------------------------------------------

    def service(self, name: str) -> ServiceState | None:
        """Unknown services return None rather than raising.

        The hidden scenario may name a service that is not in the JSON. Tools
        degrade to empty evidence in that case; the knowledge base is
        service-agnostic, so the agent still has something to reason from.
        """
        return self.services.get(name.strip().lower())

    def known_services(self) -> list[str]:
        return sorted(self.services)

    # -- observation ------------------------------------------------------

    def logs(self, service: str, since_minutes: int, level: str | None) -> list[dict[str, Any]]:
        state = self.service(service)
        if state is None:
            return []
        wanted = (level or "").upper()
        order = {"ERROR": 3, "WARN": 2, "INFO": 1, "DEBUG": 0}
        floor = order.get(wanted, 0) if wanted and wanted != "ALL" else 0
        entries = []
        for raw in state.spec.get("logs", []):
            if raw["minutes_ago"] > since_minutes:
                continue
            if order.get(raw["level"], 0) < floor:
                continue
            entries.append(
                {
                    "timestamp": _iso(_now() - timedelta(minutes=raw["minutes_ago"])),
                    "level": raw["level"],
                    "service": state.name,
                    "message": raw["message"],
                    "count": raw.get("count", 1),
                }
            )
        return sorted(entries, key=lambda e: e["count"], reverse=True)

    def metrics(self, service: str) -> list[dict[str, Any]]:
        state = self.service(service)
        if state is None:
            return []
        state.metric_calls += 1
        # FR15: deterministic, first-call-only failure. Retrying works, which
        # is what makes it a *handled* failure rather than a dead end.
        if state.flaky_metrics and state.metric_calls == 1:
            raise ToolUnavailableError(
                f"metrics backend returned 503 for {state.name} (collector busy); retry is expected to succeed"
            )
        return state.readings()

    def changes(self, service: str, hours: int) -> list[dict[str, Any]]:
        state = self.service(service)
        if state is None:
            return []
        events = []
        for raw in state.spec.get("recent_changes", []):
            if raw["hours_ago"] > hours:
                continue
            events.append(
                {
                    "change_id": raw["change_id"],
                    "service": state.name,
                    "type": raw["type"],
                    "description": raw["description"],
                    "occurred_at": _iso(_now() - timedelta(hours=raw["hours_ago"])),
                    "author": raw["author"],
                    "rollback_available": raw.get("rollback_available", False),
                }
            )
        return sorted(events, key=lambda e: e["occurred_at"], reverse=True)

    def history(self, service: str, limit: int) -> list[dict[str, Any]]:
        matches = [h for h in self.incident_history if h["service"] == service.strip().lower()]
        return sorted(matches, key=lambda h: h["occurred_at"], reverse=True)[:limit]

    def search_kb(self, query: str, limit: int) -> list[dict[str, Any]]:
        """Keyword-overlap search, scored against title plus symptoms.

        Service-agnostic on purpose: this is the one investigation tool that
        still returns something useful when the incident names a service the
        world has never heard of.
        """
        query_tokens = _tokens(query)
        if not query_tokens:
            return []
        scored: list[tuple[float, dict[str, Any]]] = []
        for article in self.knowledge_base:
            article_tokens = _tokens(article["title"] + " " + " ".join(article["symptoms"]))
            overlap = query_tokens & article_tokens
            if not overlap:
                continue
            relevance = len(overlap) / len(query_tokens | article_tokens)
            scored.append(
                (
                    relevance,
                    {
                        "id": article["id"],
                        "title": article["title"],
                        "symptoms": article["symptoms"],
                        "probable_cause": article["probable_cause"],
                        "recommended_actions": article["recommended_actions"],
                        "relevance": round(relevance, 3),
                    },
                )
            )
        scored.sort(key=lambda pair: pair[0], reverse=True)
        return [article for _, article in scored[:limit]]

    def health(self, service: str) -> dict[str, Any]:
        state = self.service(service)
        if state is None:
            return {
                "service": service,
                "status": "unknown",
                "healthy": False,
                "checks": [],
                "summary": f"{service} is not registered with the monitoring platform.",
            }
        checks = state.readings()
        breached = [c for c in checks if c["breached"]]
        if not breached:
            status, healthy = "healthy", True
            summary = f"{state.name}: all {len(checks)} monitored metrics within tolerance."
        elif len(breached) >= 4:
            status, healthy = "down", False
            summary = f"{state.name}: {len(breached)} metrics breached -- " + ", ".join(
                f"{c['name']} {c['current']}{c['unit']} vs {c['baseline']}{c['unit']}" for c in breached
            )
        else:
            status, healthy = "degraded", False
            summary = f"{state.name}: {len(breached)} metric(s) breached -- " + ", ".join(
                f"{c['name']} {c['current']}{c['unit']} vs {c['baseline']}{c['unit']}" for c in breached
            )
        return {
            "service": state.name,
            "status": status,
            "healthy": healthy,
            "checks": checks,
            "summary": summary,
        }

    # -- mutation ---------------------------------------------------------

    def apply_action(self, action: str, service: str, params: dict[str, Any]) -> dict[str, Any]:
        """Apply a simulated remediation and return an operator-style receipt.

        The receipt says what the action *did*, never whether it worked. The
        world knows the effect; leaking it here would let the agent skip
        verification and still look correct, and FR12 only means something if
        recovery has to be observed through ``health()``.
        """
        state = self.service(service)
        if state is None:
            return {
                "status": "failed",
                "message": f"{service} is not managed by the automation platform; no action taken.",
            }

        entry = state.spec.get("remediation", {}).get(action)
        if entry is None:
            return {
                "status": "rejected",
                "message": f"{action} is not available for {state.name}.",
            }

        # A remediation can depend on an earlier one (raising a pool ceiling
        # only takes effect after a restart), or on being pointed at the right
        # object (rolling back the change that actually caused the incident).
        required_param = entry.get("requires_param")
        if required_param and any(str(params.get(k)) != str(v) for k, v in required_param.items()):
            entry = {**entry, **entry.get("wrong_param", {"effect": "none"})}
        elif any(prior not in state.actions_applied for prior in entry.get("requires", [])):
            entry = {**entry, **entry.get("without_prerequisite", {"effect": "none"})}

        effect = entry.get("effect", "none")
        if effect == "full":
            state.metrics = {m: state.baseline(m) for m in state.metric_specs}
        elif effect == "partial":
            for metric, value in entry.get("metrics", {}).items():
                if metric in state.metrics:
                    state.metrics[metric] = float(value)

        state.actions_applied.append(action)
        message = entry.get("message", f"{action} executed.")
        try:
            message = message.format(**params)
        except (KeyError, IndexError):
            pass  # a placeholder the caller did not supply; the raw text is fine

        record = {
            "action": action,
            "service": state.name,
            "params": dict(params),
            "status": "success",
            "message": message,
            "executed_at": _iso(_now()),
        }
        self.action_log.append(record)
        return {"status": "success", "message": message}


# ---------------------------------------------------------------------------
# Loading and scoping
# ---------------------------------------------------------------------------

_raw_cache: dict[str, Any] | None = None
_cache_lock = threading.Lock()


def load_scenarios(path: str | Path | None = None) -> dict[str, Any]:
    """Read scenarios.json once and hand out deep copies from then on."""
    global _raw_cache
    source = Path(path or os.getenv("SCENARIOS_PATH") or _DEFAULT_SCENARIOS)
    if path is None and os.getenv("SCENARIOS_PATH") is None:
        with _cache_lock:
            if _raw_cache is None:
                _raw_cache = json.loads(source.read_text(encoding="utf-8"))
            return json.loads(json.dumps(_raw_cache))
    return json.loads(source.read_text(encoding="utf-8"))


def new_world(path: str | Path | None = None) -> World:
    return World(load_scenarios(path))


_active_world: ContextVar[World | None] = ContextVar("active_world", default=None)


def active_world() -> World:
    """The World the current incident run is bound to.

    Falls back to a lazily created process-wide world so a tool called
    outside ``incident_world()`` -- from a notebook, a smoke test, or the
    /chat agent -- still works.
    """
    world = _active_world.get()
    if world is None:
        world = new_world()
        _active_world.set(world)
    return world


@contextmanager
def incident_world(path: str | Path | None = None) -> Iterator[World]:
    """Bind a fresh World for one incident run.

    Wrap the graph invocation in this (see service.py) so each POST
    /incidents starts from the scenario's incident state and cannot see
    another request's remediations.
    """
    world = new_world(path)
    token = _active_world.set(world)
    try:
        yield world
    finally:
        _active_world.reset(token)


def reset_world(path: str | Path | None = None) -> World:
    """Replace the active world with a fresh one. Handy in tests."""
    world = new_world(path)
    _active_world.set(world)
    return world
