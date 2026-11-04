"""Offline evaluation contracts and deterministic report runner."""

from arclight.evaluation.benchmark_v1 import build_benchmark_v1
from arclight.evaluation.contracts import (
    AgentRole,
    EvaluationCase,
    EvaluationDataset,
    EvaluationObservation,
    EvaluationReport,
    HumanAssessment,
)
from arclight.evaluation.runner import AgentEvaluationAdapter, EvaluationRunner

__all__ = [
    "AgentEvaluationAdapter",
    "AgentRole",
    "EvaluationCase",
    "EvaluationDataset",
    "EvaluationObservation",
    "EvaluationReport",
    "EvaluationRunner",
    "HumanAssessment",
    "build_benchmark_v1",
]
