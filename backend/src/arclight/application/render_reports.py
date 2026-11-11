"""Register proven formal reports and reuse the shared human decision journal."""

import hashlib
import json
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.continuity_selection import require_shot_review
from arclight.application.human_reviews import ReviewHandler, request_review, require_review_actor
from arclight.application.production_versions import checked_snapshot as binding_snapshot
from arclight.application.production_versions import find_binding
from arclight.application.render_admission import RenderFacts, admission_facts
from arclight.application.render_inputs import file_storage
from arclight.application.render_manifests import checked_snapshot
from arclight.application.render_reviews import require_export_approval
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.continuity_selection import ADMISSION_RESOURCE, ReviewAdmission
from arclight.domain.human_reviews import HumanDecision, ReviewHandoff, ReviewSnapshot
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.domain.render_manifests import RenderManifestSnapshot, RenderReportEnvelope
from arclight.domain.render_reports import (
    RenderDeliveryAcceptance,
    RenderMachineReport,
    RenderReportChecks,
    RenderWarning,
)
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    RenderManifest,
    RenderReportLink,
    StoredFile,
    Timeline,
)
from arclight.storage.objects import ObjectStorage

DELIVERY_HANDLER = "RENDER_DELIVERY"
MAX_REPORT_BYTES = 256 * 1024


def machine_report_key(project_id: UUID, attempt_id: UUID) -> str:
    return f"{project_id}/render-reports/{attempt_id}.json"


