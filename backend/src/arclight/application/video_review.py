"""Fail-closed consumption of trusted temporal reports, without an HTTP PASS writer."""

from math import ceil
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.continuity_workflow import require_shot_binding
from arclight.application.human_reviews import require_review_actor
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.application.video_candidates import require_video_candidate_media_check
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import ShotReviewBinding
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.domain.video_review import (
    VIDEO_ADMISSION_RESOURCE,
    VIDEO_HUMAN_HANDLER,
    VIDEO_REVIEW_RESOURCE,
    VideoReviewAdmission,
    VideoReviewRecord,
    VideoReviewReport,
)
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationCandidate,
    GenerationJob,
    ProductionVersion,
    VersionBinding,
    VisualAsset,
)


def _definitions(db: Session, binding: VersionBinding, report: VideoReviewReport) -> None:
    snapshot = checked_snapshot(db, binding)
    if snapshot["components"] != [part.model_dump(mode="json") for part in report.versions]:
        raise ValueError("Temporal report and envelope require the same exact definitions")
    for part in report.versions:
        row = db.get(ProductionVersion, part.id)
        if row is None or any(
            getattr(row, field) != getattr(part, field)
            for field in ("family", "kind", "name", "version", "digest")
        ):
            raise ValueError("Temporal production definitions must retain their exact identity")
        if part.kind == "SCHEMA" and row.definition_json.get("schema") != (
            VideoReviewReport.model_json_schema()
        ):
            raise ValueError("An image schema cannot authorize temporal evidence")


def _latest_record(db: Session, project_id: UUID, candidate_id: UUID) -> VideoReviewRecord:
    rows = list(
        db.scalars(
            select(VersionBinding).where(
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == VIDEO_REVIEW_RESOURCE,
                VersionBinding.snapshot_json["video_review"]["report"]["target"][
                    "candidate_id"
                ].as_string()
                == str(candidate_id),
            )
        )
    )
    if not rows:
        raise DomainError("VIDEO_REVIEW_REQUIRED", "镜头视频须先取得可信的完整时序审查", 409)
    try:
        records = []
        for row in rows:
            record = VideoReviewRecord.model_validate(checked_snapshot(db, row)["video_review"])
            if row.resource_id != record.report.report_id or record.report.project_id != project_id:
                raise ValueError("Temporal receipt identity is not exact")
            _definitions(db, row, record.report)
            records.append(record)
        records.sort(key=lambda item: item.sequence)
        if [item.sequence for item in records] != list(range(1, len(records) + 1)):
            raise ValueError("Temporal report sequence must be complete and unambiguous")
        for previous, record in zip(records, records[1:], strict=False):
            if (
                record.report.target != previous.report.target
                or record.previous_report_id != previous.report.report_id
                or record.previous_record_sha256 != digest(previous.model_dump(mode="json"))
            ):
                raise ValueError("A temporal rerun must retain its exact target and prior receipt")
        return records[-1]
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise DomainError("VIDEO_REVIEW_INVALID", "视频时序审查历史或版本绑定无效", 409) from error


def _human_acceptance(
    db: Session,
    record: VideoReviewRecord,
    admission: VideoReviewAdmission,
    version: AssetVersion,
    manifest: DependencyManifest,
) -> None:
    request = (
        db.get(HumanReviewRequest, admission.human_review_request_id)
        if (admission.human_review_request_id is not None)
        else None
    )
    decision = (
        db.scalar(select(HumanReviewDecision).where(HumanReviewDecision.request_id == request.id))
        if request is not None
        else None
    )
    try:
        if request is None or decision is None:
            raise ValueError("Missing exact video exception request and Owner decision")
        snapshot = ReviewSnapshot.model_validate(request.snapshot_json)
        answer = HumanDecision.model_validate(decision.decision_json)
        report = record.report
        sources = {
            UUID(item["source_asset_version_id"]) for item in manifest.snapshot_json["inputs"]
        }
        if (
            request.project_id != report.project_id
            or request.episode_id != report.episode_id
            or request.handler != VIDEO_HUMAN_HANDLER
            or request.run_id != snapshot.run_id
            or request.request_sha256 != digest(snapshot.model_dump(mode="json"))
            or snapshot.project_id != report.project_id
            or snapshot.episode_id != report.episode_id
            or snapshot.kind != "EXCEPTIONAL_SHOT"
            or snapshot.purpose != "PRODUCTION"
            or snapshot.object_id != report.report_id
            or snapshot.version_id != version.id
            or snapshot.content_sha256 != version.checksum
            or snapshot.input_sha256 != admission.record_sha256
            or set(snapshot.source_version_ids) != sources
            or answer.version_id != version.id
            or answer.request_sha256 != request.request_sha256
            or answer.decision != "APPROVE"
            or not answer.comment.strip()
        ):
            raise ValueError("Owner acceptance must bind this exact temporal report and version")
        require_review_actor(db, report.project_id, decision.actor_id, owner=True)
    except (ValidationError, KeyError, TypeError, ValueError) as error:
        raise DomainError(
            "HUMAN_DECISION_REQUIRED", "视频须有绑定本次时序审查的有效Owner决定", 409
        ) from error


