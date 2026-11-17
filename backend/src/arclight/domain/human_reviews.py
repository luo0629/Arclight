"""Exact, bounded human decisions shared by production review entry points."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.director_revision import DirectorIssue
from arclight.domain.production_state import ProductionContract
from arclight.domain.workflow_contracts import Checksum

ReviewKind = Literal[
    "SCRIPT", "STORYBOARD", "REFERENCE", "VISUAL_SPEC", "EXCEPTIONAL_SHOT", "EXPORT", "DELIVERY"
]


class ReviewSnapshot(ProductionContract):
    schema_version: Literal["human-review/1"] = "human-review/1"
    project_id: UUID
    episode_id: UUID | None
    run_id: UUID | None
    loop_id: UUID | None = None
    thread_id: str = Field(min_length=1, max_length=160, pattern=r"^[\w.:-]+$")
    kind: ReviewKind
    purpose: Literal["PRODUCTION", "PREVIEW"] = "PRODUCTION"
    object_id: UUID
    version_id: UUID
    content_sha256: Checksum
    input_sha256: Checksum
    source_version_ids: list[UUID] = Field(default_factory=list, max_length=4096)
    expected_revision: int = Field(default=0, strict=True, ge=0)

    @model_validator(mode="after")
    def exact_scope(self):
        if self.loop_id is not None and self.run_id is None:
            raise ValueError("Agent loop reviews require the original run")
        thread = f"agent-loop:{self.loop_id}" if self.loop_id else str(self.run_id)
        if self.run_id is not None and (self.episode_id is None or self.thread_id != thread):
            raise ValueError("Workflow reviews must retain the original run/thread")
        if len(set(self.source_version_ids)) != len(self.source_version_ids):
            raise ValueError("Duplicate review input")
        if self.kind != "EXPORT" and self.purpose != "PRODUCTION":
            raise ValueError("Preview scope is reserved for export reviews")
        return self


class PerformanceIntentApproval(ProductionContract):
    scope: Literal["INTENT_ONLY"] = "INTENT_ONLY"
    production_ready: Literal[False] = False
    source_script_version_id: UUID
    source_script_sha256: Checksum
    blockers: list[DirectorIssue] = Field(default_factory=list, max_length=4096)


class HumanDecision(ProductionContract):
    version_id: UUID
    request_sha256: Checksum
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field(default="", max_length=2000)
    impact_digest: Checksum | None = None
    performance_intent: PerformanceIntentApproval | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

    @model_validator(mode="after")
    def explicit_intent(self):
        if self.performance_intent is not None and (
            self.decision != "APPROVE" or not self.comment.strip()
        ):
            raise ValueError("仅确认表演意图须明确批准并填写理由")
        return self


class ReviewResume(ProductionContract):
    decision_id: UUID
    request_sha256: Checksum


class ReviewHandoff(ProductionContract):
    version_id: UUID
    run_id: UUID | None = None
    thread_id: str = Field(min_length=1, max_length=160, pattern=r"^[\w.:-]+$")
    revision: int | None = Field(default=None, strict=True, ge=0)
    status: Literal["QUEUED", "APPROVED", "REJECTED"]
