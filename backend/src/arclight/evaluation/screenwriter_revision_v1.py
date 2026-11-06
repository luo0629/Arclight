"""Revision observations supplement the five-role baseline without replacing its human rubric."""

from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import EvaluationDataset, ExecutionDecision, UncertaintyLevel
from arclight.evaluation.screenwriter_planning_v1 import build_planning_benchmark_v1

REVISION_CASE_IDS = tuple(
    f"screenwriter.revision-{mode}.v1" for mode in ("repair", "rewrite", "exhausted", "recovery")
)


def build_revision_benchmark_v1() -> EvaluationDataset:
    planning = build_planning_benchmark_v1()
    example = next(case for case in planning.cases if case.input_payload.get("mode") == "episode")
    cases = []
    for mode, identity in zip(
        ("repair", "rewrite", "exhausted", "recovery"), REVISION_CASE_IDS, strict=True
    ):
        exhausted = mode == "exhausted"
        expectation = example.expectation.model_copy(
            update={
                "allowed_tool_ids": ("story.investigate", "script.check_patch"),
                "required_tool_ids": ("story.investigate", "script.check_patch"),
                "allowed_citation_ids": ("input:paper-boat.v1",),
                "required_citation_ids": ("input:paper-boat.v1",),
                "allowed_evidence_ids": ("input:paper-boat.v1",),
                "required_evidence_ids": ("input:paper-boat.v1",),
                "expected_decision": ExecutionDecision.HUMAN_REVIEW
                if exhausted
                else ExecutionDecision.COMPLETE,
                "minimum_uncertainty_for_review": UncertaintyLevel.HIGH if exhausted else None,
                "target_duration_ms": 30000,
                "maximum_latency_ms": 60000,
            }
        )
        cases.append(
            example.model_copy(
                update={
                    "case_id": identity,
                    "sample_id": identity + ".paper-boat",
                    "input_payload": example.input_payload | {"mode": mode},
                    "expectation": expectation,
                }
            )
        )
    return EvaluationDataset(
        dataset_id="arclight.screenwriter-revision",
        version="v1.0.0",
        cases=(*build_benchmark_v1().cases, *cases),
        thresholds=planning.thresholds,
    )
