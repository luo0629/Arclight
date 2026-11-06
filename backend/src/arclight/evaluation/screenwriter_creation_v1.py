"""Creation-only offline cases retain the five-role benchmark and human narrative rubric."""

from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import EvaluationDataset
from arclight.evaluation.screenwriter_planning_v1 import build_planning_benchmark_v1

CREATION_CASE_IDS = tuple(
    f"screenwriter.creation-{mode}.v1" for mode in ("season", "episode", "clarify", "retry")
)


def build_creation_benchmark_v1() -> EvaluationDataset:
    planning = build_planning_benchmark_v1()
    examples = {
        case.input_payload.get("mode"): case
        for case in planning.cases
        if "mode" in case.input_payload
    }
    cases = []
    for mode, identity in zip(
        ("season", "episode", "clarify", "retry"), CREATION_CASE_IDS, strict=True
    ):
        original = examples["episode" if mode == "retry" else mode]
        # Even a clarification has observed the original story before asking its question.
        expectation = original.expectation.model_copy(
            update={
                "allowed_evidence_ids": ("story:paper-boat.v1",),
                "required_evidence_ids": ("story:paper-boat.v1",),
                "maximum_latency_ms": 60000,
            }
        )
        cases.append(
            original.model_copy(
                update={
                    "case_id": identity,
                    "sample_id": identity + ".paper-boat",
                    "input_payload": original.input_payload | {"mode": mode},
                    "expectation": expectation,
                }
            )
        )
    return EvaluationDataset(
        dataset_id="arclight.screenwriter-creation",
        version="v1.0.0",
        cases=(*build_benchmark_v1().cases, *cases),
        thresholds=planning.thresholds,
    )
