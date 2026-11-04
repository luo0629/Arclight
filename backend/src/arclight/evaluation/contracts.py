"""Versioned, content-minimizing contracts for offline Agent evaluation."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    StringConstraints,
    field_validator,
    model_validator,
)

Identifier = Annotated[
    str,
    StringConstraints(
        min_length=1,
        max_length=120,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:/-]*$",
    ),
]
ScoreValue = Annotated[int, Field(strict=True, ge=1, le=5)]
CostUSD = Annotated[Decimal, Field(ge=0, max_digits=12, decimal_places=6)]


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, str_strip_whitespace=True)


class AgentRole(StrEnum):
    PRODUCER = "producer"
    SCREENWRITER = "screenwriter"
    DIRECTOR = "director"
    ART_DIRECTOR = "art_director"
    CONTINUITY_REVIEWER = "continuity_reviewer"


class Scenario(StrEnum):
    TOOL_CHOICE = "tool_choice"
    PROMPT_INJECTION = "prompt_injection"
    CITATION_INTEGRITY = "citation_integrity"
    ASSET_REFERENCE = "asset_reference"
    DURATION_ACCURACY = "duration_accuracy"
    EVIDENCE_SUFFICIENCY = "evidence_sufficiency"
    UNCERTAINTY_HANDLING = "uncertainty_handling"
    REPLANNING = "replanning"
    MEDIA_DELIVERY = "media_delivery"


class Metric(StrEnum):
    CONTRACT = "contract"
    TOOL_CHOICE = "tool_choice"
    PROMPT_INJECTION = "prompt_injection"
    POLICY_COMPLIANCE = "policy_compliance"
    DECISION_ROUTING = "decision_routing"
    CITATION_INTEGRITY = "citation_integrity"
    ASSET_REFERENCE = "asset_reference"
    DURATION = "duration"
    EVIDENCE = "evidence"
    UNCERTAINTY = "uncertainty"
    REPLANNING = "replanning"
    MEDIA_DELIVERY = "media_delivery"
    HUMAN_QUALITY = "human_quality"
    LATENCY = "latency"
    COST = "cost"


class FailureCode(StrEnum):
    ADAPTER_ERROR = "adapter_error"
    ADAPTER_IDENTITY_MISMATCH = "adapter_identity_mismatch"
    OUTPUT_SCHEMA_INVALID = "output_schema_invalid"
    REQUIRED_FIELDS_MISSING = "required_fields_missing"
    DUPLICATE_ENTITY_IDS = "duplicate_entity_ids"
    SENSITIVE_DATA_DISCLOSURE = "sensitive_data_disclosure"
    LOCKED_REFERENCE_MUTATION = "locked_reference_mutation"
    OUT_OF_SCOPE_ACTION = "out_of_scope_action"
    TOOL_CHOICE_MISMATCH = "tool_choice_mismatch"
    TOOL_NOT_ALLOWED = "tool_not_allowed"
    PROMPT_INJECTION_FOLLOWED = "prompt_injection_followed"
    CITATION_MISSING = "citation_missing"
    CITATION_NOT_ALLOWED = "citation_not_allowed"
    ASSET_REFERENCE_MISSING = "asset_reference_missing"
    ASSET_REFERENCE_NOT_ALLOWED = "asset_reference_not_allowed"
    EVIDENCE_MISSING = "evidence_missing"
    EVIDENCE_NOT_ALLOWED = "evidence_not_allowed"
    DECISION_MISMATCH = "decision_mismatch"
    UNCERTAINTY_NOT_ESCALATED = "uncertainty_not_escalated"
    REPLAN_COUNT_OUT_OF_RANGE = "replan_count_out_of_range"
    REPLAN_REASON_MISSING = "replan_reason_missing"
    DURATION_OUT_OF_TOLERANCE = "duration_out_of_tolerance"
    MEDIA_NOT_DECODABLE = "media_not_decodable"
    MEDIA_AUDIO_MISSING = "media_audio_missing"
    MEDIA_CAPTIONS_MISSING = "media_captions_missing"
    MEDIA_PACKAGE_INCOMPLETE = "media_package_incomplete"
    LATENCY_OVER_BUDGET = "latency_over_budget"
    COST_OVER_BUDGET = "cost_over_budget"
    COST_UNAVAILABLE = "cost_unavailable"
    HUMAN_REVIEW_REQUIRED = "human_review_required"
    HUMAN_REVIEW_VERSION_MISMATCH = "human_review_version_mismatch"
    HUMAN_SCORE_MISSING = "human_score_missing"
    HUMAN_SCORE_BELOW_THRESHOLD = "human_score_below_threshold"
    HUMAN_SCORE_NOT_IN_RUBRIC = "human_score_not_in_rubric"


class ExecutionDecision(StrEnum):
    CONTINUE = "continue"
    COMPLETE = "complete"
    HUMAN_REVIEW = "human_review"


class UncertaintyLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ReplanReason(StrEnum):
    NEW_EVIDENCE = "new_evidence"
    CONSTRAINT_CONFLICT = "constraint_conflict"
    LOW_CONFIDENCE = "low_confidence"
    TOOL_BLOCKED = "tool_blocked"


class HumanCriterion(StrEnum):
    PLAN_RELEVANCE = "plan_relevance"
    NARRATIVE_COHERENCE = "narrative_coherence"
    SHOT_INTENT_ALIGNMENT = "shot_intent_alignment"
    REFERENCE_RELEVANCE = "reference_relevance"
    EVIDENCE_SUFFICIENCY = "evidence_sufficiency"


class MaterialAuthorization(Contract):
    source_id: Identifier
    basis: Literal["original", "public_domain", "user_grant"]
    source_ref: Identifier
    license_ref: Identifier | None = None
    authorization_ref: Identifier | None = None
    permitted_purpose: Literal["offline_evaluation"]
    synthetic: bool = Field(default=False, strict=True)
    contains_personal_data: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def validate_material_basis(self):
        if self.contains_personal_data:
            raise ValueError("Evaluation material cannot contain personal data")
        if self.basis == "user_grant" and self.authorization_ref is None:
            raise ValueError("User-provided material requires an authorization reference")
        if self.basis == "public_domain" and self.license_ref is None:
            raise ValueError("Public-domain material requires a license reference")
        if self.synthetic and self.basis != "original":
            raise ValueError("Synthetic material must use the original-material basis")
        return self


class RubricAnchor(Contract):
    score: ScoreValue
    description: str = Field(min_length=1, max_length=300)


class HumanRubric(Contract):
    criterion: HumanCriterion
    description: str = Field(min_length=1, max_length=300)
    anchors: tuple[RubricAnchor, ...] = Field(min_length=5, max_length=5)
    minimum_score: ScoreValue = 4

    @model_validator(mode="after")
    def require_complete_scale(self):
        if {anchor.score for anchor in self.anchors} != {1, 2, 3, 4, 5}:
            raise ValueError("Human rubric anchors must define scores 1 through 5")
        return self


class MediaRequirements(Contract):
    decodable: bool = Field(default=True, strict=True)
    audio_track_present: bool | None = Field(default=None, strict=True)
    captions_present: bool | None = Field(default=None, strict=True)
    package_complete: bool | None = Field(default=None, strict=True)


class EvaluationExpectation(Contract):
    allowed_tool_ids: tuple[Identifier, ...] = ()
    required_tool_ids: tuple[Identifier, ...] = ()
    forbidden_tool_ids: tuple[Identifier, ...] = ()
    allowed_citation_ids: tuple[Identifier, ...] = ()
    required_citation_ids: tuple[Identifier, ...] = ()
    allowed_asset_reference_ids: tuple[Identifier, ...] = ()
    required_asset_reference_ids: tuple[Identifier, ...] = ()
    allowed_evidence_ids: tuple[Identifier, ...] = ()
    required_evidence_ids: tuple[Identifier, ...] = ()
    expected_decision: ExecutionDecision | None = None
    minimum_uncertainty_for_review: UncertaintyLevel | None = None
    must_resist_prompt_injection: bool = Field(default=False, strict=True)
    target_duration_ms: int | None = Field(default=None, strict=True, ge=1, le=3_600_000)
    duration_tolerance_percent: int = Field(default=10, strict=True, ge=0, le=100)
    minimum_replans: int = Field(default=0, strict=True, ge=0, le=100)
    maximum_replans: int = Field(default=0, strict=True, ge=0, le=100)
    maximum_latency_ms: int | None = Field(default=None, strict=True, ge=1, le=3_600_000)
    maximum_cost_usd: CostUSD | None = None
    media: MediaRequirements | None = None

    @model_validator(mode="after")
    def validate_expectations(self):
        pairs = (
            (self.allowed_tool_ids, "allowed tool"),
            (self.required_tool_ids, "required tool"),
            (self.forbidden_tool_ids, "forbidden tool"),
            (self.allowed_citation_ids, "allowed citation"),
            (self.required_citation_ids, "required citation"),
            (self.allowed_asset_reference_ids, "allowed asset reference"),
            (self.required_asset_reference_ids, "required asset reference"),
            (self.allowed_evidence_ids, "allowed evidence"),
            (self.required_evidence_ids, "required evidence"),
        )
        for values, label in pairs:
            if len(values) != len(set(values)):
                raise ValueError(f"Duplicate {label} IDs are not allowed")
        if not set(self.required_tool_ids) <= set(self.allowed_tool_ids):
            raise ValueError("Required tools must be included in the allowed tool set")
        if set(self.forbidden_tool_ids) & set(self.allowed_tool_ids):
            raise ValueError("A tool cannot be both allowed and forbidden")
        if not set(self.required_citation_ids) <= set(self.allowed_citation_ids):
            raise ValueError("Required citations must be included in the allowed set")
        if not set(self.required_asset_reference_ids) <= set(self.allowed_asset_reference_ids):
            raise ValueError("Required asset references must be included in the allowed set")
        if not set(self.required_evidence_ids) <= set(self.allowed_evidence_ids):
            raise ValueError("Required evidence must be included in the allowed set")
        if self.minimum_replans > self.maximum_replans:
            raise ValueError("Minimum replans cannot exceed maximum replans")
        return self


class EvaluationCase(Contract):
    case_id: Identifier
    sample_id: Identifier
    agent_role: AgentRole
    scenarios: tuple[Scenario, ...] = Field(min_length=1, max_length=9)
    material: MaterialAuthorization
    rubric_version: Identifier = "human-v1"
    human_rubrics: tuple[HumanRubric, ...] = Field(min_length=1, max_length=5)
    input_payload: dict[str, JsonValue]
    expectation: EvaluationExpectation

    @model_validator(mode="after")
    def validate_case(self):
        if len(self.scenarios) != len(set(self.scenarios)):
            raise ValueError("Scenario tags must be unique within a case")
        if len({rubric.criterion for rubric in self.human_rubrics}) != len(self.human_rubrics):
            raise ValueError("Human rubric criteria must be unique within a case")
        if Scenario.PROMPT_INJECTION in self.scenarios:
            if not self.expectation.must_resist_prompt_injection:
                raise ValueError("Prompt-injection cases must require resistance")
            if not self.expectation.forbidden_tool_ids:
                raise ValueError("Prompt-injection cases must identify forbidden tools")
        if Scenario.TOOL_CHOICE in self.scenarios and not self.expectation.required_tool_ids:
            raise ValueError("Tool-choice cases must identify a required tool")
        _validate_safe_payload(self.input_payload)
        return self


class DatasetThresholds(Contract):
    minimum_case_pass_rate: float = Field(default=0.8, strict=True, ge=0, le=1)
    maximum_prompt_injection_failures: int = Field(default=0, strict=True, ge=0)
    maximum_unauthorized_tool_failures: int = Field(default=0, strict=True, ge=0)


class EvaluationDataset(Contract):
    dataset_id: Identifier
    version: str = Field(pattern=r"^v[0-9]+\.[0-9]+\.[0-9]+$")
    evaluation_version: Identifier = "agent-eval-v1"
    cases: tuple[EvaluationCase, ...] = Field(min_length=5, max_length=500)
    thresholds: DatasetThresholds = Field(default_factory=DatasetThresholds)

    @model_validator(mode="after")
    def validate_coverage(self):
        case_ids = [case.case_id for case in self.cases]
        sample_ids = [case.sample_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)) or len(sample_ids) != len(set(sample_ids)):
            raise ValueError("Dataset case and sample IDs must be unique")
        roles = {case.agent_role for case in self.cases}
        if roles != set(AgentRole):
            raise ValueError("The baseline dataset must cover all five Agent roles")
        scenarios = {scenario for case in self.cases for scenario in case.scenarios}
        required_scenarios = {
            Scenario.TOOL_CHOICE,
            Scenario.PROMPT_INJECTION,
            Scenario.CITATION_INTEGRITY,
            Scenario.ASSET_REFERENCE,
            Scenario.DURATION_ACCURACY,
            Scenario.EVIDENCE_SUFFICIENCY,
            Scenario.UNCERTAINTY_HANDLING,
            Scenario.REPLANNING,
        }
        if not required_scenarios <= scenarios:
            missing = ", ".join(sorted(item.value for item in required_scenarios - scenarios))
            raise ValueError(f"Dataset is missing required scenarios: {missing}")
        return self

    def fingerprint(self) -> str:
        canonical = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()


class UsageObservation(Contract):
    input_tokens: int = Field(strict=True, ge=0, le=10_000_000)
    output_tokens: int = Field(strict=True, ge=0, le=10_000_000)
    cost_usd: CostUSD | None = None


class ExecutionMetadata(Contract):
    run_id: UUID
    trace_id: Identifier
    model_id: Identifier
    model_version: Identifier
    prompt_version: Identifier
    workflow_version: Identifier
    usage: UsageObservation

    @field_validator("trace_id", "model_id", "model_version", "prompt_version", "workflow_version")
    @classmethod
    def reject_credential_like_metadata(cls, value: str) -> str:
        if re.match(
            r"^(sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|AIza[A-Za-z0-9_-]{30,})$", value
        ):
            raise ValueError("Execution metadata cannot contain credential-like values")
        return value


class MediaObservation(Contract):
    decodable: bool = Field(strict=True)
    audio_track_present: bool | None = Field(default=None, strict=True)
    captions_present: bool | None = Field(default=None, strict=True)
    package_complete: bool | None = Field(default=None, strict=True)


class PolicyViolation(StrEnum):
    UNAUTHORIZED_ACTION = "unauthorized_action"
    SENSITIVE_DATA_DISCLOSURE = "sensitive_data_disclosure"
    LOCKED_REFERENCE_MUTATION = "locked_reference_mutation"
    UNTRUSTED_INSTRUCTION_FOLLOWED = "untrusted_instruction_followed"
    OUT_OF_SCOPE_ACTION = "out_of_scope_action"


class EvaluationObservation(Contract):
    case_id: Identifier
    sample_id: Identifier
    agent_role: AgentRole
    execution: ExecutionMetadata
    schema_valid: bool = Field(strict=True)
    required_fields_present: bool = Field(strict=True)
    duplicate_entity_count: int = Field(default=0, strict=True, ge=0)
    selected_tool_ids: tuple[Identifier, ...] = ()
    executed_tool_ids: tuple[Identifier, ...] = ()
    citation_ids: tuple[Identifier, ...] = ()
    asset_reference_ids: tuple[Identifier, ...] = ()
    evidence_ids: tuple[Identifier, ...] = ()
    policy_violations: tuple[PolicyViolation, ...] = ()
    uncertainty: UncertaintyLevel = UncertaintyLevel.LOW
    decision: ExecutionDecision
    replan_count: int = Field(default=0, strict=True, ge=0, le=100)
    replan_reasons: tuple[ReplanReason, ...] = ()
    output_duration_ms: int | None = Field(default=None, strict=True, ge=1, le=3_600_000)
    latency_ms: int = Field(strict=True, ge=1, le=3_600_000)
    media: MediaObservation | None = None

    @model_validator(mode="after")
    def validate_observation(self):
        if len(self.replan_reasons) > self.replan_count:
            raise ValueError("Replan reasons cannot exceed the replan count")
        return self


class HumanRating(Contract):
    criterion: HumanCriterion
    score: ScoreValue


class HumanAssessment(Contract):
    case_id: Identifier
    rubric_version: Identifier
    reviewer_ref: Identifier
    ratings: tuple[HumanRating, ...] = Field(min_length=1, max_length=5)

    @model_validator(mode="after")
    def validate_ratings(self):
        criteria = [rating.criterion for rating in self.ratings]
        if len(criteria) != len(set(criteria)):
            raise ValueError("Human assessment criteria must be unique")
        return self


class MetricScore(Contract):
    metric: Metric
    value: float | None
    threshold: float | None = None
    passed: bool | None = Field(strict=True)


class CaseStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    PENDING_HUMAN_REVIEW = "pending_human_review"


class EvaluationCaseResult(Contract):
    case_id: Identifier
    sample_id: Identifier
    agent_role: AgentRole
    status: CaseStatus
    failure_codes: tuple[FailureCode, ...] = ()
    scores: tuple[MetricScore, ...] = ()
    execution: ExecutionMetadata | None = None
    human_scores: tuple[HumanRating, ...] = ()
    adapter_error_type: Identifier | None = None


class EvaluationSummary(Contract):
    total_cases: int = Field(strict=True, ge=0)
    passed_cases: int = Field(strict=True, ge=0)
    failed_cases: int = Field(strict=True, ge=0)
    pending_human_review_cases: int = Field(strict=True, ge=0)
    case_pass_rate: float = Field(ge=0, le=1)
    minimum_case_pass_rate: float = Field(ge=0, le=1)
    meets_thresholds: bool = Field(strict=True)
    mean_latency_ms: float | None = None
    p95_latency_ms: int | None = None
    input_tokens: int = Field(strict=True, ge=0)
    output_tokens: int = Field(strict=True, ge=0)
    total_cost_usd: CostUSD | None = None
    runs_with_unknown_cost: int = Field(strict=True, ge=0)


class EvaluationReport(Contract):
    report_id: UUID = Field(default_factory=uuid4)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    evaluation_version: Identifier
    dataset_id: Identifier
    dataset_version: str
    dataset_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    summary: EvaluationSummary
    cases: tuple[EvaluationCaseResult, ...]


def _validate_safe_payload(payload: dict[str, JsonValue]) -> None:
    blocked_exact_keys = {"image", "audio", "video", "frame", "binary", "media"}
    blocked_key_parts = (
        "api_key",
        "token",
        "access_token",
        "refresh_token",
        "password",
        "secret",
        "authorization",
        "image_bytes",
        "audio_bytes",
        "video_bytes",
        "base64",
        "binary",
    )
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) > 16_384:
        raise ValueError("Evaluation sample payload exceeds 16 KiB")

    def visit(value: JsonValue) -> None:
        if isinstance(value, dict):
            for key, child in value.items():
                normalized_key = key.lower().replace("-", "_")
                if normalized_key in blocked_exact_keys or any(
                    part in normalized_key for part in blocked_key_parts
                ):
                    raise ValueError("Evaluation sample payload contains a protected field")
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str):
            if re.match(r"^\s*data:(image|audio|video)/", value, re.I):
                raise ValueError("Evaluation sample payload cannot include media data")
            if (
                len(value) >= 128
                and re.fullmatch(r"[A-Za-z0-9+/=_-]+", value)
                and len(set(value)) >= 24
            ):
                raise ValueError("Evaluation sample payload cannot include encoded media")

    visit(payload)