def canonical_report_bytes(envelope: RenderReportEnvelope) -> bytes:
    return json.dumps(
        envelope.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode()


def report_warning(
    code: str,
    source_version_id: UUID,
    line_id: str | None,
    reason: str,
    *,
    human_review_request_id: UUID | None = None,
    human_review_decision_id: UUID | None = None,
    human_review_request_sha256: str | None = None,
) -> RenderWarning:
    value = {
        "code": code,
        "source_version_id": str(source_version_id),
        "line_id": line_id,
        "reason": reason,
        "human_review_request_id": (
            str(human_review_request_id) if human_review_request_id is not None else None
        ),
        "human_review_decision_id": (
            str(human_review_decision_id) if human_review_decision_id is not None else None
        ),
        "human_review_request_sha256": human_review_request_sha256,
    }
    return RenderWarning.model_validate({"id": digest(value), **value})


def report_warnings(db: Session, snapshot: RenderManifestSnapshot) -> tuple[RenderWarning, ...]:
    warnings = [
        report_warning(
            "EXPLICIT_SILENCE" if decision.mode == "SILENT" else "SUBTITLE_ONLY",
            decision.script_version_id,
            decision.line_id,
            decision.reason,
        )
        for decision in snapshot.line_audio_decisions
    ]
    for source in snapshot.sources:
        if source.asset_type not in {"IMAGE", "SHOT_IMAGE"}:
            continue
        version = db.get(AssetVersion, source.asset_version_id)
        assert version is not None
        record = require_shot_review(db, version)
        binding = find_binding(
            db, record.review.project_id, ADMISSION_RESOURCE, record.review.result_id
        )
        if binding is None:
            raise DomainError("RENDER_REPORT_INVALID", "镜头缺少精确审查准入", 409)
        admission = ReviewAdmission.model_validate(
            binding_snapshot(db, binding)["review_admission"]
        )
        if admission.human_review_request_id is not None:
            decision_row = db.scalar(
                select(HumanReviewDecision).where(
                    HumanReviewDecision.request_id == admission.human_review_request_id
                )
            )
            assert (
                decision_row is not None
            )  # require_shot_review verified the exact Owner decision.
            answer = HumanDecision.model_validate(decision_row.decision_json)
            warnings.append(
                report_warning(
                    "MEDIA_EXCEPTION",
                    version.id,
                    None,
                    answer.comment,
                    human_review_request_id=decision_row.request_id,
                    human_review_decision_id=decision_row.id,
                    human_review_request_sha256=answer.request_sha256,
                )
            )
    if len(warnings) > 7000:
        raise DomainError("RENDER_REPORT_LIMIT", "成片例外数量超过验收报告上限", 413)
    return tuple(sorted(warnings, key=lambda warning: warning.id))


def report_envelope(report: RenderMachineReport) -> RenderReportEnvelope:
    return RenderReportEnvelope(
        manifest_id=report.manifest_id,
        manifest_sha256=report.manifest_sha256,
        timeline_id=report.timeline_id,
        output_file_id=report.output_file_id,
        output_sha256=report.output_sha256,
        details=report.model_dump(mode="json", round_trip=True),
    )


def register_machine_report(
    db: Session,
    settings: Settings,
    run: RenderExecution,
    job: RenderJob,
    attempt: RenderAttempt,
    artifact: RenderArtifact,
    facts: RenderFacts,
    inspection: RenderInspectionResult,
) -> RenderReportLink | None:
    """Publish the report in the caller's final owned, freshly admitted transaction."""
    snapshot = facts.snapshot
    if snapshot.output.name != "FORMAL":
        return None
    previous = db.scalar(
        select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id)
    )
    if previous is not None:
        if read_machine_report(db, artifact) is None:
            raise DomainError("RENDER_RESULT_CONFLICT", "已有报告不能替换或升级为机器验收", 409)
        return previous
    durable = db.scalar(select(RenderInspection).where(RenderInspection.attempt_id == attempt.id))
    manifest = db.get(RenderManifest, job.manifest_id)
    timeline = db.get(Timeline, snapshot.timeline_id)
    if (
        durable is None
        or manifest is None
        or timeline is None
        or not durable.passed
        or not inspection.passed
        or durable.result_json != inspection.model_dump(mode="json", round_trip=True)
        or (run.id, job.id, attempt.run_id, artifact.job_id) != (job.id,) * 4
        or artifact.asset_version_id is None
        or (artifact.manifest_id, durable.manifest_id) != (manifest.id,) * 2
        or (durable.manifest_sha256, job.manifest_sha256) != (manifest.content_sha256,) * 2
        or (durable.input_sha256, attempt.input_sha256) != (facts.input_sha256,) * 2
        or (artifact.checksum, durable.output_sha256) != (inspection.output_sha256,) * 2
        or (artifact.byte_size, durable.byte_size) != (inspection.byte_size,) * 2
        or checked_snapshot(manifest) != snapshot
    ):
        raise DomainError("RENDER_REPORT_INVALID", "机器报告缺少同一任务和精确产物的真实检查", 409)
    report = RenderMachineReport(
        job_id=job.id,
        attempt_id=attempt.id,
        artifact_id=artifact.id,
        asset_version_id=artifact.asset_version_id,
        manifest_id=manifest.id,
        manifest_sha256=manifest.content_sha256,
        timeline_id=snapshot.timeline_id,
        timeline_sha256=snapshot.timeline_sha256,
        timeline_version=timeline.version_no,
        inspection_id=durable.id,
        input_sha256=facts.input_sha256,
        output_file_id=artifact.file_id,
        output_sha256=artifact.checksum,
        byte_size=artifact.byte_size,
        duration_ms=artifact.duration_ms,
        sources=snapshot.sources,
        font=snapshot.font,
        software=snapshot.software,
        output=snapshot.output,
        line_audio_decisions=snapshot.line_audio_decisions,
        checks=RenderReportChecks(
            video_decodable=bool(inspection.measurement and inspection.measurement.decodable),
            audio_stream_present=bool(inspection.measurement and inspection.measurement.audio),
            subtitle_range_valid=True,
            missing_required_shots=0,
            missing_audio_lines=0,
            stale_dependencies=0,
            technical=inspection,
        ),
        warnings=report_warnings(db, snapshot),
    )
    envelope = report_envelope(report)
    data = canonical_report_bytes(envelope)
    if len(data) > MAX_REPORT_BYTES:
        raise DomainError("RENDER_REPORT_LIMIT", "机器验收报告超过大小上限", 413)
    key = machine_report_key(job.project_id, attempt.id)
    store = ObjectStorage(settings)

    # Durable candidate cleanup owns this deterministic key across rollback and crashes.
    store.put(key, data, "application/json")
    checksum = hashlib.sha256(data).hexdigest()
    file = StoredFile(
        id=uuid5(attempt.id, "render-report-file"),
        project_id=job.project_id,
        storage_backend=settings.object_storage_backend.upper(),
        bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
        object_key=key,
        checksum=checksum,
        byte_size=len(data),
        media_type="application/json",
        original_name=f"render-report-{artifact.id}.json",
        source_note=f"Render Manifest {manifest.id} sha256:{manifest.content_sha256}",
        rights_note="服务端确定性机器检查及已冻结来源的验收记录",
        rights_confirmed=True,
        created_by=run.created_by,
    )
    db.add(file)
    db.flush()
    link = RenderReportLink(
        id=uuid5(attempt.id, "render-report-link"),
        artifact_id=artifact.id,
        file_id=file.id,
        object_key=key,
        checksum=checksum,
        byte_size=len(data),
        inspection_id=durable.id,
        input_sha256=facts.input_sha256,
        machine_report_json=envelope.model_dump(mode="json"),
    )
    db.add(link)
    db.flush()
    audit(
        db,
        job.project_id,
        run.created_by,
        "render.machine_report_registered",
        link.id,
        {"artifact_id": str(artifact.id), "inspection_id": str(durable.id)},
    )
    return link


