"""Wire the nine stages into one StateGraph.

The edges here ARE the business workflow, and they are meant to be read:

    s1 -> s2 -> [refusal? -> s9] -> s3 -> s4 -> s5
    s5 -> [rejected? -> s8] -> s6 -> s7
    s7 -> s5 (revision bumped: approval was cleared)
       -> s6 (steps remain)
       -> s8 (done, or the replan cap was hit)
    s8 -> s9 -> END
"""

from __future__ import annotations

from langgraph.checkpoint.memory import MemorySaver
from langgraph.graph import END, START, StateGraph

from agentcore.pipeline import (
    s1_intake, s2_guard_in, s3_ground, s4_plan,
    s5_gate, s6_act, s7_replan, s8_compose, s9_guard_out,
)
from agentcore import observability
from agentcore.pipeline.state import AgentState


def _traced(name: str, run):
    """Wrap a stage so it opens a span named after the node.

    Span name == node name, with no mapping table to drift: a trace in App
    Insights reads `s1_intake -> ... -> s9_guard_out`, which is the same
    sequence `ls src/agentcore/pipeline/` prints.

    The audit events a stage returns are attached as span EVENTS rather than
    child spans, because `audit_event` records what happened and carries no
    timings. Turning them into spans would mean inventing durations.

    A no-op when telemetry is off, and it never swallows a stage's exception -
    the stage's own error handling stays in charge.
    """

    def node(state):
        with observability.span(name) as active:
            result = run(state)
            if active is not None and isinstance(result, dict):
                observability.record_audit(active, result.get("audit") or [])
            return result

    node.__name__ = name
    return node


def build_graph() -> StateGraph:
    graph = StateGraph(AgentState)

    for name, stage in (
        ("s1_intake", s1_intake),
        ("s2_guard_in", s2_guard_in),
        ("s3_ground", s3_ground),
        ("s4_plan", s4_plan),
        ("s5_gate", s5_gate),
        ("s6_act", s6_act),
        ("s7_replan", s7_replan),
        ("s8_compose", s8_compose),
        ("s9_guard_out", s9_guard_out),
    ):
        graph.add_node(name, _traced(name, stage.run))

    graph.add_edge(START, "s1_intake")
    graph.add_edge("s1_intake", "s2_guard_in")
    graph.add_conditional_edges("s2_guard_in", s2_guard_in.route, ["s3_ground", "s9_guard_out"])
    graph.add_edge("s3_ground", "s4_plan")
    graph.add_edge("s4_plan", "s5_gate")
    graph.add_conditional_edges("s5_gate", s5_gate.route, ["s6_act", "s8_compose"])
    graph.add_edge("s6_act", "s7_replan")
    graph.add_conditional_edges("s7_replan", s7_replan.route, ["s5_gate", "s6_act", "s8_compose"])
    graph.add_edge("s8_compose", "s9_guard_out")
    graph.add_edge("s9_guard_out", END)

    return graph


def build_app(checkpointer=None):
    """Compile. A checkpointer is REQUIRED for the interrupt at s5 to resume.

    Langfuse tracing (the course's guide 04) is bound here, ONCE, via
    `.with_config()`: a compiled graph is a Runnable, so every invoke, ainvoke
    and stream call site - service.py, chainlit_app.py, console.py,
    evaluation/* - gets it without threading a callback through each of them.
    A no-op when Langfuse is not configured; see `llm.langfuse_handler()`.

    THE ONE THING TO KNOW BEFORE CHANGING THIS: `.with_config()` on a
    `CompiledStateGraph` returns a `CompiledStateGraph`, not a
    `RunnableBinding`, so `get_state` and `stream` survive. The console and
    `service.py` both reach through this for `get_state`, and a
    `RunnableBinding` would break approval-resume the moment anyone added a
    key. Verified when D45 landed; re-verify if the binding changes shape.

    The always-on layer is the graph itself: every stage appends to
    `state["audit"]`, and `stream(stream_mode="updates")` yields once per node.
    `agentcore.tracing` renders both, with no account and no network.
    """
    from agentcore.llm import langfuse_handler

    app = build_graph().compile(checkpointer=checkpointer or MemorySaver())
    handler = langfuse_handler()
    return app.with_config(callbacks=[handler]) if handler else app
