"""Bridge existing text approval services into the common immutable review journal."""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.application.audit import audit
from arclight.application.content_dependencies import reference_ids, review_registry
from arclight.application.content_duration import validate_duration
from arclight.application.content_references import (
    validate_script_assets,
    validate_storyboard_assets,
)
from arclight.application.human_reviews import (
    ReviewHandler,
    record_resume,
    request_review,
    submit_decision,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import HumanReviewDecision, HumanReviewRequest
from arclight.persistence.models import AssetVersion, Episode, Script, Shot, Storyboard, WorkflowRun


def text_snapshot(
    db: Session, artifact: Script | Storyboard, run: WorkflowRun | None
) -> ReviewSnapshot:
    episode = db.get(Episode, artifact.episode_id, populate_existing=True)
    if episode is None or artifact.status != "READY_FOR_REVIEW":
        raise DomainError("REVIEW_INPUT_CHANGED", "文本对象已不在等待审核状态", 409)
    kind: Literal["SCRIPT", "STORYBOARD"] = (
        "SCRIPT" if isinstance(artifact, Script) else "STORYBOARD"
    )
    source_ids = sorted(UUID(value) for value in reference_ids(artifact.content_json))
    if isinstance(artifact, Storyboard):
        source_ids.append(artifact.script_version_id)
    source_ids = sorted(set(source_ids))
    inputs = []
    for identity in source_ids:
        source = db.get(AssetVersion, identity, populate_existing=True)
        inputs.append(
            {
                "id": str(identity),
                "checksum": source.checksum if source else None,
                "status": source.status if source else None,
                "stale": source.stale if source else None,
                "revision": source.revision if source else None,
            }
        )
    registry = db.get(AssetVersion, artifact.id, populate_existing=True)
    if registry is not None and (registry.stale or registry.status in {"STALE", "ARCHIVED"}):
        raise DomainError("REVIEW_INPUT_CHANGED", "文本输入已经失效，请另存新版本", 409)
    if run is not None and (
        run.status != "WAITING_REVIEW"
        or run.episode_id != episode.id
        or run.pending_review != {"resource_type": kind.lower(), "version_id": str(artifact.id)}
    ):
        raise DomainError("REVIEW_INPUT_CHANGED", "运行已不再等待该精确版本", 409)
    return ReviewSnapshot(
        project_id=episode.project_id,
        episode_id=episode.id,
        run_id=run.id if run else None,
        thread_id=str(run.id) if run else "manual:" + str(artifact.id),
        kind=kind,
        object_id=artifact.id,
        version_id=artifact.id,
        content_sha256=digest(artifact.content_json),
        input_sha256=digest(
            {
                # Manual text has no frozen task startup. Its current duration
                # policy is revalidated by the original approval service; a
                # target change must still allow rejecting this exact draft.
                "startup": run.input_json if run else {"manual_content_id": str(artifact.id)},
                "sources": inputs,
            }
        ),
        source_version_ids=source_ids,
        expected_revision=run.revision if run else 0,
    )


def read_text(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    artifact = (
        db.get(Script, frozen.version_id, populate_existing=True)
        if frozen.kind == "SCRIPT"
        else db.get(Storyboard, frozen.version_id, populate_existing=True)
    )
    if (
        artifact is None
        or artifact.episode_id != frozen.episode_id
        or artifact.run_id != frozen.run_id
    ):
        raise DomainError("REVIEW_INPUT_CHANGED", "精确文本对象不存在或作用域已变化", 409)
    run = db.get(WorkflowRun, frozen.run_id, populate_existing=True) if frozen.run_id else None
    return text_snapshot(db, artifact, run)


def pause_for_content(db: Session, artifact: Script | Storyboard, actor: str) -> HumanReviewRequest:
    run = db.get(WorkflowRun, artifact.run_id) if artifact.run_id else None
    snapshot = text_snapshot(db, artifact, run)
    key = "text:" + str(artifact.id)
    existing = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.project_id == snapshot.project_id,
            HumanReviewRequest.creation_key == key,
        )
    )
    if existing is not None:
        return existing
    return request_review(db, actor, key, "TEXT_" + snapshot.kind, snapshot)


def pause_for_run(db: Session, run: WorkflowRun) -> None:
    from arclight.application.gateway_text import execution_actor

    if not run.pending_review:
        return
    identity = UUID(run.pending_review["version_id"])
    artifact = (
        db.get(Script, identity)
        if run.pending_review["resource_type"] == "script"
        else db.get(Storyboard, identity)
    )
    if artifact is None:
        raise DomainError("REVIEW_INPUT_CHANGED", "待审核对象不存在", 409)
    pause_for_content(db, artifact, execution_actor(db, run))