def read_machine_report(
    db: Session,
    artifact: RenderArtifact,
    *,
    current: bool = False,
    verify_object: bool = True,
) -> RenderMachineReport | None:
    """Read immutable proof, verifying the actual report object by default.

    Only the internal technical state projection may omit object verification;
    it still cannot confirm delivery without separately checking acceptance.
    """
    link = db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id))
    if link is None or link.machine_report_json is None:
        return None
    try:
        envelope = RenderReportEnvelope.model_validate(link.machine_report_json)
        report = RenderMachineReport.model_validate(envelope.details)
        manifest = db.get(RenderManifest, artifact.manifest_id)
        durable = db.get(RenderInspection, report.inspection_id)
        version = db.get(AssetVersion, artifact.asset_version_id)
        file = db.get(StoredFile, link.file_id)
        job = db.get(RenderJob, report.job_id)
        attempt = db.get(RenderAttempt, report.attempt_id)
        timeline = db.get(Timeline, report.timeline_id)
        if (
            manifest is None
            or durable is None
            or version is None
            or file is None
            or job is None
            or attempt is None
            or timeline is None
        ):
            raise ValueError("Incomplete machine report proof")
        snapshot = checked_snapshot(manifest)
        data = canonical_report_bytes(envelope)
        if (
            envelope != report_envelope(report)
            or hashlib.sha256(data).hexdigest() != link.checksum
            or len(data) != link.byte_size
            or (file.checksum, file.byte_size, file.object_key, file.media_type, file.project_id)
            != (
                link.checksum,
                link.byte_size,
                link.object_key,
                "application/json",
                manifest.project_id,
            )
            or (report.artifact_id, report.asset_version_id, report.job_id, report.output_file_id)
            != (artifact.id, artifact.asset_version_id, artifact.job_id, artifact.file_id)
            or (report.output_sha256, report.byte_size, report.duration_ms)
            != (artifact.checksum, artifact.byte_size, artifact.duration_ms)
            or (report.manifest_id, report.manifest_sha256)
            != (manifest.id, manifest.content_sha256)
            or (report.inspection_id, report.input_sha256)
            != (link.inspection_id, link.input_sha256)
            or (
                durable.attempt_id,
                durable.manifest_id,
                durable.manifest_sha256,
                durable.input_sha256,
                durable.output_sha256,
                durable.byte_size,
                durable.passed,
            )
            != (
                attempt.id,
                manifest.id,
                manifest.content_sha256,
                report.input_sha256,
                artifact.checksum,
                artifact.byte_size,
                True,
            )
            or durable.result_json
            != report.checks.technical.model_dump(mode="json", round_trip=True)
            or (attempt.run_id, attempt.input_sha256, job.manifest_id, job.manifest_sha256)
            != (job.id, report.input_sha256, manifest.id, manifest.content_sha256)
            or (report.timeline_id, report.timeline_sha256, report.timeline_version)
            != (snapshot.timeline_id, snapshot.timeline_sha256, timeline.version_no)
            or (
                report.sources,
                report.font,
                report.software,
                report.output,
                report.line_audio_decisions,
            )
            != (
                snapshot.sources,
                snapshot.font,
                snapshot.software,
                snapshot.output,
                snapshot.line_audio_decisions,
            )
        ):
            raise ValueError("Machine report differs from immutable evidence")
        if verify_object:
            settings = db.info.get("settings")
            if not isinstance(settings, Settings):
                raise DomainError("RENDER_REPORT_INVALID", "机器报告缺少实际存储校验配置", 409)
            stored_data = file_storage(settings, file).read(
                file.object_key, MAX_REPORT_BYTES, checksum=link.checksum
            )
            if stored_data != data:
                raise ValueError("Stored machine report differs from immutable evidence")
        if current:
            run = db.get(RenderExecution, artifact.job_id)
            if run is None or run.status != "SUCCEEDED":
                raise DomainError("RENDER_NOT_SUCCEEDED", "成片任务尚未成功", 409)
            if version.stale or version.status in {"STALE", "ARCHIVED", "REJECTED"}:
                raise DomainError("ASSET_NOT_USABLE", "成片或其精确输入已失效", 409)
            facts = admission_facts(db, manifest)
            if (
                facts.input_sha256 != report.input_sha256
                or report_warnings(db, snapshot) != report.warnings
            ):
                raise DomainError("RENDER_INPUT_CHANGED", "机器报告的精确输入或例外已变化", 409)
            require_export_approval(db, manifest, facts)
        return report
    except (ValidationError, ValueError, TypeError, KeyError) as exc:
        raise DomainError("RENDER_REPORT_INVALID", "机器验收报告与持久检查不一致", 409) from exc


