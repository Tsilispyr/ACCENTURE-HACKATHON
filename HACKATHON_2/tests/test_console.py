"""The terminal front end, tested without a terminal.

Two things here earn tests rather than a manual click-through.

The APPROVAL PROMPT fails closed. A timeout, a dismissed prompt, a piped stdin
that ran out - all of them must mean reject. "Nobody answered" becoming "yes"
is the worst bug this system could have, and it is one input() call away.

The RENDERING reads model fields. The Chainlit view shipped reading
`contradiction.left`, a field that does not exist, because a UI is only
exercised by a person looking at it. This one is exercised here instead.
"""

from __future__ import annotations

import builtins

import pytest

from agentcore import console
from agentcore.contracts import Answer, Claim, Contradiction, PlanStep, RiskFinding

pytestmark = pytest.mark.workflow


@pytest.fixture(autouse=True)
def no_colour(monkeypatch):
    """Assert on text, not escape codes."""
    monkeypatch.setattr(console, "STYLE", False)


def feed(monkeypatch, *answers: str):
    """Replace input() with a queue, then EOF - which is the fail closed path."""
    queue = list(answers)

    def fake(_prompt=""):
        if not queue:
            raise EOFError
        return queue.pop(0)

    monkeypatch.setattr(builtins, "input", fake)


PAYLOAD = {
    "reason": "Plan revision 1 contains high-risk steps.",
    "steps": [{"id": "s1", "description": "Record the assessment.", "tool":
               "record_assessment", "risk": "high", "owner": "security_reviewer"}],
}


# ------------------------------------------------------------ fail closed ---


def test_no_input_at_all_is_a_rejection(monkeypatch, capsys):
    """Piped stdin that ran out. The single most important case in this file."""
    feed(monkeypatch)
    assert console.ask_approval(PAYLOAD)["decision"] == "reject"


def test_a_keyboard_interrupt_is_a_rejection(monkeypatch):
    def interrupt(_prompt=""):
        raise KeyboardInterrupt

    monkeypatch.setattr(builtins, "input", interrupt)
    assert console.ask_approval(PAYLOAD)["decision"] == "reject"


def test_unparseable_answers_do_not_fall_through_to_approval(monkeypatch):
    """A reviewer typing nonsense must be re-asked, never silently approved."""
    feed(monkeypatch, "maybe", "what?", "")
    assert console.ask_approval(PAYLOAD)["decision"] == "reject"


@pytest.mark.parametrize("word", ["a", "y", "yes", "approve"])
def test_approval_words(monkeypatch, word):
    feed(monkeypatch, word)
    assert console.ask_approval(PAYLOAD)["decision"] == "approve"


@pytest.mark.parametrize("word", ["r", "n", "no", "reject"])
def test_rejection_words(monkeypatch, word):
    feed(monkeypatch, word)
    assert console.ask_approval(PAYLOAD)["decision"] == "reject"


def test_conditional_approval_collects_its_conditions(monkeypatch):
    feed(monkeypatch, "c", "Supply the SOC 2 report.", "Close the incident.", "")
    result = console.ask_approval(PAYLOAD)
    assert result["decision"] == "approve_with_conditions"
    assert result["conditions"] == ["Supply the SOC 2 report.", "Close the incident."]


def test_conditional_approval_with_no_conditions_says_so(monkeypatch, capsys):
    """The gate degrades this to a plain approval, so the reviewer must be told."""
    feed(monkeypatch, "c", "")
    result = console.ask_approval(PAYLOAD)
    assert result["conditions"] == []
    assert "plain approval" in capsys.readouterr().out


def test_the_prompt_shows_the_step_owner(monkeypatch, capsys):
    """A reviewer approving a step must see who executes it."""
    feed(monkeypatch, "r")
    console.ask_approval(PAYLOAD)
    assert "security_reviewer" in capsys.readouterr().out


# -------------------------------------------------------------- rendering ---


def full_answer() -> Answer:
    return Answer(
        summary="Asteria carries material risk.",
        citations=["northstar.md | Section 9"],
        decision="approve_with_conditions", recommendation="reject", decided_by="human",
        conditions=["Supply the SOC 2 Type II report."],
        findings=[
            RiskFinding(domain="security", level="high", assessed=True,
                        summary="Open incident.", gaps=["no pen test report"]),
            RiskFinding(domain="legal_compliance", level="none", assessed=False,
                        summary="Not assessed."),
        ],
        claims=[
            Claim(statement="SOC 2 claimed not supplied", basis="evidence",
                  citations=["asteria.md | Section 2"]),
            Claim(statement="TCO exceeds the threshold", basis="inference",
                  reasoning="180k x 3 + 40k"),
            Claim(statement="No pen test evidence", basis="missing"),
            Claim(statement="Probably fine", basis="evidence"),
        ],
        contradictions=[Contradiction(
            statement_a="Data stays in the EU",
            statement_b="Sub-processor accesses from Singapore",
            source_a="asteria.md | Section 4", note="residency conflict")],
    )


def render(capsys, answer: Answer) -> str:
    console.show_answer({"answer": answer})
    return capsys.readouterr().out


def test_every_section_renders(capsys):
    out = render(capsys, full_answer())
    for section in ("ANSWER", "DECISION", "RISK FINDINGS", "CLAIMS", "CONTRADICTIONS",
                    "SOURCES"):
        assert section in out, f"{section} missing"


def test_the_decision_names_who_decided(capsys):
    out = render(capsys, full_answer())
    assert "decided by you" in out
    assert "the model recommended reject" in out