def prepare_text_decision(
    db: Session,
    artifact: Script | Storyboard,
    actor: str,
    decision: str,
    comment: str,
    impact_digest: str | None,
) -> tuple[HumanReviewRequest, HumanReviewDecision]:
    row = pause_for_content(db, artifact, actor)
    payload = HumanDecision.model_validate(
        {
            "version_id": artifact.id,
            "request_sha256": row.request_sha256,
            "decision": decision,
            "comment": comment,
            "impact_digest": impact_digest,
        }
    )
    return row, submit_decision(db, row.id, actor, payload)


def validate_manual_approval(
    db: Session, artifact: Script | Storyboard, settings: Settings
) -> None:
    episode = db.get(Episode, artifact.episode_id)
    assert episode is not None
    parsed: ScriptContent | StoryboardContent
    if isinstance(artifact, Script):
        parsed = ScriptContent.model_validate(artifact.content_json)
        validate_script_assets(db, episode.project_id, parsed)
    else:
        source = db.get(Script, artifact.script_version_id, populate_existing=True)
        if source is None or source.episode_id != episode.id or source.status != "APPROVED":
            raise DomainError("SCRIPT_NOT_APPROVED", "请先批准本剧集的来源剧本版本", 409)
        parsed = StoryboardContent.model_validate(artifact.content_json)
        validate_storyboard_assets(db, episode.project_id, parsed, episode_id=episode.id)
    validate_duration(parsed, episode.target_duration_ms, settings.text_duration_tolerance_percent)


def apply_manual_content(
    db: Session,
    artifact: Script | Storyboard,
    actor: str,
    payload: HumanDecision,
    settings: Settings,
) -> dict:
    episode = db.get(Episode, artifact.episode_id)
    assert episode is not None
    if payload.decision == "APPROVE":
        validate_manual_approval(db, artifact, settings)
    artifact.status = "APPROVED" if payload.decision == "APPROVE" else "REJECTED"
    if isinstance(artifact, Storyboard):
        for shot in db.scalars(select(Shot).where(Shot.storyboard_id == artifact.id)):
            shot.status = artifact.status
    review_registry(db, artifact, actor, payload.impact_digest)
    kind = "scripts" if isinstance(artifact, Script) else "storyboards"
    audit(
        db,
        episode.project_id,
        actor,
        kind + ".reviewed",
        artifact.id,
        {
            "decision": payload.decision,
            "comment": payload.comment,
            "impact_digest": payload.impact_digest,
        },
    )
    return {"version_id": str(artifact.id), "status": artifact.status}


def apply_text(
    db: Session, row: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    from arclight.api.schemas import ReviewCreate
    from arclight.application.commands import resume_run

    snapshot = ReviewSnapshot.model_validate(row.snapshot_json)
    payload = HumanDecision.model_validate(decision.decision_json)
    settings = db.info.get("settings")
    if not isinstance(settings, Settings):
        raise DomainError("REVIEW_SETTINGS_UNAVAILABLE", "审核执行配置不可用", 503)
    if row.run_id is not None:
        run = db.get(WorkflowRun, row.run_id)
        assert run is not None
        resume_run(
            db,
            run,
            actor,
            ReviewCreate.model_validate(
                {
                    "resource_type": snapshot.kind.lower(),
                    "version_id": snapshot.version_id,
                    "decision": payload.decision,
                    "comment": payload.comment,
                    "impact_digest": payload.impact_digest,
                    "expected_revision": snapshot.expected_revision,
                }
            ),
            settings,
        )
        return {
            "run_id": str(run.id),
            "revision": run.revision,
            "thread_id": snapshot.thread_id,
            "status": "QUEUED",
        }
    artifact = (
        db.get(Script, snapshot.version_id)
        if snapshot.kind == "SCRIPT"
        else db.get(Storyboard, snapshot.version_id)
    )
    assert artifact is not None
    return apply_manual_content(db, artifact, actor, payload, settings)


def complete_manual_review(
    db: Session,
    artifact: Script | Storyboard,
    actor: str,
    settings: Settings,
    *,
    decision: str,
    comment: str,
    impact_digest: str | None,
) -> dict:
    # Preserve the established manual endpoint's domain error precedence.
    # The unified journal still independently checks the exact frozen inputs.
    if decision == "APPROVE":
        validate_manual_approval(db, artifact, settings)
    row, decision_record = prepare_text_decision(
        db, artifact, actor, decision, comment, impact_digest
    )
    result = apply_manual_content(
        db, artifact, actor, HumanDecision.model_validate(decision_record.decision_json), settings
    )
    record_resume(db, row, decision_record, actor, result)
    return result


TEXT_HANDLERS = {
    "TEXT_SCRIPT": ReviewHandler(read_text, apply_text),
    "TEXT_STORYBOARD": ReviewHandler(read_text, apply_text),
}
