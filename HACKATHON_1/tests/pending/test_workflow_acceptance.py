"""Acceptance test placeholders for the actual implementation, not coverage."""

import pytest


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(tools-and-state): awaiting tool contracts and evidence/diagnosis state")
async def test_diagnosis_uses_collected_tool_evidence():
    # PLACEHOLDER: For FR03, FR05-FR07; choose a case exercising at least three
    # meaningful tools to demonstrate the minimum tool requirement.
    # TODO: Supply controlled logs, metrics, and knowledge responses through the
    # agreed tool adapters. Script the LLM's classification/diagnosis responses.
    # Assert actual tool calls, retained evidence, classification/severity, and
    # diagnosis references to evidence returned by those tools. Do not satisfy
    # this by copying a prefilled final diagnosis into the initial graph state.
    # This verifies data flow; live diagnostic quality requires a separate eval.
    raise NotImplementedError("Integrate tool evidence and diagnosis assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(remediation-and-verification): awaiting executor and health-check interfaces")
async def test_low_risk_action_is_executed_and_recovery_verified():
    # PLACEHOLDER: low-risk path and FR08, FR11-FR12.
    # TODO: Use an action classified as low risk by the agreed policy. Script
    # successful simulated execution and a subsequent healthy verification.
    # Assert execution precedes verification, the agreed low-risk route is taken,
    # and the final outcome reports recovery supported by the verification result.
    # Do not infer recovery solely from an executor's successful return value.
    raise NotImplementedError("Integrate simulated execution and verification assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(approval): awaiting interrupt/resume and rejection interfaces")
@pytest.mark.parametrize("approval_case", ["pending", "rejected"])
async def test_high_risk_action_does_not_execute_without_approval(approval_case):
    # PLACEHOLDER: FR10. approval_case is a test label, not a proposed enum.
    # TODO: Reach a high-risk proposal through the real graph; assert the executor
    # has not been called while awaiting approval. For the rejected case, deliver
    # rejection through the actual resume mechanism and again assert no execution
    # of that proposed action. Check the simulator state remains unchanged by it.
    # The exact paused/rejected outcome and any alternate plan are team decisions.
    raise NotImplementedError(f"Integrate {approval_case} approval assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(approval-resume): awaiting persisted state and approval resume contract")
async def test_approved_action_resumes_the_same_incident():
    # PLACEHOLDER: FR02, FR10-FR12.
    # TODO: Pause at approval, preserve the incident identity and gathered evidence,
    # then approve through the agreed interface. Assert the intended incident
    # resumes, the approved action executes, and verification follows. Observe
    # executor calls before and after approval rather than trusting report text.
    raise NotImplementedError("Integrate approval/resume state assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(replanning): awaiting verification result and replanning state contracts")
async def test_failed_verification_triggers_replanning_before_recovery():
    # PLACEHOLDER: FR12-FR13 and failure/replanning path.
    # TODO: First execution returns success, but verification reports unhealthy.
    # Script a revised LLM plan; its execution and verification then succeed.
    # Assert the real replanning path is entered, approval is obtained for any
    # revised high-risk action, and recovery is claimed only after verification.
    # Record call order through mocks/events; do not invent graph node names here.
    raise NotImplementedError("Integrate failed-verification/replanning assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(retry-policy): awaiting retry bound and exhausted outcome")
async def test_persistent_verification_failure_stops_at_the_agreed_bound():
    # PLACEHOLDER: robustness extension of FR13. There is not a
    # numeric retry limit or an exact terminal status. These are team decisions.
    # TODO: Keep verification unhealthy and configure the agreed attempt bound.
    # Assert termination within that bound and an unresolved/failure outcome,
    # with no fabricated healthy result. Distinguish retries from initial attempts
    # according to the implementation's policy; do not hard-code a guessed count.
    raise NotImplementedError("Integrate bounded-retry assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(tool-failure-policy): awaiting tool error/retry handling contract")
async def test_investigation_tool_failure_is_handled_without_crashing():
    # PLACEHOLDER: FR15. Existing report-storage recovery is already tested;
    # this extends coverage to a meaningful investigation tool.
    # TODO: Raise a controlled timeout at the actual tool boundary. Assert the
    # agreed recovery or controlled terminal outcome, retention of any earlier
    # evidence, and absence of invented findings from the failed tool call.
    raise NotImplementedError("Integrate investigation-tool failure assertions")


@pytest.mark.pending
@pytest.mark.skip(reason="TODO(report-contract): awaiting structured final report field names")
async def test_final_report_contains_the_required_incident_outcomes():
    # PLACEHOLDER: Section 2 and FR14. Exact JSON keys/enums are not specified.
    # TODO: Assert classification/severity, collected evidence, probable cause,
    # recommended remediation/risk, approval status where required, execution
    # result, post-action verification, final incident status, and concise report.
    # Compare those values with the actual run's evidence/actions/decisions so
    # merely returning all field names cannot count as a correct report.
    raise NotImplementedError("Integrate final-report assertions")