def warning_review_snapshot(
    db: Session, artifact: RenderArtifact, report: RenderMachineReport
) -> ReviewSnapshot:
    manifest = db.get(RenderManifest, artifact.manifest_id)
    link = db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id))
    assert manifest is not None and link is not None
    return ReviewSnapshot(
        project_id=manifest.project_id,
        episode_id=checked_snapshot(manifest).timeline.episode_id,
        run_id=None,
        thread_id=f"render-delivery:{artifact.id}",
        kind="DELIVERY",
        object_id=artifact.id,
        version_id=report.asset_version_id,
        content_sha256=link.checksum,
        input_sha256=digest(
            {
                "input_sha256": report.input_sha256,
                "report_sha256": link.checksum,
                "warning_ids": [warning.id for warning in report.warnings],
            }
        ),
        source_version_ids=sorted(
            {
                report.timeline_id,
                report.asset_version_id,
                *(source.asset_version_id for source in report.sources),
            }
        ),
    )


def read_delivery_review(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    artifact = db.get(RenderArtifact, frozen.object_id)
    if artifact is None:
        raise DomainError("REVIEW_INPUT_CHANGED", "原精确成片不存在", 409)
    report = read_machine_report(db, artifact, current=True)
    if report is None or not report.warnings:
        raise DomainError("REVIEW_INPUT_CHANGED", "原机器报告没有待确认例外", 409)
    return warning_review_snapshot(db, artifact, report)


def check_delivery_decision(
    db: Session, row: HumanReviewRequest, decision: HumanDecision, actor: str
) -> None:
    if decision.decision == "APPROVE" and not decision.comment.strip():
        raise DomainError("REVIEW_REASON_REQUIRED", "确认成片全部例外须记录人工依据", 422)


def apply_delivery_review(
    db: Session, row: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    answer = HumanDecision.model_validate(decision.decision_json)
    check_delivery_decision(db, row, answer, actor)
    return {"status": "APPROVED" if answer.decision == "APPROVE" else "REJECTED"}


DELIVERY_HANDLERS = {
    DELIVERY_HANDLER: ReviewHandler(
        read_delivery_review, apply_delivery_review, check_delivery_decision
    )
}


def request_delivery_warning_review(
    db: Session, artifact: RenderArtifact, actor: str
) -> HumanReviewRequest:
    manifest = db.get(RenderManifest, artifact.manifest_id)
    assert manifest is not None
    require_review_actor(db, manifest.project_id, actor, owner=True)
    report = read_machine_report(db, artifact, current=True)
    if report is None or not report.warnings:
        raise DomainError("RENDER_WARNINGS_NOT_PENDING", "该成片没有待确认的机器验收例外", 409)
    return request_review(
        db,
        actor,
        f"render-delivery:{artifact.id}",
        DELIVERY_HANDLER,
        warning_review_snapshot(db, artifact, report),
    )


def delivery_acceptance(
    db: Session, artifact: RenderArtifact, actor: str
) -> RenderDeliveryAcceptance:
    manifest = db.get(RenderManifest, artifact.manifest_id)
    assert manifest is not None
    require_project(db, manifest.project_id, actor)
    if checked_snapshot(manifest).output.name == "PREVIEW":
        return RenderDeliveryAcceptance(phase="PREVIEW")
    link = db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id))
    if link is None or link.machine_report_json is None:
        return RenderDeliveryAcceptance(phase="LEGACY")
    try:
        report = read_machine_report(db, artifact, current=True)
        assert report is not None
        if not report.warnings:
            return RenderDeliveryAcceptance(
                phase="COMPLETED", is_delivery=True, warnings_confirmed=True
            )
        expected = warning_review_snapshot(db, artifact, report)
        identity = uuid5(manifest.project_id, f"human-review/1:render-delivery:{artifact.id}")
        request = db.get(HumanReviewRequest, identity, populate_existing=True)
        decision = db.scalar(
            select(HumanReviewDecision).where(HumanReviewDecision.request_id == identity)
        )
        resume = db.scalar(
            select(HumanReviewResume).where(HumanReviewResume.request_id == identity)
        )
        pending = RenderDeliveryAcceptance(
            phase="DELIVERY_QA",
            review_id=request.id if request else None,
            blocking_issues=("WARNINGS_NOT_CONFIRMED",),
        )
        if request is None or decision is None or resume is None:
            return pending
        answer = HumanDecision.model_validate(decision.decision_json)
        handoff = ReviewHandoff.model_validate(resume.result_json)
        if (
            request.handler != DELIVERY_HANDLER
            or request.project_id != manifest.project_id
            or request.episode_id != expected.episode_id
            or request.run_id is not None
            or request.snapshot_json != expected.model_dump(mode="json")
            or request.request_sha256 != digest(request.snapshot_json)
            or answer.version_id != report.asset_version_id
            or answer.request_sha256 != request.request_sha256
            or answer.decision != "APPROVE"
            or not answer.comment.strip()
            or resume.decision_id != decision.id
            or handoff.status != "APPROVED"
            or handoff.version_id != report.asset_version_id
            or handoff.run_id is not None
            or handoff.thread_id != expected.thread_id
        ):
            return pending
        require_review_actor(db, manifest.project_id, decision.actor_id, owner=True)
        require_review_actor(db, manifest.project_id, resume.actor_id, owner=True)
        return RenderDeliveryAcceptance(
            phase="COMPLETED_WITH_WARNINGS",
            is_delivery=True,
            warnings_confirmed=True,
            review_id=request.id,
            reviewed_by_user_id=decision.actor_id,
        )
    except (DomainError, ValidationError, ValueError, TypeError, KeyError) as exc:
        code = exc.code if isinstance(exc, DomainError) else "RENDER_REPORT_INVALID"
        return RenderDeliveryAcceptance(phase="DELIVERY_QA", blocking_issues=(code,))
