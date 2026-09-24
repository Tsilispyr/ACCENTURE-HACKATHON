"""The Chainlit renderer, tested without a browser.

This file exists because of a real bug. The assessment view was written against
field names that did not exist - `contradiction.left` instead of
`statement_a` - and nothing caught it, because the UI is only exercised by a
person clicking. A renderer that reads model fields is code, and code that
reads fields can read the wrong ones.

`chainlit` is stubbed rather than imported, so these run in CI with no browser,
no server and no event loop of Chainlit's own.
"""

from __future__ import annotations

import asyncio
import sys
import types

import pytest

from agentcore.contracts import Answer, Claim, Contradiction, RiskFinding

pytestmark = pytest.mark.workflow


# - the stub -


class _Message:
    def __init__(self, content, elements=None, author=None):
        self.content, self.elements, self.author = content, elements or [], author
        _Message.sent.append(self)

    sent: list = []

    async def send(self):
        return self


class _Text:
    def __init__(self, name, display, content):
        self.name, self.display, self.content = name, display, content


@pytest.fixture
def view(monkeypatch):
    """Import the app against a stub chainlit, and capture what it sends."""
    stub = types.ModuleType("chainlit")
    stub.Message, stub.Text = _Message, _Text
    stub.Action = stub.AskActionMessage = stub.AskUserMessage = stub.Step = object
    stub.on_chat_start = stub.on_message = lambda f=None, **k: (
        (lambda g: g) if f is None else f
    )
    stub.set_chat_profiles = stub.on_chat_start
    stub.ChatProfile = lambda **k: types.SimpleNamespace(**k)
    stub.user_session = types.SimpleNamespace(set=lambda *a: None, get=lambda *a: None)
    monkeypatch.setitem(sys.modules, "chainlit", stub)
    sys.modules.pop("agentcore.api.chainlit_app", None)

    from agentcore.api import chainlit_app

    _Message.sent = []
    return chainlit_app


def render(view, answer: Answer) -> tuple[str, dict[str, str]]:
    asyncio.run(view._show_answer({"answer": answer}))
    message = _Message.sent[-1]
    return message.content, {e.name: e.content for e in message.elements}


# - the cases -


def full_assessment() -> Answer:
    return Answer(
        summary="Asteria carries material risk.",
        citations=["northstar.md | Section 9"],
        decision="approve_with_conditions", recommendation="reject", decided_by="human",
        conditions=["Supply the SOC 2 Type II report before signature."],
        findings=[
            RiskFinding(domain="security", level="high", assessed=True,
                        summary="Open incident.", gaps=["no pen test report"]),
            RiskFinding(domain="ai_governance", level="none", assessed=False,
                        summary="Not assessed."),
        ],
        claims=[
            Claim(statement="SOC 2 claimed not supplied", basis="evidence",
                  citations=["asteria.md | Section 2"]),
            Claim(statement="TCO exceeds the threshold", basis="inference",
                  reasoning="180k x 3 + 40k = 580k"),
            Claim(statement="No pen test evidence", basis="missing"),
            Claim(statement="Probably fine", basis="evidence"),
        ],
        contradictions=[Contradiction(
            statement_a="Data stays in the EU",
            statement_b="Sub-processor accesses from Singapore",
            source_a="asteria.md | Section 4", note="residency conflict")],
    )


def test_every_assessment_field_renders(view):
    """The regression guard: each panel reads real model fields."""
    _, panels = render(view, full_assessment())
    assert set(panels) == {
        "Risk findings", "Claims", "Contradictions", "Conditions", "Sources"
    }


def test_the_decision_line_says_who_decided(view):
    """A model recommendation and a human sign-off must never look alike."""
    body, _ = render(view, full_assessment())
    assert "decided by you" in body
    assert "the model recommended reject" in body


def test_a_model_recommendation_is_marked_as_unreviewed(view):
    answer = full_assessment()
    answer.decided_by, answer.decision = "model", "reject"
    body, _ = render(view, answer)
    assert "not yet reviewed" in body


def test_an_unassessed_domain_is_visible_not_omitted(view):
    _, panels = render(view, full_assessment())
    findings = panels["Risk findings"]
    assert "ai_governance" in findings
    assert "**no**" in findings          # the assessed column, not a blank row
    assert "no pen test report" in findings


def test_an_unsupported_claim_is_flagged(view):
    _, panels = render(view, full_assessment())
    claims = panels["Claims"]
    assert "Probably fine  **UNSUPPORTED**" in claims


def test_a_missing_claim_is_not_flagged_as_unsupported(view):
    """It is reporting an absence. That is its content, not a defect in it."""
    _, panels = render(view, full_assessment())
    section = panels["Claims"].split("### MISSING")[1]
    assert "UNSUPPORTED" not in section
    assert "not in the corpus" in section


def test_contradictions_name_both_sides_and_their_sources(view):
    _, panels = render(view, full_assessment())
    text = panels["Contradictions"]
    assert "Data stays in the EU" in text
    assert "Sub-processor accesses from Singapore" in text
    assert "asteria.md | Section 4" in text
    assert "source not named" in text    # statement_b had none
    assert "residency conflict" in text


def test_a_refusal_shows_only_the_refusal(view):
    body, panels = render(view, Answer(summary="Refused.", refused=True,
                                       claims=[Claim(statement="x")]))
    assert "**Refused.**" in body
    assert not panels


def test_a_plain_answer_renders_without_assessment_panels(view):
    """A domain with no risk domains must not grow empty side panels."""
    body, panels = render(view, Answer(summary="Forty two.",
                                       citations=["gdpr.pdf | Article 5"]))
    assert "Forty two." in body
    assert set(panels) == {"Sources"}
    assert "Decision" not in body        # 'pending' is not a decision to show


def test_a_partial_answer_says_so(view):
    body, _ = render(view, Answer(summary="Half of it.", partial=True))
    assert "partial" in body.lower()


def test_chat_history_lifecycle(view, tmp_path, monkeypatch):
    test_hist_file = tmp_path / 'chat_history.json'
    monkeypatch.setattr(view, 'HISTORY_FILE', test_hist_file)

    view._clear_chat_history()
    assert view._load_chat_history() == []

    view._append_chat_turn('user', 'Hello Asteria', 'Assessment summary')
    loaded = view._load_chat_history()
    assert len(loaded) == 1
    assert loaded[0]['user'] == 'Hello Asteria'
    assert loaded[0]['assistant'] == 'Assessment summary'

    view._clear_chat_history()
    assert view._load_chat_history() == []


def test_chat_history_replays_on_start(view, tmp_path, monkeypatch):
    test_hist_file = tmp_path / 'chat_history.json'
    monkeypatch.setattr(view, 'HISTORY_FILE', test_hist_file)

    view._append_chat_turn('user', 'Initial query', 'Initial answer')
    _Message.sent = []

    asyncio.run(view.start())
    contents = [m.content for m in _Message.sent]
    assert any('Initial query' in c for c in contents)
    assert any('Initial answer' in c for c in contents)
    assert any('Session Active for user' in c for c in contents)
