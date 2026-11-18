"""Bridge existing text approval services into the common immutable review journal."""

from typing import Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import (
    ScriptContent,
    StoryboardContent,
    StoryboardContentV3,
    read_script_content,
    read_storyboard_content,
)
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
from arclight.domain.human_reviews import (
    HumanDecision,
    PerformanceIntentApproval,
    ReviewSnapshot,
)
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


PERFORMANCE_INTENT_BLOCKERS = frozenset(
    {
        "PERFORMANCE_MOTION_UNVERIFIED",
        "PERFORMANCE_CAPABILITY_UNSUPPORTED",
        "PERFORMANCE_CAPABILITY_UNVERIFIED",
        "PERFORMANCE_LIPSYNC_UNDECLARED",
        "PERFORMANCE_MOCK_NOT_PRODUCTION",
    }
)


def _performance_intent_facts(
    db: Session,
    artifact: Storyboard,
    content: StoryboardContentV3,
    source: Script,
) -> PerformanceIntentApproval:
    from arclight.application.director_performance import check_performance_storyboard

    run = db.get(WorkflowRun, artifact.run_id) if artifact.run_id else None
    issues = check_performance_storyboard(
        db, content, read_script_content(source.content_json), run=run
    )
    structural = next(
        (issue for issue in issues if issue.code not in PERFORMANCE_INTENT_BLOCKERS), None
    )
    if structural is not None:
        raise DomainError(
            "PERFORMANCE_NOT_READY", "表演意图仍有必须修订的问题：" + structural.message, 409
        )
    return PerformanceIntentApproval(
        source_script_version_id=source.id,
        source_script_sha256=digest(source.content_json),
        blockers=issues,
    )


def performance_intent_evidence(
    db: Session, artifact: Script | Storyboard, decision: str, comment: str
) -> PerformanceIntentApproval:
    if decision != "APPROVE" or not isinstance(artifact, Storyboard):
        raise DomainError("PERFORMANCE_INTENT_INVALID", "仅3.0表演分镜可确认表演意图", 422)
    content = read_storyboard_content(artifact.content_json)
    if not isinstance(content, StoryboardContentV3):
        raise DomainError("PERFORMANCE_INTENT_INVALID", "仅3.0表演分镜可确认表演意图", 422)
    if not comment.strip():
        raise DomainError("PERFORMANCE_INTENT_REASON_REQUIRED", "仅确认表演意图须填写理由", 422)
    source = db.get(Script, artifact.script_version_id, populate_existing=True)
    if source is None or source.episode_id != artifact.episode_id or source.status != "APPROVED":
        raise DomainError("SCRIPT_NOT_APPROVED", "请先批准本剧集的来源剧本版本", 409)
    return _performance_intent_facts(db, artifact, content, source)


def has_performance_intent_approval(db: Session, storyboard_id: UUID) -> bool:
    decision = db.scalar(
        select(HumanReviewDecision)
        .join(HumanReviewRequest, HumanReviewRequest.id == HumanReviewDecision.request_id)
        .where(
            HumanReviewRequest.creation_key == "text:" + str(storyboard_id),
            HumanReviewRequest.handler == "TEXT_STORYBOARD",
        )
    )
    # The immutable decision remains binding even if provider declarations later improve.
    return decision is not None and decision.decision_json.get("performance_intent") is not None


def require_performance_production(db: Session, storyboard_id: UUID) -> None:
    if has_performance_intent_approval(db, storyboard_id):
        raise DomainError(
            "PERFORMANCE_NOT_READY",
            "此版本仅确认表演意图，制作条件仍阻塞；请另存新版本并重新进行能力审核。",
            409,
        )


def prepare_text_decision(
    db: Session,
    artifact: Script | Storyboard,
    actor: str,
    decision: str,
    comment: str,
    impact_digest: str | None,
    *,
    performance_intent: PerformanceIntentApproval | None = None,
) -> tuple[HumanReviewRequest, HumanReviewDecision]:
    row = pause_for_content(db, artifact, actor)
    payload = HumanDecision.model_validate(
        {
            "version_id": artifact.id,
            "request_sha256": row.request_sha256,
            "decision": decision,
            "comment": comment,
            "impact_digest": impact_digest,
            "performance_intent": performance_intent,
        }
    )
    return row, submit_decision(db, row.id, actor, payload)


