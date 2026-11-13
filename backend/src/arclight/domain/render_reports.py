"""Machine evidence and human acceptance of one exact formal render."""

from typing import Literal
from uuid import UUID

from pydantic import Field, StrictBool, field_validator, model_validator

from arclight.domain.render_inspections import RenderInspectionResult
from arclight.domain.render_manifests import (
    LineAudioDecision,
    RenderContract,
    RenderFileReference,
    RenderOutputProfile,
    RenderReportEnvelope,
    RenderSoftware,
    RenderSource,
    Sha256,
)
from arclight.domain.versions import digest


class RenderWarning(RenderContract):
    id: Sha256
    code: Literal["EXPLICIT_SILENCE", "SUBTITLE_ONLY", "MEDIA_EXCEPTION"]
    source_version_id: UUID
    line_id: str | None = Field(default=None, min_length=1, max_length=100)
    reason: str = Field(min_length=1, max_length=2000)
    human_review_request_id: UUID | None = None
    human_review_decision_id: UUID | None = None
    human_review_request_sha256: Sha256 | None = None

    @field_validator("reason")
    @classmethod
    def meaningful_reason(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("A delivery exception requires its actual reason")
        return value

    @model_validator(mode="after")
    def exact_identity(self):
        if (self.code == "MEDIA_EXCEPTION") != (self.line_id is None):
            raise ValueError("Audio exceptions must name the exact script line")
        review_provenance = (
            self.human_review_request_id,
            self.human_review_decision_id,
            self.human_review_request_sha256,
        )
        if (self.code == "MEDIA_EXCEPTION" and any(item is None for item in review_provenance)) or (
            self.code != "MEDIA_EXCEPTION" and any(item is not None for item in review_provenance)
        ):
            raise ValueError("Media exceptions must retain their exact human review provenance")
        if self.id != digest(self.model_dump(mode="json", exclude={"id"})):
            raise ValueError("Warning identity must include all exact exception facts")
        return self


class RenderReportChecks(RenderContract):
    video_decodable: StrictBool
    audio_stream_present: StrictBool
    subtitle_range_valid: StrictBool
    missing_required_shots: int = Field(strict=True, ge=0)
    missing_audio_lines: int = Field(strict=True, ge=0)
    stale_dependencies: int = Field(strict=True, ge=0)
    technical: RenderInspectionResult

    @model_validator(mode="after")
    def successful_checks(self):
        if not (
            self.video_decodable
            and self.audio_stream_present
            and self.subtitle_range_valid
            and self.missing_required_shots == 0
            and self.missing_audio_lines == 0
            and self.stale_dependencies == 0
            and self.technical.passed
        ):
            raise ValueError("A registered formal report cannot waive blocking checks")
        return self


class RenderMachineReport(RenderContract):
    schema_version: Literal["render-report/1"] = "render-report/1"
    checker: Literal["arclight-delivery-v1"] = "arclight-delivery-v1"
    render_status: Literal["passed"] = "passed"
    job_id: UUID
    attempt_id: UUID
    artifact_id: UUID
    asset_version_id: UUID
    manifest_id: UUID
    manifest_sha256: Sha256
    timeline_id: UUID
    timeline_sha256: Sha256
    timeline_version: int = Field(strict=True, gt=0)
    inspection_id: UUID
    input_sha256: Sha256
    output_file_id: UUID
    output_sha256: Sha256
    byte_size: int = Field(strict=True, gt=0)
    duration_ms: int = Field(strict=True, gt=0)
    profile: Literal["FORMAL"] = "FORMAL"
    sources: tuple[RenderSource, ...] = Field(max_length=2000)
    font: RenderFileReference | None
    software: RenderSoftware
    output: RenderOutputProfile
    line_audio_decisions: tuple[LineAudioDecision, ...] = Field(max_length=5000)
    checks: RenderReportChecks
    warnings: tuple[RenderWarning, ...] = Field(default_factory=tuple, max_length=7000)

    @model_validator(mode="after")
    def exact_measurement(self):
        technical = self.checks.technical
        if (
            self.output.name != "FORMAL"
            or technical.output_sha256 != self.output_sha256
            or technical.byte_size != self.byte_size
            or len({warning.id for warning in self.warnings}) != len(self.warnings)
            or len({source.asset_version_id for source in self.sources}) != len(self.sources)
        ):
            raise ValueError("Machine report must retain exact output and unique evidence")
        return self


class RenderDeliveryAcceptance(RenderContract):
    phase: Literal["LEGACY", "PREVIEW", "DELIVERY_QA", "COMPLETED", "COMPLETED_WITH_WARNINGS"]
    is_delivery: StrictBool = False
    warnings_confirmed: StrictBool = False
    review_id: UUID | None = None
    reviewed_by_user_id: str | None = None
    blocking_issues: tuple[str, ...] = Field(default_factory=tuple, max_length=100)


class RenderDeliveryReport(RenderContract):
    report_id: UUID
    report_file_id: UUID
    report_sha256: Sha256
    report: RenderReportEnvelope
    acceptance: RenderDeliveryAcceptance
