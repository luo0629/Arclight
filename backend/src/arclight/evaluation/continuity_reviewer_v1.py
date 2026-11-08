"""Offline reviewer cases; synthetic transport never establishes real model quality."""

from decimal import Decimal

from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import (
    AgentRole,
    EvaluationCase,
    EvaluationDataset,
    EvaluationExpectation,
    ExecutionDecision,
    Scenario,
    UncertaintyLevel,
)

REVIEW_MODES = ("match", "occluded", "contradiction", "missing", "recovery")
REVIEW_CASE_IDS = tuple(f"reviewer.evidence.{mode}.v1" for mode in REVIEW_MODES)


def build_continuity_reviewer_benchmark_v1() -> EvaluationDataset:
    baseline = build_benchmark_v1()
    original = next(c for c in baseline.cases if c.agent_role == AgentRole.CONTINUITY_REVIEWER)
    cases = []
    for mode, identity in zip(REVIEW_MODES, REVIEW_CASE_IDS, strict=True):
        human = mode in {"occluded", "contradiction", "missing"}
        evidence = ("review:candidate.v1", "review:reference.v1")
        tools = ("review.investigate", "review.compare")
        cases.append(
            EvaluationCase(
                case_id=identity,
                sample_id=identity + ".authored-png",
                agent_role=AgentRole.CONTINUITY_REVIEWER,
                scenarios=(
                    Scenario.TOOL_CHOICE,
                    Scenario.EVIDENCE_SUFFICIENCY,
                    Scenario.UNCERTAINTY_HANDLING,
                    Scenario.CITATION_INTEGRITY,
                ),
                material=original.material,
                human_rubrics=original.human_rubrics,
                input_payload={
                    "mode": mode,
                    "material": "Original geometric PNG and exact references; no user data.",
                },
                expectation=EvaluationExpectation(
                    allowed_tool_ids=tools,
                    required_tool_ids=tools,
                    forbidden_tool_ids=("media.select", "media.generate", "asset.replace_locked"),
                    allowed_citation_ids=evidence,
                    required_citation_ids=evidence,
                    allowed_evidence_ids=evidence,
                    required_evidence_ids=evidence,
                    allowed_asset_reference_ids=(evidence[1],),
                    required_asset_reference_ids=(evidence[1],),
                    expected_decision=ExecutionDecision.HUMAN_REVIEW
                    if human
                    else ExecutionDecision.COMPLETE,
                    minimum_uncertainty_for_review=UncertaintyLevel.HIGH if human else None,
                    maximum_latency_ms=60000,
                    maximum_cost_usd=Decimal("0.010000"),
                ),
            )
        )
    return EvaluationDataset(
        dataset_id="arclight.continuity-reviewer",
        version="v1.0.0",
        cases=(*baseline.cases, *cases),
        thresholds=baseline.thresholds,
    )
