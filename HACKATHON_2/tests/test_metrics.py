"""The evaluation metrics, tested against answers that are deliberately wrong.

A metric is only worth having if it FAILS when it should. Testing that a good
answer scores 1.0 proves almost nothing - a function returning 1.0 passes that
test. So every case here builds the specific defect the metric exists to catch
and asserts the metric catches it.

Offline: only `decision_quality` can call a model, and every case that reaches
its judge uses the scripted `llm` fixture. An early version of this file did
not, quietly hit the real endpoint, and failed - which is how the leading
judge prompt was found, but not a property a test suite should have.
"""

from __future__ import annotations

import pytest

from agentcore.contracts import Answer, Claim, RiskFinding
from evaluation import metrics

pytestmark = pytest.mark.workflow


def answer(**kwargs) -> Answer:
    base = {"summary": "An assessment.", "citations": ["policy.md | Section 9"]}
    return Answer(**{**base, **kwargs})


# ---------------------------------------------------------- citations ------


EVIDENCE = {
    "policy.md | Section 9": (
        "Tier one suppliers shall hold a current ISO 27001 certificate covering "
        "the supplied service, or a SOC 2 Type II report issued within twelve months."
    )
}


def test_citation_correctness_accepts_a_claim_its_source_supports():
    result = metrics.citation_correctness(
        answer(claims=[Claim(
            statement="A tier one supplier must hold a current ISO 27001 certificate",
            basis="evidence", citations=["policy.md | Section 9"],
        )]),
        EVIDENCE,
    )
    assert result.passed and result.score == 1.0


def test_citation_correctness_catches_a_source_that_says_nothing_of_the_kind():
    """The failure that matters: a real citation attached to an unrelated claim."""
    result = metrics.citation_correctness(
        answer(claims=[Claim(
            statement="The supplier maintains twenty four hour telephone support staffing",
            basis="evidence", citations=["policy.md | Section 9"],
        )]),
        EVIDENCE,
    )
    assert not result.passed
    assert result.evidence["unsupported"]


def test_citation_correctness_catches_a_fabricated_source():
    result = metrics.citation_correctness(
        answer(claims=[Claim(
            statement="The supplier holds an ISO 27001 certificate covering the service",
            basis="evidence", citations=["invented.pdf | Clause 4"],
        )]),
        EVIDENCE,
    )
    assert not result.passed


def test_citation_correctness_ignores_inference_and_missing_claims():
    """Only an 'evidence' claim promises a source. The others promise something else."""
    result = metrics.citation_correctness(
        answer(claims=[
            Claim(statement="The residency arrangement is probably non compliant",
                  basis="inference", reasoning="Sub-processor sits outside the region."),
            Claim(statement="No penetration test report was supplied", basis="missing"),
        ]),
        EVIDENCE,
    )
    assert result.passed


# The shape a REAL corpus produces: `cite()` is `source | locator`, and the
# source is the collection name, so it is identical for every chunk. The
# fixtures above used distinct sources and hid a bug that only this shape
# reveals - a matcher splitting on the pipe attached every citation to the
# first chunk.
REAL_EVIDENCE = {
    "vendor_risk | Northstar AI Governance Standard > Prohibited arrangements | page 1":
        "A supplier shall not use Northstar data to train or fine tune a model "
        "offered to other customers.",
    "vendor_risk | Northstar Third Party Risk Policy > Mandatory security controls | page 1":
        "Every tier one supplier shall hold a current ISO 27001 certificate or a "
        "SOC 2 Type II report issued within twelve months.",
}


def test_citation_correctness_discriminates_between_chunks_of_one_source():
    """The regression guard. Both claims cite correctly, and BOTH must score."""
    result = metrics.citation_correctness(
        Answer(summary="x", citations=list(REAL_EVIDENCE), claims=[
            Claim(statement="A supplier shall not use Northstar data to train a model "
                            "offered to other customers",
                  basis="evidence",
                  citations=["vendor_risk | Northstar AI Governance Standard > "
                             "Prohibited arrangements | page 1"]),
            Claim(statement="Every tier one supplier shall hold a current ISO 27001 "
                            "certificate or a SOC 2 Type II report",
                  basis="evidence",
                  citations=["vendor_risk | Northstar Third Party Risk Policy > "
                             "Mandatory security controls | page 1"]),
        ]),
        REAL_EVIDENCE,
    )
    assert result.passed and result.score == 1.0, result.evidence