def require_video_review(db: Session, version: AssetVersion) -> VideoReviewRecord:
    """Read durable evidence; callers retain permissions, CAS and lifecycle authority."""
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True)
    candidate = db.scalar(
        select(GenerationCandidate).where(GenerationCandidate.asset_version_id == version.id)
    )
    if (
        asset is None
        or asset.asset_type != "VIDEO"
        or candidate is None
        or (candidate.project_id != asset.project_id)
    ):
        raise DomainError("VIDEO_REVIEW_REQUIRED", "视频须有独立候选、媒体检查和时序审查", 409)
    check = require_video_candidate_media_check(db, asset.project_id, candidate.id)
    assert check.video is not None
    record = _latest_record(db, asset.project_id, candidate.id)
    report = record.report
    manifest = db.get(DependencyManifest, candidate.input_manifest_id)
    job = db.get(GenerationJob, candidate.job_id)
    source = version.metadata_json.get("video_source", {})
    if (
        manifest is None
        or job is None
        or job.project_id != asset.project_id
        or report.project_id != asset.project_id
        or report.episode_id != job.episode_id
        or report.target.candidate_id != candidate.id
        or report.target.asset_id != asset.id
        or report.target.asset_version_id != version.id
        or report.target.checksum != version.checksum
        or report.target.input_manifest_id != manifest.id
        or report.target.manifest_sha256 != digest(manifest.snapshot_json)
        or report.target.media_check_sha256 != digest(check.model_dump(mode="json"))
        or str(report.target.storyboard_id) != source.get("storyboard_id")
        or str(report.target.shot_id) != source.get("shot_id")
        or report.duration_ms != check.video.duration_ms
        or report.samples[-1].time_ms
        < max(1, check.video.duration_ms - ceil(1000 / check.video.frame_rate))
        or not check.video.full_decode_verified
        or check.video.decoded_frames < 2
        or version.stale
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
    ):
        raise DomainError(
            "VIDEO_REVIEW_TARGET_INVALID", "时序审查与当前精确视频或覆盖范围不匹配", 409
        )
    require_shot_binding(
        db,
        asset.project_id,
        ShotReviewBinding(
            storyboard_id=report.target.storyboard_id,
            shot_id=report.target.shot_id,
        ),
        report.episode_id,
    )
    require_review_actor(db, asset.project_id, record.recorded_by, owner=False)
    binding = find_binding(db, asset.project_id, VIDEO_ADMISSION_RESOURCE, report.report_id)
    if binding is None:
        raise DomainError("VIDEO_REVIEW_REQUIRED", "时序报告尚未经可信领域路由确认", 409)
    try:
        _definitions(db, binding, report)
        admission = VideoReviewAdmission.model_validate(
            checked_snapshot(db, binding)["video_admission"]
        )
        if admission.report_id != report.report_id or admission.record_sha256 != digest(
            record.model_dump(mode="json")
        ):
            raise ValueError("Video admission must bind the latest exact temporal receipt")
    except (ValidationError, KeyError, TypeError, ValueError, AttributeError) as error:
        raise DomainError(
            "VIDEO_ADMISSION_INVALID", "视频准入未绑定本次精确时序报告", 409
        ) from error
    if report.conclusion == "REVISION_REQUIRED" or any(
        not issue.resolved for issue in report.issues
    ):
        raise DomainError("VIDEO_REVISION_REQUIRED", "视频时序问题须修复并对新候选重新审查", 409)
    if (
        report.conclusion == "HUMAN_REVIEW"
        or admission.requires_human_review
        or (admission.human_review_request_id is not None)
    ):
        _human_acceptance(db, record, admission, version, manifest)
    return record


def read_admission(db: Session, project_id: UUID, version: AssetVersion) -> dict:
    """Safe read-only UI summary; missing evidence never grants a lifecycle action."""
    asset = db.get(VisualAsset, version.asset_id)
    if asset is None or asset.project_id != project_id:
        raise DomainError("NOT_FOUND", "视频资产不存在", 404)
    record = None
    blocked = None
    try:
        record = require_video_review(db, version)
    except DomainError as error:
        blocked = error.code
        if blocked in {
            "VIDEO_REVIEW_REQUIRED",
            "VIDEO_REVISION_REQUIRED",
            "HUMAN_DECISION_REQUIRED",
        }:
            candidate = db.scalar(
                select(GenerationCandidate).where(
                    GenerationCandidate.asset_version_id == version.id
                )
            )
            if candidate is not None and candidate.project_id == project_id:
                try:
                    latest = _latest_record(db, project_id, candidate.id)
                    if latest.report.target.asset_version_id == version.id and (
                        latest.report.target.asset_id == asset.id
                        and latest.report.target.checksum == version.checksum
                    ):
                        record = latest
                except DomainError:
                    pass
    report = record.report if record else None
    return {
        "schema_version": "video-review-summary/1",
        "status": "BLOCKED" if blocked else "ADMITTED",
        "blocked_code": blocked,
        "report_id": str(report.report_id) if report else None,
        "conclusion": report.conclusion if report else None,
        "report_sha256": record.report_sha256 if record else None,
        "summary": report.summary if report else None,
        "sampled_frame_count": len(report.samples) if report else 0,
        "coverage_start_ms": report.coverage_start_ms if report else None,
        "coverage_end_ms": report.coverage_end_ms if report else None,
        "limitations": list(report.limitations) if report else [],
        "sampling_strategy": report.sampling_strategy if report else None,
        "samples": [sample.model_dump(mode="json") for sample in report.samples] if report else [],
    }
