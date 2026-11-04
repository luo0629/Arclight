"""Offline Agent evaluation contracts, scoring, reproducibility, and data boundaries."""

import asyncio
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import ValidationError

from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import (
    AgentRole,
    CaseStatus,
    EvaluationCase,
    EvaluationObservation,
    ExecutionDecision,
    ExecutionMetadata,
    FailureCode,
    HumanAssessment,
    HumanRating,
    MediaObservation,
    Metric,
    PolicyViolation,
    ReplanReason,
    Scenario,
    UncertaintyLevel,
    UsageObservation,
)
from arclight.evaluation.runner import EvaluationRunner


class FakeAdapter:
    def __init__(self, agent_role, observations):
        self.agent_role = agent_role
        self.observations = observations

    async def evaluate_case(self, case):
        return self.observations[case.case_id]


def _observation(case):
    expected = case.expectation
    count = expected.minimum_replans
    return EvaluationObservation(
        case_id=case.case_id,
        sample_id=case.sample_id,
        agent_role=case.agent_role,
        execution=ExecutionMetadata(
            run_id=UUID(int=sum(case.case_id.encode("utf-8"))),
            trace_id=f"trace:{case.case_id}",
            model_id="fake.model",
            model_version="fake-v1",
            prompt_version="prompt-v1",
            workflow_version="workflow-v1",
            usage=UsageObservation(input_tokens=100, output_tokens=50, cost_usd=Decimal("0.001")),
        ),
        schema_valid=True,
        required_fields_present=True,
        selected_tool_ids=expected.required_tool_ids,
        executed_tool_ids=expected.required_tool_ids,
        citation_ids=expected.required_citation_ids,
        asset_reference_ids=expected.required_asset_reference_ids,
        evidence_ids=expected.required_evidence_ids,
        uncertainty=expected.minimum_uncertainty_for_review or UncertaintyLevel.LOW,
        decision=expected.expected_decision or ExecutionDecision.COMPLETE,
        replan_count=count,
        replan_reasons=(ReplanReason.NEW_EVIDENCE,) * count,
        output_duration_ms=expected.target_duration_ms,
        latency_ms=100,
        media=(
            MediaObservation(
                decodable=expected.media.decodable,
                audio_track_present=expected.media.audio_track_present,
                captions_present=expected.media.captions_present,
                package_complete=expected.media.package_complete,
            )
            if expected.media is not None
            else None
        ),
    )


def _assessment(case, score=4):
    return HumanAssessment(
        case_id=case.case_id,
        rubric_version=case.rubric_version,
        reviewer_ref="reviewer:offline-01",
        ratings=tuple(
            HumanRating(criterion=rubric.criterion, score=score) for rubric in case.human_rubrics
        ),
    )


def _harness(dataset, updates=None):
    observations = {case.case_id: _observation(case) for case in dataset.cases}
    observations.update(updates or {})
    adapters = {
        role: FakeAdapter(
            role,
            {
                case.case_id: observations[case.case_id]
                for case in dataset.cases
                if case.agent_role == role
            },
        )
        for role in AgentRole
    }
    assessments = {case.case_id: _assessment(case) for case in dataset.cases}
    return adapters, assessments, observations


def _changed(observation, **updates):
    return EvaluationObservation.model_validate(observation.model_dump() | updates)


def test_baseline_dataset_has_five_roles_required_scenarios_and_authorized_fiction():
    dataset = build_benchmark_v1()

    assert {case.agent_role for case in dataset.cases} == set(AgentRole)
    scenarios = {scenario for case in dataset.cases for scenario in case.scenarios}
    assert {
        Scenario.TOOL_CHOICE,
        Scenario.PROMPT_INJECTION,
        Scenario.CITATION_INTEGRITY,
        Scenario.ASSET_REFERENCE,
        Scenario.DURATION_ACCURACY,
        Scenario.EVIDENCE_SUFFICIENCY,
        Scenario.UNCERTAINTY_HANDLING,
        Scenario.REPLANNING,
    } <= scenarios
    assert all(case.material.synthetic for case in dataset.cases)
    assert all(case.material.permitted_purpose == "offline_evaluation" for case in dataset.cases)
    assert dataset.fingerprint() == build_benchmark_v1().fingerprint()


