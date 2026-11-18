"""Exact temporal evidence consumed by VIDEO-01; trusted execution belongs to VIDEO-02."""

import json
from datetime import UTC
from typing import Annotated, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.generation_requests import safe_metadata
from arclight.domain.production_state import ProductionContract
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import Checksum, ProductionComponent

VIDEO_REVIEW_RESOURCE = "VIDEO_TEMPORAL_REVIEW"
VIDEO_ADMISSION_RESOURCE = "VIDEO_TEMPORAL_ADMISSION"
VIDEO_HUMAN_HANDLER = "video_temporal_review"
Brief = Annotated[str, Field(min_length=1, max_length=1000, pattern=r"\S")]
Milliseconds = Annotated[int, Field(strict=True, ge=0, le=30000)]


class VideoReviewTarget(ReviewTarget):
    checksum: Checksum
    manifest_sha256: Checksum


class VideoReviewSample(ProductionContract):
    time_ms: Milliseconds
    frame_sha256: Checksum
    observation: Brief


class VideoReviewIssue(ProductionContract):
    issue_id: UUID
    time_ms: Milliseconds
    description: Brief
    resolved: bool = Field(strict=True)


class VideoReviewReport(ProductionContract):
    schema_version: Literal["video-temporal-review/1"] = "video-temporal-review/1"
    report_id: UUID
    project_id: UUID
    episode_id: UUID
    target: VideoReviewTarget
    versions: list[ProductionComponent] = Field(min_length=4, max_length=4)
    duration_ms: Annotated[int, Field(strict=True, gt=0, le=30000)]
    sampling_strategy: Literal["BOUNDED_UNIFORM", "SCENE_AWARE", "FULL_DECODE"]
    coverage_start_ms: Milliseconds
    coverage_end_ms: Milliseconds
    samples: list[VideoReviewSample] = Field(min_length=2, max_length=128)
    limitations: list[Brief] = Field(min_length=1, max_length=16)
    conclusion: Literal["PASS", "REVISION_REQUIRED", "HUMAN_REVIEW"]
    summary: Brief
    issues: list[VideoReviewIssue] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def temporal_evidence(self):
        times = [sample.time_ms for sample in self.samples]
        if times != sorted(set(times)) or times[0] != 0 or times[-1] >= self.duration_ms:
            raise ValueError("Video evidence requires ordered, distinct in-range timecodes")
        if self.coverage_start_ms != 0 or self.coverage_end_ms != self.duration_ms:
            raise ValueError("Temporal coverage must explicitly name the complete candidate")
        if {part.kind for part in self.versions} != {"AGENT", "PROMPT", "SCHEMA", "MODEL"} or any(
            part.family != "REVIEW" for part in self.versions
        ):
            raise ValueError("Video review requires all four exact REVIEW definitions")
        if len({part.id for part in self.versions}) != len(self.versions):
            raise ValueError("Review definitions must be unique")
        if len({issue.issue_id for issue in self.issues}) != len(self.issues) or any(
            issue.time_ms >= self.duration_ms for issue in self.issues
        ):
            raise ValueError("Issues require unique identities and candidate timecodes")
        if self.conclusion == "PASS" and self.issues:
            raise ValueError("PASS cannot suppress reported issues")
        if self.conclusion == "REVISION_REQUIRED" and not self.issues:
            raise ValueError("Revision requires an exact temporal issue")
        value = self.model_dump(mode="json")
        safe_metadata(value)
        if len(json.dumps(value, ensure_ascii=False).encode()) > 65536:
            raise ValueError("Video review exceeds its 64 KiB limit")
        return self


def video_review_input_sha256(report: VideoReviewReport) -> str:
    return digest(
        {
            "project_id": str(report.project_id),
            "episode_id": str(report.episode_id),
            "target": report.target.model_dump(mode="json"),
            "versions": [part.model_dump(mode="json") for part in report.versions],
        }
    )


class VideoReviewRecord(ProductionContract):
    schema_version: Literal["video-temporal-review-record/1"] = "video-temporal-review-record/1"
    report: VideoReviewReport
    input_sha256: Checksum
    report_sha256: Checksum
    sequence: Annotated[int, Field(strict=True, ge=1)]
    previous_report_id: UUID | None = None
    previous_record_sha256: Checksum | None = None
    recorded_by: Annotated[str, Field(min_length=1, max_length=100)]
    recorded_at: AwareDatetime

    @model_validator(mode="after")
    def exact_receipt(self):
        if self.report_sha256 != digest(self.report.model_dump(mode="json")) or (
            self.input_sha256 != video_review_input_sha256(self.report)
        ):
            raise ValueError("Temporal receipt must preserve exact report and inputs")
        previous = self.previous_report_id is not None and self.previous_record_sha256 is not None
        if (
            (self.sequence > 1) != previous
            or (
                self.sequence == 1
                and (self.previous_report_id is not None or self.previous_record_sha256 is not None)
            )
            or self.previous_report_id == self.report.report_id
        ):
            raise ValueError("Later temporal reports require the exact preceding receipt")
        object.__setattr__(self, "recorded_at", self.recorded_at.astimezone(UTC))
        return self


def video_review_schema_registration():
    from arclight.domain.versions import VersionRegistration

    return VersionRegistration(
        family="REVIEW",
        kind="SCHEMA",
        name="video.temporal.review",
        version="video-temporal-review/1",
        definition={
            "schema": VideoReviewReport.model_json_schema(),
            "authority": "advisory-only/1",
        },
    )


class VideoReviewAdmission(ProductionContract):
    """Only a trusted VIDEO-02 domain router may issue this immutable decision."""

    schema_version: Literal["video-temporal-admission/1"] = "video-temporal-admission/1"
    report_id: UUID
    record_sha256: Checksum
    requires_human_review: bool = Field(strict=True)
    human_review_request_id: UUID | None = None