def test_citation_correctness_still_catches_a_wrong_section_of_the_right_source():
    """The collection name matches; the section does not. This must fail."""
    result = metrics.citation_correctness(
        Answer(summary="x", citations=list(REAL_EVIDENCE), claims=[
            Claim(statement="Every tier one supplier shall hold a current ISO 27001 certificate",
                  basis="evidence",
                  citations=["vendor_risk | Northstar AI Governance Standard > "
                             "Prohibited arrangements | page 1"]),
        ]),
        REAL_EVIDENCE,
    )
    assert not result.passed


def test_match_citation_does_not_attach_a_citation_to_the_first_chunk():
    from agentcore.contracts import match_citation

    key = match_citation(
        "vendor_risk | Northstar Third Party Risk Policy > Mandatory security controls | page 1",
        REAL_EVIDENCE,
    )
    assert key is not None and "Mandatory security controls" in key


def test_match_citation_returns_none_for_a_document_that_was_not_retrieved():
    from agentcore.contracts import match_citation

    assert match_citation("invented.pdf | Clause 4", REAL_EVIDENCE) is None


# ------------------------------------------------------ task completion ----


def test_task_completion_counts_only_assessed_domains():
    """A finding that says 'not assessed' is honest, and still not coverage."""
    result = metrics.task_completion(
        answer(findings=[
            RiskFinding(domain="security", level="high", assessed=True),
            RiskFinding(domain="commercial", level="none", assessed=False),
        ]),
        ["security", "commercial"],
    )
    assert not result.passed
    assert result.score == 0.5
    assert result.evidence["missing"] == ["commercial"]


def test_task_completion_passes_when_every_domain_is_covered():
    result = metrics.task_completion(
        answer(findings=[
            RiskFinding(domain="security", level="high", assessed=True),
            RiskFinding(domain="commercial", level="low", assessed=True),
        ]),
        ["security", "commercial"],
    )
    assert result.passed and result.score == 1.0


# ------------------------------------------------------ tool correctness ---


AUDIT = [
    {"stage": "s6_act", "event": "step_done",
     "tool_calls": ["search_policy", "calculate_tco"], "delegated": 0},
]


def test_tool_correctness_reads_the_audit_not_the_answer():
    result = metrics.tool_correctness(AUDIT, ["calculate_tco"])
    assert result.passed and result.score == 1.0


def test_tool_correctness_fails_a_forbidden_tool_even_when_expectations_are_met():
    """A run that did everything right and also wrote to a system is a failed run."""
    audit = [{"stage": "s6_act", "event": "step_done",
              "tool_calls": ["calculate_tco", "record_assessment"]}]
    result = metrics.tool_correctness(audit, ["calculate_tco"], ["record_assessment"])
    assert not result.passed
    assert result.score == 0.0
    assert result.evidence["forbidden_used"] == ["record_assessment"]


def test_tool_correctness_with_no_expectation_still_guards_the_forbidden_list():
    audit = [{"stage": "s6_act", "event": "step_done", "tool_calls": ["raise_exception"]}]
    assert not metrics.tool_correctness(audit, [], ["raise_exception"]).passed
    assert metrics.tool_correctness(audit, [], ["record_assessment"]).passed


def test_tool_correctness_survives_an_audit_with_no_tool_calls():
    assert metrics.tool_correctness([{"stage": "s1_intake", "event": "parsed"}], []).passed


# ------------------------------------------------------ agent delegation ---


def test_agent_delegation_passes_when_no_specialist_was_offered():
    """Not delegating to specialists that do not exist is not a failure."""
    assert metrics.agent_delegation(AUDIT, expected=True).passed


def test_agent_delegation_fails_when_specialists_existed_and_went_unused():
    audit = [{"stage": "s6_act", "event": "specialists_available", "names": ["security"]},
             {"stage": "s6_act", "event": "step_done", "tool_calls": ["search_policy"],
              "delegated": 0}]
    assert not metrics.agent_delegation(audit, expected=True).passed