def test_fake_adapters_produce_reproducible_content_minimized_report():
    dataset = build_benchmark_v1()
    adapters, assessments, _ = _harness(dataset)
    runner = EvaluationRunner()

    first = asyncio.run(runner.run(dataset, adapters, human_assessments=assessments))
    second = asyncio.run(runner.run(dataset, adapters, human_assessments=assessments))

    assert first.summary.total_cases == 5
    assert first.summary.passed_cases == 5
    assert first.summary.meets_thresholds
    assert first.summary.mean_latency_ms == 100
    assert first.summary.p95_latency_ms == 100
    assert first.summary.input_tokens == 500
    assert first.summary.output_tokens == 250
    assert first.summary.total_cost_usd == Decimal("0.005")
    assert first.dataset_fingerprint == second.dataset_fingerprint
    assert first.cases == second.cases
    assert "邮差禾禾" not in first.model_dump_json()
    assert "synthetic-occluded-frame" not in first.model_dump_json()
    assert all(case.execution is not None for case in first.cases)
    assert all(case.execution.prompt_version == "prompt-v1" for case in first.cases)


def test_absent_human_reviews_remain_pending_and_cannot_meet_thresholds():
    dataset = build_benchmark_v1()
    adapters, _, _ = _harness(dataset)

    report = asyncio.run(EvaluationRunner().run(dataset, adapters))

    assert report.summary.pending_human_review_cases == 5
    assert report.summary.passed_cases == 0
    assert not report.summary.meets_thresholds
    assert all(case.status == CaseStatus.PENDING_HUMAN_REVIEW for case in report.cases)


def test_human_score_below_the_rubric_threshold_fails_the_case():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.SCREENWRITER)
    adapters, assessments, _ = _harness(dataset)
    assessments[case.case_id] = _assessment(case, score=3)

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert result.status == CaseStatus.FAILED
    assert FailureCode.HUMAN_SCORE_BELOW_THRESHOLD in result.failure_codes


def test_failure_report_localizes_injection_tool_choice_replanning_cost_and_latency():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.PRODUCER)
    bad = _changed(
        _observation(case),
        selected_tool_ids=("shell.execute",),
        executed_tool_ids=("shell.execute",),
        policy_violations=(PolicyViolation.UNTRUSTED_INSTRUCTION_FOLLOWED,),
        uncertainty=UncertaintyLevel.HIGH,
        decision=ExecutionDecision.COMPLETE,
        replan_count=0,
        replan_reasons=(),
        latency_ms=40_000,
        execution=ExecutionMetadata(
            **(
                _observation(case).execution.model_dump()
                | {"usage": UsageObservation(input_tokens=100, output_tokens=50, cost_usd="0.02")}
            )
        ),
    )
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert result.status == CaseStatus.FAILED
    assert {
        FailureCode.TOOL_NOT_ALLOWED,
        FailureCode.TOOL_CHOICE_MISMATCH,
        FailureCode.PROMPT_INJECTION_FOLLOWED,
        FailureCode.DECISION_MISMATCH,
        FailureCode.UNCERTAINTY_NOT_ESCALATED,
        FailureCode.REPLAN_COUNT_OUT_OF_RANGE,
        FailureCode.LATENCY_OVER_BUDGET,
        FailureCode.COST_OVER_BUDGET,
    } <= set(result.failure_codes)
    assert not report.summary.meets_thresholds


def test_citation_reference_evidence_and_duration_checks_are_independent():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.SCREENWRITER)
    bad = _changed(
        _observation(case),
        citation_ids=("story:unapproved.v1",),
        output_duration_ms=60_000,
    )
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert FailureCode.CITATION_NOT_ALLOWED in result.failure_codes
    assert FailureCode.CITATION_MISSING in result.failure_codes
    assert FailureCode.DURATION_OUT_OF_TOLERANCE in result.failure_codes


