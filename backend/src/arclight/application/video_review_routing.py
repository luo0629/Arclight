"""Persist exact Reviewer evidence and hand insufficient conclusions to QA-06."""

from datetime import UTC, datetime
from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.human_reviews import ReviewHandler, request_review, require_review_actor
from arclight.application.production_versions import bind, checked_snapshot, component, find_binding
from arclight.application.video_review import latest_video_review
from arclight.application.video_review_evidence import VideoEvidence
from arclight.application.video_review_work import (
    VideoReviewInputs,
    read_temporal_work,
    review_inputs,
)
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.domain.video_review import (
    VIDEO_ADMISSION_RESOURCE,
    VIDEO_HUMAN_HANDLER,
    VIDEO_REVIEW_RESOURCE,
    VideoReviewAdmission,
    VideoReviewIssue,
    VideoReviewRecord,
    VideoReviewReport,
    video_review_input_sha256,
)
from arclight.domain.video_temporal_reviewer import (
    TEMPORAL_AGENT,
    TEMPORAL_PROMPT,
    TemporalAssessment,
)
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import WorkflowRun

CATEGORIES = ("身份与服装", "闪烁与畸变", "动作完成", "人物互动", "空间与道具", "异常静止")


def temporal_report_id(run: WorkflowRun, candidate_id: UUID) -> UUID:
    return uuid5(run.id, "temporal-report:" + str(candidate_id))


def receipt_payload(record: VideoReviewRecord, **data) -> dict:
    return {
        "schema_version": 1,
        "compatibility": "EXACT_DEFINITIONS",
        "components": [version.model_dump(mode="json") for version in record.report.versions],
        "parameters": {},
        "parameter_sha256": digest({}),
        **data,
    }


