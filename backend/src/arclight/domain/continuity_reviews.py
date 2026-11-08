"""Versioned advisory review results; domain services retain all production authority."""

import json
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from arclight.domain.enums import RunStatus
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.production_state import ProductionContract
from arclight.domain.versions import VersionRegistration, digest
from arclight.domain.workflow_contracts import ProductionComponent, WorkflowResult

REVIEW_VERSION: Literal["continuity-review/1"] = "continuity-review/1"
REVIEW_SCHEMA_VERSION = "continuity-review/1.1"
Checksum = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Brief = Annotated[str, Field(min_length=1, max_length=500, pattern=r"\S")]
Unit = Annotated[float, Field(strict=True, ge=0, le=1)]
Risk = Literal["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
Action = Literal["NONE", "REVISE_PROMPT", "REGENERATE", "REQUEST_EVIDENCE", "HUMAN_REVIEW"]


class ReviewTarget(ProductionContract):
    candidate_id: UUID
    asset_id: UUID
    asset_version_id: UUID
    input_manifest_id: UUID
    media_check_sha256: Checksum
    storyboard_id: UUID
    shot_id: UUID


class ReviewReference(ProductionContract):
    reference_id: UUID
    role: Literal[
        "CANDIDATE", "TARGET_SHOT", "STORYBOARD", "REFERENCE", "ADJACENT_SHOT", "VISUAL_BIBLE"
    ]
    resource_id: UUID
    content_sha256: Checksum
    asset_id: UUID | None = None
    asset_type: (
        Literal[
            "CHARACTER",
            "COSTUME",
            "SCENE",
            "PROP",
            "STYLE",
            "IMAGE",
            "SHOT_IMAGE",
            "SCRIPT",
            "STORYBOARD",
        ]
        | None
    ) = None

    @model_validator(mode="after")
    def exact_asset_identity(self):
        if (self.role == "REFERENCE") != (
            self.asset_id is not None and self.asset_type is not None
        ):
            raise ValueError("Asset references require their exact asset identity and type")
        if self.role != "REFERENCE" and (self.asset_id is not None or self.asset_type is not None):
            raise ValueError("Non-asset evidence cannot claim an asset identity")
        return self


class ReviewRegion(ProductionContract):
    """Normalized rectangle in the cited image, independent of display resolution."""

    x: Unit
    y: Unit
    width: Annotated[float, Field(strict=True, gt=0, le=1)]
    height: Annotated[float, Field(strict=True, gt=0, le=1)]

    @model_validator(mode="after")
    def inside_image(self):
        if self.x + self.width > 1 or self.y + self.height > 1:
            raise ValueError("Evidence rectangle must remain inside its image")
        return self


class ReviewEvidence(ProductionContract):
    evidence_id: UUID
    reference_id: UUID
    region: ReviewRegion | None = None
    basis: Brief


class ReviewIssue(ProductionContract):
    issue_id: UUID
    category: Literal[
        "CHARACTER_APPEARANCE",
        "COSTUME",
        "SCENE",
        "PROP",
        "STORYBOARD_INTENT",
        "ADJACENT_CONTINUITY",
    ]
    risk: Literal["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    summary: Brief
    evidence_ids: list[UUID] = Field(min_length=1, max_length=16)
    suggested_action: Literal["REVISE_PROMPT", "REGENERATE", "REQUEST_EVIDENCE", "HUMAN_REVIEW"]


class ReviewUncertainty(ProductionContract):
    reason: Literal[
        "OCCLUDED",
        "MISSING_REFERENCE",
        "LOW_VISIBILITY",
        "CONTRADICTION",
        "BUDGET_EXHAUSTED",
        "OTHER",
    ]
    detail: Brief


class ReviewConfidence(ProductionContract):
    kind: Literal["ORDINAL_UNCALIBRATED"] = "ORDINAL_UNCALIBRATED"
    level: Literal["HIGH", "MEDIUM", "LOW", "UNKNOWN"]
    uncertainty: list[ReviewUncertainty] = Field(default_factory=list, max_length=16)


class ReviewRerun(ProductionContract):
    previous_review_id: UUID
    previous_sha256: Checksum
    reason: Brief


class ContinuityReview(ProductionContract):
    schema_version: Literal["continuity-review/1"] = REVIEW_VERSION
    result_id: UUID
    project_id: UUID
    episode_id: UUID
    target: ReviewTarget
    references: list[ReviewReference] = Field(min_length=3, max_length=64)
    versions: list[ProductionComponent] = Field(min_length=4, max_length=4)
    conclusion: Literal["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"]
    summary: Brief
    risk: Risk
    confidence: ReviewConfidence
    evidence: list[ReviewEvidence] = Field(min_length=1, max_length=128)
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=32)
    suggested_action: Action
    rerun: ReviewRerun | None = None

    @model_validator(mode="after")
    def consistent_review(self):
        refs = {item.reference_id: item for item in self.references}
        evidence = {item.evidence_id: item for item in self.evidence}
        for values, count in (
            (refs, len(self.references)),
            (evidence, len(self.evidence)),
            ({item.issue_id for item in self.issues}, len(self.issues)),
            ({item.id for item in self.versions}, len(self.versions)),
            ({(item.role, item.resource_id) for item in self.references}, len(self.references)),
        ):
            if len(values) != count:
                raise ValueError("Review identities and exact references must be unique")
        required = {
            "CANDIDATE": self.target.candidate_id,
            "TARGET_SHOT": self.target.shot_id,
            "STORYBOARD": self.target.storyboard_id,
        }
        for role, identity in required.items():
            matches = [item.resource_id for item in self.references if item.role == role]
            if matches != [identity]:
                raise ValueError(
                    "Review requires one exact candidate, shot and storyboard reference"
                )
        if {item.kind for item in self.versions} != {"AGENT", "PROMPT", "SCHEMA", "MODEL"} or any(
            item.family != "REVIEW" for item in self.versions
        ):
            raise ValueError("Review requires all four exact REVIEW production definitions")
        for item in self.evidence:
            ref = refs.get(item.reference_id)
            if ref is None:
                raise ValueError("Evidence must cite a declared exact reference")
            if item.region is not None and not (
                ref.role == "CANDIDATE"
                or ref.role == "REFERENCE"
                and ref.asset_type in {"IMAGE", "SHOT_IMAGE", "COSTUME"}
            ):
                raise ValueError("Regions can only locate evidence in a declared image")
        if not any(refs[item.reference_id].role == "CANDIDATE" for item in self.evidence):
            raise ValueError("A review must provide candidate evidence")
        for issue in self.issues:
            if (
                len(set(issue.evidence_ids)) != len(issue.evidence_ids)
                or not set(issue.evidence_ids) <= evidence.keys()
            ):
                raise ValueError("Issues require unique evidence from this review")
            if not any(
                refs[evidence[key].reference_id].role == "CANDIDATE" for key in issue.evidence_ids
            ):
                raise ValueError("An issue must identify evidence in the candidate")
        levels = ["NONE", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
        if self.risk != max((item.risk for item in self.issues), key=levels.index, default="NONE"):
            raise ValueError("Overall risk must retain the highest reported issue risk")
        uncertain = bool(self.confidence.uncertainty)
        if self.confidence.level in {"LOW", "UNKNOWN"} and not uncertain:
            raise ValueError("Low or unknown confidence requires an explicit uncertainty reason")
        human = (
            uncertain
            or self.risk in {"HIGH", "CRITICAL"}
            or any(
                item.suggested_action in {"HUMAN_REVIEW", "REQUEST_EVIDENCE"}
                for item in self.issues
            )
        )
        if human and self.conclusion != "HUMAN_REVIEW":
            raise ValueError("Uncertainty and serious risks require a human-review conclusion")
        if self.conclusion == "PASS" and (self.issues or self.suggested_action != "NONE"):
            raise ValueError("PASS cannot suppress issues or request a revision")
        if self.conclusion == "REVISION_REQUIRED" and (
            not self.issues or self.suggested_action not in {"REVISE_PROMPT", "REGENERATE"}
        ):
            raise ValueError(
                "Revision requires an evidenced issue and a bounded revision suggestion"
            )
        if self.conclusion == "HUMAN_REVIEW" and self.suggested_action != "HUMAN_REVIEW":
            raise ValueError("Human-review conclusions cannot authorize automatic execution")
        if self.rerun is not None and self.rerun.previous_review_id == self.result_id:
            raise ValueError("A review cannot rerun itself")
        value = self.model_dump(mode="json")
        safe_metadata(value)
        if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
            raise ValueError("Continuity review exceeds its 64 KiB limit")
        return self


def review_schema_registration(*, costume: bool = False) -> VersionRegistration:
    schema = ContinuityReview.model_json_schema()
    if not costume:
        # Reconstruct the exact original definition; saved /1 digests remain valid.
        asset_types = schema["$defs"]["ReviewReference"]["properties"]["asset_type"]["anyOf"][0][
            "enum"
        ]
        asset_types.remove("COSTUME")
    return VersionRegistration(
        family="REVIEW",
        kind="SCHEMA",
        name="continuity.review",
        version=REVIEW_SCHEMA_VERSION if costume else REVIEW_VERSION,
        definition={"schema": schema, "authority": "advisory-only/1"},
    )


def review_input_sha256(review: ContinuityReview) -> str:
    return digest(
        {key: review.model_dump(mode="json")[key] for key in ("target", "references", "versions")}
    )


class ReviewContractRecord(ProductionContract):
    schema_version: Literal["continuity-review-record/1"] = "continuity-review-record/1"
    state: Literal["CONTRACT_VALIDATED"] = "CONTRACT_VALIDATED"
    review: ContinuityReview
    input_sha256: Checksum
    review_sha256: Checksum
    sequence: Annotated[int, Field(strict=True, ge=1)]
    recorded_by: Annotated[str, Field(min_length=1, max_length=100)]
    recorded_at: AwareDatetime
    result: WorkflowResult

    @model_validator(mode="after")
    def exact_receipt(self):
        if self.review_sha256 != digest(self.review.model_dump(mode="json")) or (
            self.input_sha256 != review_input_sha256(self.review)
        ):
            raise ValueError("Review receipt must preserve exact result and input digests")
        if (self.sequence == 1) != (self.review.rerun is None):
            raise ValueError("Later reviews require an explicit rerun audit reference")
        expected = WorkflowResult(
            result_id=self.review.result_id,
            project_id=self.review.project_id,
            episode_id=self.review.episode_id,
            agent_name="continuity_reviewer",
            status=RunStatus.SUCCEEDED,
            summary=self.review.summary,
            artifact_ids=[self.review.result_id],
            warnings=[],
            requires_human_review=True,
        )
        if self.result != expected:
            raise ValueError("Review receipt must retain the public result and human authority")
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(UTC))
        safe_metadata(self.model_dump(mode="json"))
        return self