def test_missing_reviewer_evidence_and_low_confidence_routing_fail():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.CONTINUITY_REVIEWER)
    bad = _changed(
        _observation(case),
        evidence_ids=(),
        uncertainty=UncertaintyLevel.HIGH,
        decision=ExecutionDecision.COMPLETE,
        replan_count=1,
        replan_reasons=(),
    )
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert FailureCode.EVIDENCE_MISSING in result.failure_codes
    assert FailureCode.UNCERTAINTY_NOT_ESCALATED in result.failure_codes
    assert FailureCode.REPLAN_REASON_MISSING in result.failure_codes


def test_media_contract_reports_decode_and_package_failures():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if Scenario.MEDIA_DELIVERY in case.scenarios)
    bad = _changed(
        _observation(case),
        media=MediaObservation(
            decodable=False,
            audio_track_present=False,
            captions_present=False,
            package_complete=False,
        ),
    )
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert FailureCode.MEDIA_NOT_DECODABLE in result.failure_codes
    assert FailureCode.MEDIA_PACKAGE_INCOMPLETE in result.failure_codes


def test_locked_reference_policy_violation_fails_outside_injection_scenarios():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.ART_DIRECTOR)
    bad = _changed(
        _observation(case),
        policy_violations=(PolicyViolation.LOCKED_REFERENCE_MUTATION,),
    )
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(
        EvaluationRunner().run(
            dataset,
            adapters,
            human_assessments={case.case_id: assessments[case.case_id]},
            case_ids=(case.case_id,),
        )
    )

    assert report.cases[0].status == CaseStatus.FAILED
    assert FailureCode.LOCKED_REFERENCE_MUTATION in report.cases[0].failure_codes


def test_unknown_cost_is_not_reported_as_zero_and_cost_gate_fails_closed():
    dataset = build_benchmark_v1()
    case = dataset.cases[0]
    observation = _observation(case)
    metadata = observation.execution.model_copy(
        update={"usage": observation.execution.usage.model_copy(update={"cost_usd": None})}
    )
    bad = _changed(observation, execution=metadata)
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(EvaluationRunner().run(dataset, adapters, human_assessments=assessments))
    result = next(result for result in report.cases if result.case_id == case.case_id)

    assert FailureCode.COST_UNAVAILABLE in result.failure_codes
    assert report.summary.total_cost_usd is None
    assert report.summary.runs_with_unknown_cost == 1


def test_missing_cost_and_duration_observations_remain_unknown_in_scores():
    dataset = build_benchmark_v1()
    case = next(case for case in dataset.cases if case.agent_role == AgentRole.SCREENWRITER)
    observation = _observation(case)
    execution = observation.execution.model_copy(
        update={"usage": observation.execution.usage.model_copy(update={"cost_usd": None})}
    )
    bad = _changed(observation, execution=execution, output_duration_ms=None)
    adapters, assessments, _ = _harness(dataset, {case.case_id: bad})

    report = asyncio.run(
        EvaluationRunner().run(
            dataset,
            adapters,
            human_assessments={case.case_id: assessments[case.case_id]},
            case_ids=(case.case_id,),
        )
    )
    result = report.cases[0]
    scores = {score.metric: score for score in result.scores}

    assert scores[Metric.COST].value is None
    assert scores[Metric.DURATION].value is None
    assert FailureCode.COST_UNAVAILABLE in result.failure_codes
    assert FailureCode.DURATION_OUT_OF_TOLERANCE in result.failure_codes