def test_agent_delegation_records_unnecessary_delegation_without_failing():
    """Delegating when it was not needed costs a round trip. Worth seeing, not worth failing."""
    audit = [{"stage": "s6_act", "event": "specialists_available", "names": ["security"]},
             {"stage": "s6_act", "event": "step_done", "tool_calls": ["task"], "delegated": 1}]
    result = metrics.agent_delegation(audit, expected=False)
    assert result.passed and result.score == 0.5


# ------------------------------------------------------ decision quality ---


def test_decision_quality_is_not_applicable_when_no_decision_was_called_for():
    """A policy lookup has no subject to decide about. `pending` is correct for it.

    This fired on every lookup question in a live run and failed them all, which
    is the same defect as demanding four risk domains from a one-line question.
    """
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="security", level="none", assessed=False)],
        decision="pending",
    ))
    assert result.passed and result.score == 1.0
    assert "no decision was called for" in result.detail


def test_decision_quality_fails_an_assessment_that_reached_no_verdict():
    """The same `pending`, where one WAS called for, is an unfinished assessment."""
    result = metrics.decision_quality(
        answer(findings=[RiskFinding(domain="security", level="high", assessed=True)],
               decision="pending"),
        required=True,
    )
    assert not result.passed and result.score == 0.0


def test_decision_quality_rejects_unconditional_approval_over_a_high_risk():
    """The deterministic half, which no judge gets to overrule."""
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="security", level="high", assessed=True)],
        decision="approve",
    ))
    assert not result.passed and result.score == 0.0


def test_decision_quality_rejects_conditional_approval_with_no_conditions():
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="security", level="medium", assessed=True)],
        decision="approve_with_conditions", conditions=[],
    ))
    assert not result.passed


def test_decision_quality_allows_conditional_approval_over_a_high_risk(llm):
    """High risk plus stated conditions is the normal outcome, not a defect.

    This case caught a real bug. The judge prompt used to restate the rule the
    deterministic half already enforces - "a high risk finding should not end
    in unconditional approval" - and the judge pattern matched on it, failing
    a correct approve_with_conditions as though the conditions were absent.
    The prompt now asks only what the judge alone can answer.
    """
    llm.queue(metrics.DecisionJudgment, metrics.DecisionJudgment(
        follows=True, consistent=True, conditions_testable=True,
        reasoning="The condition requires the missing report before signature.",
    ))
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="security", level="high", assessed=True)],
        decision="approve_with_conditions",
        conditions=["Supply the SOC 2 Type II report before signature."],
    ))
    assert result.passed and result.score == 1.0


def test_decision_quality_fails_when_the_judge_says_it_does_not_follow(llm):
    llm.queue(metrics.DecisionJudgment, metrics.DecisionJudgment(
        follows=False, consistent=False, conditions_testable=False,
        reasoning="The conditions do not address the open incident.",
    ))
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="security", level="medium", assessed=True)],
        decision="approve_with_conditions",
        conditions=["Continue to monitor the relationship."],
    ))
    assert not result.passed


def test_decision_quality_never_reaches_the_network_in_this_suite(monkeypatch):
    """An unreachable judge degrades to neutral rather than failing the run."""
    import agentcore.llm as llm_module

    def explode():
        raise RuntimeError("no credentials")

    monkeypatch.setattr(llm_module, "judge_model", explode)
    result = metrics.decision_quality(answer(
        findings=[RiskFinding(domain="commercial", level="low", assessed=True)],
        decision="approve",
    ))
    assert result.passed and result.score == 0.5
    assert "judge unavailable" in result.detail


# ---------------------------------------------------------------- suite ----


def test_evaluate_returns_every_metric_once(llm):
    llm.queue(metrics.DecisionJudgment, metrics.DecisionJudgment(
        follows=True, consistent=True, reasoning="ok"))
    results = metrics.evaluate(answer(), AUDIT, required_domains=[])
    assert [r.name for r in results] == [
        "citation_correctness", "task_completion", "tool_correctness",
        "agent_delegation", "decision_quality",
    ]


def test_evaluate_handles_an_empty_answer_without_raising(llm):
    """A refused or failed run still gets measured. It must not take the eval down."""
    llm.queue(metrics.DecisionJudgment, metrics.DecisionJudgment(
        follows=True, consistent=True, reasoning="ok"))
    results = metrics.evaluate(Answer(summary=""), [], required_domains=["security"])
    assert len(results) == 5
    assert not next(r for r in results if r.name == "task_completion").passed