def validate_manual_approval(
    db: Session,
    artifact: Script | Storyboard,
    settings: Settings,
    *,
    performance_intent: PerformanceIntentApproval | None = None,
    target_duration_ms: int | None = None,
) -> None:
    episode = db.get(Episode, artifact.episode_id)
    assert episode is not None
    parsed: ScriptContent | StoryboardContent
    if isinstance(artifact, Script):
        if performance_intent is not None:
            raise DomainError("PERFORMANCE_INTENT_INVALID", "剧本不能使用表演意图确认", 422)
        parsed = read_script_content(artifact.content_json)
        validate_script_assets(db, episode.project_id, parsed)
    else:
        source = db.get(Script, artifact.script_version_id, populate_existing=True)
        if source is None or source.episode_id != episode.id or source.status != "APPROVED":
            raise DomainError("SCRIPT_NOT_APPROVED", "请先批准本剧集的来源剧本版本", 409)
        parsed = read_storyboard_content(artifact.content_json)
        source_content = read_script_content(source.content_json)
        try:
            parsed.validate_against(source_content)
        except ValueError as exc:
            raise DomainError("INVALID_REFERENCES", str(exc), 422) from exc
        if isinstance(parsed, StoryboardContentV3):
            facts = _performance_intent_facts(db, artifact, parsed, source)
            if performance_intent is not None:
                if facts != performance_intent:
                    raise DomainError(
                        "PERFORMANCE_INTENT_CHANGED",
                        "表演意图确认的精确来源或能力阻断已变化，请刷新后重新决定。",
                        409,
                    )
            else:
                # Motion is verified on the resulting video. Storyboard approval still
                # needs a capable route so the shot can produce that exact candidate.
                blocker = next(
                    (
                        item
                        for item in facts.blockers
                        if item.code != "PERFORMANCE_MOTION_UNVERIFIED"
                    ),
                    None,
                )
                if blocker is not None:
                    raise DomainError(
                        "PERFORMANCE_NOT_READY",
                        "表演分镜尚不具备制作条件："
                        + blocker.message
                        + "；请修订制作路线，或明确仅确认表演意图并保留制作阻塞。",
                        409,
                    )
        elif performance_intent is not None:
            raise DomainError("PERFORMANCE_INTENT_INVALID", "仅3.0表演分镜可确认表演意图", 422)
        validate_storyboard_assets(db, episode.project_id, parsed, episode_id=episode.id)
    # Run-bound reviews supply their frozen target; manual reviews use the current episode.
    validate_duration(
        parsed,
        episode.target_duration_ms if target_duration_ms is None else target_duration_ms,
        settings.text_duration_tolerance_percent,
    )


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
        validate_manual_approval(
            db, artifact, settings, performance_intent=payload.performance_intent
        )
    artifact.status = "APPROVED" if payload.decision == "APPROVE" else "REJECTED"
    if isinstance(artifact, Storyboard):
        for shot in db.scalars(select(Shot).where(Shot.storyboard_id == artifact.id)):
            shot.status = artifact.status
    review_registry(db, artifact, actor, payload.impact_digest)
    state_receipt = None
    if isinstance(artifact, Script) and payload.decision == "APPROVE":
        from arclight.application.script_state_commit import commit_script_state

        state_receipt = commit_script_state(db, artifact, actor)
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
    result: dict[str, object] = {"version_id": str(artifact.id), "status": artifact.status}
    if state_receipt is not None:
        result["story_state_receipt"] = state_receipt
    if payload.performance_intent is not None:
        result["performance_intent"] = payload.performance_intent.model_dump(mode="json")
    return result


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
                    "performance_intent_only": payload.performance_intent is not None,
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
    result = apply_manual_content(db, artifact, actor, payload, settings)
    return {key: value for key, value in result.items() if key != "performance_intent"}


def check_text_decision(
    db: Session, row: HumanReviewRequest, payload: HumanDecision, actor: str
) -> None:
    snapshot = ReviewSnapshot.model_validate(row.snapshot_json)
    artifact = (
        db.get(Script, snapshot.version_id)
        if snapshot.kind == "SCRIPT"
        else db.get(Storyboard, snapshot.version_id)
    )
    if artifact is None:
        raise DomainError("REVIEW_INPUT_CHANGED", "精确文本对象不存在", 409)
    if payload.performance_intent is None and (
        not isinstance(artifact, Storyboard) or artifact.content_json.get("schema_version") != "3.0"
    ):
        return
    if payload.decision == "APPROVE":
        settings = db.info.get("settings")
        if not isinstance(settings, Settings):
            raise DomainError("REVIEW_SETTINGS_UNAVAILABLE", "审核执行配置不可用", 503)
        run = db.get(WorkflowRun, row.run_id) if row.run_id else None
        validate_manual_approval(
            db,
            artifact,
            settings,
            performance_intent=payload.performance_intent,
            target_duration_ms=run.input_json["target_duration_ms"] if run else None,
        )


def complete_manual_review(
    db: Session,
    artifact: Script | Storyboard,
    actor: str,
    settings: Settings,
    *,
    decision: str,
    comment: str,
    impact_digest: str | None,
    performance_intent_only: bool = False,
) -> dict:
    # Preserve the established manual endpoint's domain error precedence.
    # The unified journal still independently checks the exact frozen inputs.
    intent = (
        performance_intent_evidence(db, artifact, decision, comment)
        if performance_intent_only
        else None
    )
    if decision == "APPROVE":
        if intent is None:
            validate_manual_approval(db, artifact, settings)
        else:
            validate_manual_approval(db, artifact, settings, performance_intent=intent)
    if intent is None:
        row, decision_record = prepare_text_decision(
            db, artifact, actor, decision, comment, impact_digest
        )
    else:
        row, decision_record = prepare_text_decision(
            db, artifact, actor, decision, comment, impact_digest, performance_intent=intent
        )
    result = apply_manual_content(
        db, artifact, actor, HumanDecision.model_validate(decision_record.decision_json), settings
    )
    if intent is not None:
        result["human_review_request_id"] = str(row.id)
    # The state receipt lives in the ledger and response; keep the legacy handoff shape.
    handoff = {"version_id": result["version_id"], "status": result["status"]}
    record_resume(db, row, decision_record, actor, handoff)
    return result


TEXT_HANDLERS = {
    "TEXT_SCRIPT": ReviewHandler(read_text, apply_text, check_text_decision),
    "TEXT_STORYBOARD": ReviewHandler(read_text, apply_text, check_text_decision),
}