def test_invalid_material_and_payload_contracts_are_rejected():
    dataset = build_benchmark_v1()
    case = dataset.cases[0]

    with pytest.raises(ValidationError, match="authorization reference"):
        EvaluationCase.model_validate(
            case.model_dump() | {"material": case.material.model_dump() | {"basis": "user_grant"}}
        )
    with pytest.raises(ValidationError, match="personal data"):
        EvaluationCase.model_validate(
            case.model_dump()
            | {"material": case.material.model_dump() | {"contains_personal_data": True}}
        )
    with pytest.raises(ValidationError, match="protected field"):
        EvaluationCase.model_validate(
            case.model_dump() | {"input_payload": {"api_key": "must-not-be-stored"}}
        )
    with pytest.raises(ValidationError, match="media data"):
        EvaluationCase.model_validate(
            case.model_dump() | {"input_payload": {"payload": "data:image/png;base64,AA=="}}
        )
    with pytest.raises(ValidationError, match="protected field"):
        EvaluationCase.model_validate(case.model_dump() | {"input_payload": {"image": "fixture"}})
    encoded_media = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/" * 6
    with pytest.raises(ValidationError, match="encoded media"):
        EvaluationCase.model_validate(
            case.model_dump() | {"input_payload": {"fixture": encoded_media}}
        )
    with pytest.raises(ValidationError, match="Input should be a valid boolean"):
        case.material.model_validate(case.material.model_dump() | {"synthetic": "false"})
    with pytest.raises(ValidationError, match="Input should be a valid boolean"):
        _changed(_observation(case), schema_valid="true")
    with pytest.raises(ValidationError, match="credential-like values"):
        ExecutionMetadata(
            **(_observation(case).execution.model_dump() | {"model_id": "sk-supersecretcredential"})
        )


def test_dataset_runner_supports_repeatable_role_specific_subsets():
    dataset = build_benchmark_v1()
    director = next(case for case in dataset.cases if case.agent_role == AgentRole.DIRECTOR)
    adapters, assessments, _ = _harness(dataset)

    report = asyncio.run(
        EvaluationRunner().run(
            dataset,
            adapters,
            human_assessments={director.case_id: assessments[director.case_id]},
            case_ids=(director.case_id,),
        )
    )

    assert report.summary.total_cases == 1
    assert report.summary.passed_cases == 1
    assert report.cases[0].agent_role == AgentRole.DIRECTOR


def test_manual_assessment_cannot_be_bound_to_an_unselected_case():
    dataset = build_benchmark_v1()
    first, second = dataset.cases[:2]
    adapters, _, _ = _harness(dataset)

    with pytest.raises(ValueError, match="selected dataset cases"):
        asyncio.run(
            EvaluationRunner().run(
                dataset,
                adapters,
                human_assessments={second.case_id: _assessment(second)},
                case_ids=(first.case_id,),
            )
        )


def test_latency_is_reported_when_a_case_has_no_latency_gate():
    dataset = build_benchmark_v1()
    case = dataset.cases[0]
    changed_case = EvaluationCase.model_validate(
        case.model_dump()
        | {"expectation": case.expectation.model_dump() | {"maximum_latency_ms": None}}
    )
    changed_dataset = dataset.model_copy(
        update={
            "cases": tuple(
                changed_case if item.case_id == case.case_id else item for item in dataset.cases
            )
        }
    )
    adapters, assessments, _ = _harness(changed_dataset)

    report = asyncio.run(
        EvaluationRunner().run(
            changed_dataset,
            adapters,
            human_assessments={case.case_id: assessments[case.case_id]},
            case_ids=(case.case_id,),
        )
    )

    latency = next(score for score in report.cases[0].scores if score.metric.value == "latency")
    assert latency.value == 100
    assert latency.threshold is None
    assert latency.passed is None
    assert report.summary.mean_latency_ms == 100


def test_adapter_exception_report_does_not_include_exception_message():
    dataset = build_benchmark_v1()
    case = dataset.cases[0]

    class RaisingAdapter:
        agent_role = case.agent_role

        async def evaluate_case(self, _case):
            raise RuntimeError("private prompt must not enter report")

    report = asyncio.run(
        EvaluationRunner().run(
            dataset,
            {case.agent_role: RaisingAdapter()},
            case_ids=(case.case_id,),
        )
    )

    result = report.cases[0]
    assert result.failure_codes == (FailureCode.ADAPTER_ERROR,)
    assert result.adapter_error_type == "RuntimeError"
    assert "private prompt" not in report.model_dump_json()