def test_an_unassessed_domain_is_visible(capsys):
    out = render(capsys, full_answer())
    assert "legal_compliance" in out
    assert "NOT ASSESSED" in out


def test_an_unsupported_claim_is_flagged(capsys):
    assert "UNSUPPORTED" in render(capsys, full_answer())


def test_a_missing_claim_is_not_called_unsupported(capsys):
    """It reports an absence. That is its content, not a defect in it."""
    out = render(capsys, full_answer())
    section = out.split("missing (1)")[1]
    assert "UNSUPPORTED" not in section
    assert "not in the corpus" in section


def test_contradictions_show_both_sides(capsys):
    out = render(capsys, full_answer())
    assert "Data stays in the EU" in out
    assert "Sub-processor accesses from Singapore" in out
    assert "source not named" in out          # statement_b had none


def test_a_refusal_shows_only_the_refusal(capsys):
    out = render(capsys, Answer(summary="Refused.", refused=True,
                                claims=[Claim(statement="x")]))
    assert "REFUSED" in out
    assert "CLAIMS" not in out


def test_a_plain_answer_grows_no_assessment_sections(capsys):
    """A domain with no risk domains must not sprout empty headings."""
    out = render(capsys, Answer(summary="Forty two.", citations=["gdpr.pdf | Article 5"]))
    assert "Forty two." in out
    assert "RISK FINDINGS" not in out
    assert "DECISION" not in out              # 'pending' is not a decision to show


def test_a_partial_answer_says_so(capsys):
    assert "partial" in render(capsys, Answer(summary="Half.", partial=True)).lower()


def test_no_answer_at_all_is_reported_not_crashed(capsys):
    console.show_answer({})
    assert "No answer" in capsys.readouterr().out


# ------------------------------------------------------------------ plumbing ---


def test_wrap_keeps_paragraphs_and_respects_width():
    lines = console.wrap("word " * 60, indent=4)
    assert all(len(line) <= console.WIDTH for line in lines)
    assert all(line.startswith("    ") for line in lines if line)


def test_wrap_survives_an_empty_string():
    assert console.wrap("") == [""]


def test_styling_is_off_when_output_is_redirected(monkeypatch):
    """Escape codes in a piped log or a screenshot are noise."""
    monkeypatch.delenv("FORCE_COLOR", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False, raising=False)
    assert console._styling_wanted() is False


def test_no_color_is_honoured(monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    assert console._styling_wanted() is False


def test_the_plan_view_shows_owners_and_risk(capsys):
    from agentcore.contracts import Plan

    console.show_plan({"plan": Plan(revision=2, steps=[
        PlanStep(id="s1", description="Judge the security posture.",
                 risk="high", tool_hint="search_policy", owner="security_reviewer"),
    ])})
    out = capsys.readouterr().out
    assert "REVISION 2" in out       # heading() uppercases
    assert "security_reviewer" in out
    assert "search_policy" in out


def test_views_are_quiet_when_there_is_nothing_to_show(capsys):
    for view in (console.show_plan, console.show_audit, console.show_evidence):
        view({})
    out = capsys.readouterr().out
    assert out.count("Ask something first") == 3


# ------------------------------------------------------------ conversation ---
#
# The console keeps a short history so a follow up can say "and the commercial
# side?" and have "the" resolve. Each question is still its own graph run with
# its own thread - the pipeline is request shaped, and the approval gate binds
# to a plan revision WITHIN one run, so pretending a conversation is one run
# would break it. What carries across turns is context, not state.


def answer_saying(text: str, **kwargs) -> Answer:
    return Answer(summary=text, **kwargs)


def test_the_first_question_is_sent_unchanged():
    """No history, no preamble. A single question must not gain a wrapper."""
    assert console.Session().compose("What is the window?") == "What is the window?"


def test_a_follow_up_carries_the_earlier_turn():
    session = console.Session()
    session.remember("What certification is needed?", answer_saying("ISO 27001 or SOC 2."))
    text = session.compose("And the commercial side?")

    assert "What certification is needed?" in text
    assert "ISO 27001 or SOC 2." in text
    assert text.rstrip().endswith("And the commercial side?")


def test_prior_answers_are_labelled_as_context_not_instructions():
    """This feeds model output back in as input, so it is marked as data.

    The composed text still passes through s2_guard_in on every turn, so an
    injection that reached an answer is screened again on the way back in
    rather than arriving pre-trusted because we wrote it.
    """
    session = console.Session()
    session.remember("q", answer_saying("an answer"))
    text = session.compose("next")

    assert "not instructions" in text
    assert "CURRENT REQUEST:" in text


def test_history_is_capped_so_the_prompt_cannot_grow_without_bound():
    session = console.Session()
    for i in range(8):
        session.remember(f"question {i}", answer_saying(f"answer {i}"))
    text = session.compose("now what?")

    assert "question 7" in text
    assert "question 0" not in text, "an unbounded history would degrade retrieval and cost"
    assert text.count("asked:") == console.HISTORY_TURNS


def test_a_long_answer_is_truncated_in_the_carried_context():
    session = console.Session()
    session.remember("q", answer_saying("x" * 5000))
    assert len(session.compose("next")) < 5000


def test_an_empty_answer_is_not_remembered():
    session = console.Session()
    session.remember("q", answer_saying(""))
    assert session.turns == []


def test_clearing_forgets_everything():
    session = console.Session()
    session.remember("q", answer_saying("a"))
    session.clear()
    assert session.compose("next") == "next"
