"""NiceGUI frontend, mounted onto the FastAPI app in :mod:`webserver`."""

import logging
from typing import Any

from nicegui import app as nicegui_app
from nicegui import ui

from . import db
from .config import settings
from .service import run_agent

log = logging.getLogger(__name__)

EXAMPLES = [
    "Design a rate limiter for a public REST API",
    "Plan the migration of a monolith to event-driven services",
    "Compare Postgres, ClickHouse and DuckDB for analytics on 500M rows",
]


def _session_id() -> str:
    """Stable per-browser id, used to group runs into a session."""
    return str(nicegui_app.storage.browser.get("id", "anonymous"))


def _render_result(container: ui.element, result: dict[str, Any]) -> None:
    """Draw the plan, every worker output, and the synthesized answer."""
    container.clear()
    with container:
        with ui.card().classes("w-full"):
            with ui.row().classes("items-center gap-2 w-full"):
                ui.label("Answer").classes("text-lg font-bold")
                ui.space()
                ui.badge(f"{result['duration_ms']} ms").props("color=grey-7")
                if result.get("trace_url"):
                    ui.link("View trace", result["trace_url"], new_tab=True).classes("text-sm")
            ui.separator()
            ui.markdown(result["answer"]).classes("w-full")

        with ui.card().classes("w-full"):
            ui.label(
                f"Plan - {len(result['subtasks'])} subtask(s), run in parallel"
            ).classes("text-lg font-bold")
            ui.separator()
            for item in result["results"]:
                with ui.expansion(item["title"]).classes("w-full"):
                    ui.label(item["instruction"]).classes("text-xs text-grey-7 italic")
                    ui.separator()
                    ui.markdown(item["output"]).classes("w-full")
                    ui.badge(f"{item['duration_ms']} ms").props("color=grey-7")


async def _render_history(container: ui.element) -> None:
    """List the most recent runs stored in Postgres."""
    rows = await db.recent_runs(limit=15)
    container.clear()
    with container:
        if not db.is_available():
            ui.label("History unavailable (Postgres not connected)").classes(
                "text-xs text-orange-8"
            )
            return
        if not rows:
            ui.label("No runs yet").classes("text-xs text-grey-6")
            return
        for row in rows:
            with ui.item().props("dense"):
                with ui.item_section():
                    ui.item_label(row["problem"][:70]).classes("text-sm")
                    ui.item_label(
                        f"#{row['id']} - {row['status']} - "
                        f"{row['created_at']:%Y-%m-%d %H:%M}"
                    ).props("caption")


@ui.page("/")
async def index() -> None:
    """The single page of the app."""
    ui.page_title("Orchestrator Agent")
    log.info("UI session opened: %s", _session_id())

    with ui.header().classes("items-center justify-between"):
        ui.label("Orchestrator-Workers Agent").classes("text-lg font-bold")
        with ui.row().classes("items-center gap-4"):
            ui.link("Langfuse", settings.langfuse_public_url, new_tab=True).classes("text-white")
            ui.link("API docs", "/docs", new_tab=True).classes("text-white")

    # items-start keeps the two columns from stretching to a common height.
    with ui.row().classes("w-full no-wrap items-start gap-4 p-4"):
        # ---- left: input + history ---------------------------------------
        with ui.column().classes("w-1/3 gap-4"):
            with ui.card().classes("w-full"):
                ui.label("Problem").classes("text-lg font-bold")
                # Fixed rows, not autogrow: inside a flex column autogrow
                # measures scrollHeight against a stretching parent and runs away.
                problem_input = (
                    ui.textarea(placeholder="Describe the problem to break down...")
                    .props("outlined rows=6")
                    .classes("w-full")
                )
                with ui.row().classes("items-center gap-2 w-full"):
                    solve_button = ui.button("Solve").props("color=primary")
                    spinner = ui.spinner(size="md")
                    spinner.visible = False
                ui.label("Examples").classes("text-xs text-grey-7 mt-2")
                for example in EXAMPLES:
                    ui.button(
                        example,
                        on_click=lambda _, text=example: problem_input.set_value(text),
                    ).props("flat dense no-caps align=left").classes("text-xs w-full")

            with ui.card().classes("w-full"):
                ui.label("Recent runs").classes("text-lg font-bold")
                history = ui.list().props("dense").classes("w-full")

        # ---- right: results ----------------------------------------------
        results = ui.column().classes("w-2/3 gap-4")
        with results:
            ui.label("Results will appear here.").classes("text-grey-6")

    async def on_solve() -> None:
        problem = (problem_input.value or "").strip()
        if not problem:
            ui.notify("Type a problem first", type="warning")
            return

        solve_button.disable()
        spinner.visible = True
        results.clear()
        with results:
            ui.label("Planning and dispatching workers...").classes("text-grey-6")

        try:
            result = await run_agent(problem, session_id=_session_id())
            _render_result(results, result.to_dict())
            ui.notify(f"Done in {result.duration_ms} ms", type="positive")
        except Exception as exc:
            log.exception("UI run failed")
            results.clear()
            with results:
                ui.label(f"{type(exc).__name__}: {exc}").classes("text-red-8")
            ui.notify("The run failed - see the logs", type="negative")
        finally:
            spinner.visible = False
            solve_button.enable()
            await _render_history(history)

    solve_button.on_click(on_solve)
    await _render_history(history)