def record_temporal_review(
    db: Session,
    run: WorkflowRun,
    actor: str,
    inputs: VideoReviewInputs,
    evidence: VideoEvidence,
    assessment: TemporalAssessment | None,
    evidence_gap: str | None,
    model: dict,
) -> VideoReviewRecord:
    project_id = inputs.job.project_id
    episode_id = inputs.job.episode_id
    video = inputs.check.video
    assert episode_id is not None and video is not None
    require_review_actor(db, project_id, actor, owner=False)
    report_id = temporal_report_id(run, inputs.target.candidate_id)
    versions = [
        component(
            db,
            project_id,
            "REVIEW",
            "AGENT",
            "continuity-video-review",
            "1",
            {"agent": TEMPORAL_AGENT},
        ),
        component(
            db,
            project_id,
            "REVIEW",
            "PROMPT",
            "continuity-video-review",
            "1",
            {"instruction": TEMPORAL_PROMPT},
        ),
        component(
            db,
            project_id,
            "REVIEW",
            "SCHEMA",
            "video.temporal.review",
            "video-temporal-review/1",
            {"schema": VideoReviewReport.model_json_schema(), "authority": "advisory-only/1"},
        ),
        component(db, project_id, "REVIEW", "MODEL", "continuity-video-review", "1", model),
    ]
    limitations = [
        "有界均匀抽取视频帧，逐帧完整解码用于文件检查。抽样之间的动作、瞬时闪烁和声音未逐一审查。"
    ]
    if evidence_gap is not None:
        limitations.append(evidence_gap)
    issues = []
    observations = {}
    conclusion = "HUMAN_REVIEW"
    summary = evidence_gap or "缺少可归属的视觉观察，请人工观看完整候选。"
    if assessment is not None:
        summary = assessment.summary
        observations = {item.time_ms: item.observation for item in assessment.observations}
        for category, finding in zip(CATEGORIES, assessment.findings(), strict=True):
            if finding.status == "ISSUE":
                issues.append(
                    VideoReviewIssue(
                        issue_id=uuid5(report_id, category),
                        time_ms=finding.time_ms,
                        description=f"{category} · {finding.description[:950]}",
                        resolved=False,
                    )
                )
            if finding.status == "INSUFFICIENT":
                limitations.append(f"{category} · {finding.description[:950]}")
        if evidence.samples[0].time_ms == 0 and issues:
            conclusion = "REVISION_REQUIRED"
        elif (
            evidence.end_covered
            and evidence_gap is None
            and all(item.status == "CLEAR" for item in assessment.findings())
        ):
            conclusion = "PASS"
    report = VideoReviewReport.model_validate(
        {
            "report_id": report_id,
            "project_id": project_id,
            "episode_id": episode_id,
            "target": inputs.target.model_dump(mode="json"),
            "versions": versions,
            "duration_ms": video.duration_ms,
            "sampling_strategy": "BOUNDED_UNIFORM",
            "coverage_start_ms": 0,
            "coverage_end_ms": video.duration_ms,
            "samples": [
                frame.model_dump(mode="json")
                | {"observation": observations.get(frame.time_ms, "媒体沙箱取得的可解码帧")}
                for frame in evidence.samples
            ],
            "limitations": limitations,
            "conclusion": conclusion,
            "summary": summary,
            "issues": issues,
        }
    )
    try:
        previous = latest_video_review(db, project_id, inputs.target.candidate_id)
    except DomainError as error:
        if error.code != "VIDEO_REVIEW_REQUIRED":
            raise
        previous = None
    if previous is not None and previous.report.target != report.target:
        raise DomainError("VIDEO_REVIEW_TARGET_INVALID", "重审不能改变原视频和素材", 409)
    record = VideoReviewRecord(
        report=report,
        input_sha256=video_review_input_sha256(report),
        report_sha256=digest(report.model_dump(mode="json")),
        sequence=previous.sequence + 1 if previous else 1,
        previous_report_id=previous.report.report_id if previous else None,
        previous_record_sha256=digest(previous.model_dump(mode="json")) if previous else None,
        recorded_by=actor,
        recorded_at=datetime.now(UTC),
    )
    bind(
        db,
        project_id,
        VIDEO_REVIEW_RESOURCE,
        report.report_id,
        receipt_payload(
            record,
            video_review=record.model_dump(mode="json"),
            temporal_evidence={
                "sheet_sha256": evidence.sheet_sha256,
                "shot_context_sha256": digest(evidence.shot_context),
                "assessment": assessment.model_dump(mode="json") if assessment else None,
                "limitations": evidence_gap,
            },
        ),
    )
    human = None
    if conclusion == "HUMAN_REVIEW":
        human = request_review(
            db,
            actor,
            f"video-temporal:{report_id}",
            VIDEO_HUMAN_HANDLER,
            temporal_snapshot(inputs, record, run),
        )
    admission = VideoReviewAdmission(
        report_id=report.report_id,
        record_sha256=digest(record.model_dump(mode="json")),
        requires_human_review=human is not None,
        human_review_request_id=human.id if human else None,
    )
    bind(
        db,
        project_id,
        VIDEO_ADMISSION_RESOURCE,
        report.report_id,
        receipt_payload(
            record,
            video_admission=admission.model_dump(mode="json"),
        ),
    )
    audit(
        db,
        project_id,
        actor,
        "video_review.routed",
        report.report_id,
        {
            "candidate_id": str(inputs.target.candidate_id),
            "conclusion": conclusion,
        },
    )
    return record


def temporal_snapshot(
    inputs: VideoReviewInputs, record: VideoReviewRecord, run: WorkflowRun
) -> ReviewSnapshot:
    return ReviewSnapshot(
        project_id=record.report.project_id,
        episode_id=record.report.episode_id,
        run_id=run.id,
        thread_id=str(run.id),
        kind="EXCEPTIONAL_SHOT",
        object_id=record.report.report_id,
        version_id=inputs.version.id,
        content_sha256=inputs.version.checksum,
        input_sha256=digest(record.model_dump(mode="json")),
        source_version_ids=sorted(
            {
                UUID(item["source_asset_version_id"])
                for item in inputs.manifest.snapshot_json["inputs"]
            },
            key=str,
        ),
        expected_revision=record.sequence - 1,
    )


