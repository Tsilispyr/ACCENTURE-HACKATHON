"""Every measurement, appended to one file. Numbers matter, so keep them.

A result printed to a terminal is gone the moment the terminal scrolls. That is
fine while you are iterating and useless afterwards, when the questions become
"did that change help?" and "what did we actually measure?" - and at the end,
when a slide needs a number that was true.

So every eval writes a row here. Append-only, never rewritten: a ledger you can
edit is a ledger you cannot trust.

Two formats, same rows:
    results.json   full fidelity, nested detail preserved
    results.csv    opens in Excel, pastes into a slide

    from evaluation.ledger import record
    record(domain="sample_policy", experiment="retrieval",
           arm="vector", metrics={"recall@1": 0.91, "mrr": 0.932})
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Outputs live OUTSIDE the package, at the repo root. They are results, not
# code: a reader looking for the numbers should find them without opening
# src/, and a package directory that fills up with generated files stops
# being reviewable.
RESULTS_DIR = Path(__file__).resolve().parents[2] / "evaluation-results"
JSON_PATH = RESULTS_DIR / "results.json"
CSV_PATH = RESULTS_DIR / "results.csv"

CSV_COLUMNS = ["at", "domain", "experiment", "arm", "metric", "value", "n", "note"]


def _load() -> list[dict[str, Any]]:
    if not JSON_PATH.exists():
        return []
    try:
        return json.loads(JSON_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        # A corrupt ledger must not take the eval down with it. Keep the file
        # for inspection and start a fresh one beside it.
        JSON_PATH.rename(JSON_PATH.with_suffix(".json.corrupt"))
        return []


def record(
    *,
    domain: str,
    experiment: str,
    arm: str,
    metrics: dict[str, float],
    n: int | None = None,
    note: str = "",
    detail: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append one measurement.

    experiment groups comparable arms ("retrieval", "agent", "latency");
    arm is the thing being compared ("vector", "hybrid", "deterministic").
    """
    entry = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "domain": domain,
        "experiment": experiment,
        "arm": arm,
        "metrics": {k: round(float(v), 4) for k, v in metrics.items()},
        "n": n,
        "note": note,
        "detail": detail or {},
    }

    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    rows = _load()
    rows.append(entry)
    JSON_PATH.write_text(json.dumps(rows, indent=2), encoding="utf-8")

    # The CSV is flat: one row per METRIC, not per run, so a spreadsheet can
    # pivot it without anyone parsing a nested cell.
    write_header = not CSV_PATH.exists()
    with CSV_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS)
        if write_header:
            writer.writeheader()
        for metric, value in entry["metrics"].items():
            writer.writerow(
                {
                    "at": entry["at"], "domain": domain, "experiment": experiment,
                    "arm": arm, "metric": metric, "value": value,
                    "n": n if n is not None else "", "note": note,
                }
            )
    return entry


def load(experiment: str | None = None, domain: str | None = None) -> list[dict[str, Any]]:
    rows = _load()
    if experiment:
        rows = [r for r in rows if r["experiment"] == experiment]
    if domain:
        rows = [r for r in rows if r["domain"] == domain]
    return rows


def latest(experiment: str, domain: str | None = None) -> dict[str, dict[str, float]]:
    """The most recent value per arm. What a chart should plot.

    Re-running an experiment should update the picture, not add a second bar
    beside the first.
    """
    out: dict[str, dict[str, float]] = {}
    for row in load(experiment, domain):
        out[row["arm"]] = row["metrics"]
    return out


def summary() -> str:
    rows = _load()
    if not rows:
        return "No results recorded yet."

    lines = [f"{len(rows)} measurement(s) recorded", ""]
    experiments: dict[str, list[dict]] = {}
    for row in rows:
        experiments.setdefault(row["experiment"], []).append(row)

    for name, group in experiments.items():
        lines.append(f"{name}:")
        seen: dict[str, dict] = {}
        for row in group:
            seen[f"{row['domain']}/{row['arm']}"] = row
        for key, row in seen.items():
            metrics = "  ".join(f"{k}={v}" for k, v in row["metrics"].items())
            lines.append(f"   {key:<32} {metrics}")
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    print(summary())