def current_temporal_review(
    db: Session, project_id: UUID, report_id: UUID
) -> tuple[VideoReviewRecord, VideoReviewInputs, WorkflowRun]:
    binding = find_binding(db, project_id, VIDEO_REVIEW_RESOURCE, report_id)
    if binding is None:
        raise DomainError("NOT_FOUND", "视频时序审查不存在", 404)
    original = VideoReviewRecord.model_validate(checked_snapshot(db, binding)["video_review"])
    record = latest_video_review(db, project_id, original.report.target.candidate_id)
    inputs = review_inputs(db, project_id, record.report.target.candidate_id)
    work = read_temporal_work(db, project_id, inputs.target.candidate_id)
    run = db.get(WorkflowRun, work.run_id) if work is not None else None
    if (
        record.report.report_id != report_id
        or record.report.target != inputs.target
        or work is None
        or work.target != inputs.target
        or run is None
        or run.episode_id != record.report.episode_id
        or temporal_report_id(run, inputs.target.candidate_id) != report_id
    ):
        raise DomainError("VIDEO_REVIEW_TARGET_INVALID", "视频报告或精确候选已变化", 409)
    require_review_actor(db, project_id, record.recorded_by, owner=False)
    return record, inputs, run


def require_temporal_revision(
    db: Session, project_id: UUID, report_id: UUID
) -> tuple[VideoReviewRecord, VideoReviewInputs]:
    record, inputs, run = current_temporal_review(db, project_id, report_id)
    binding = find_binding(db, project_id, VIDEO_ADMISSION_RESOURCE, report_id)
    if binding is None or run.status not in {"RUNNING", "WAITING_REVIEW", "QUEUED"}:
        raise DomainError("REVISION_NOT_ALLOWED", "视频修订要求当前有效的时序路由", 409)
    route = VideoReviewAdmission.model_validate(checked_snapshot(db, binding)["video_admission"])
    if (
        route.report_id != report_id
        or route.record_sha256 != digest(record.model_dump(mode="json"))
        or route.requires_human_review
        or record.report.conclusion != "REVISION_REQUIRED"
    ):
        raise DomainError("REVISION_NOT_ALLOWED", "视频修订须引用明确且有效的时序问题", 409)
    return record, inputs


def read_temporal_exception(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    record, inputs, run = current_temporal_review(db, frozen.project_id, frozen.object_id)
    if record.report.conclusion != "HUMAN_REVIEW":
        raise DomainError("REVIEW_INPUT_CHANGED", "视频时序证据无需例外处理", 409)
    return temporal_snapshot(inputs, record, run)


def check_temporal_decision(
    db: Session, request: HumanReviewRequest, decision: HumanDecision, actor: str
) -> None:
    record, _, _ = current_temporal_review(
        db, request.project_id, UUID(request.snapshot_json["object_id"])
    )
    if not decision.comment.strip() or decision.performance_intent is not None:
        raise DomainError("REVIEW_DECISION_INVALID", "请填写观看完整视频后的精确处理理由", 422)
    if decision.decision == "APPROVE" and any(not issue.resolved for issue in record.report.issues):
        raise DomainError("VIDEO_REVISION_REQUIRED", "已识别的时序问题须修订并重审", 409)


def apply_temporal_decision(
    db: Session, request: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    answer = HumanDecision.model_validate(decision.decision_json)
    check_temporal_decision(db, request, answer, actor)
    # Only materialize the existing human-queue handoff. Video lifecycle actions
    # remain separate, with their original CAS, permission and admission guards.
    return {"status": "APPROVED" if answer.decision == "APPROVE" else "REJECTED"}


VIDEO_REVIEW_HANDLERS = {
    VIDEO_HUMAN_HANDLER: ReviewHandler(
        read_temporal_exception, apply_temporal_decision, check_temporal_decision
    ),
}
