"""FLOW-03 EXPORT integration: exact inputs, separate decision/resume and live authority."""

from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.human_reviews import ReviewHandler, require_review_actor
from arclight.application.render_admission import RenderFacts, admission_facts
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewHandoff, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import AssetVersion
from arclight.persistence.render_manifests import RenderManifest

EXPORT_HANDLER = "RENDER_EXPORT"


def export_review_id(row: RenderManifest) -> UUID:
    return uuid5(row.project_id, f"human-review/1:render:{row.id}")


def formal_export_required() -> DomainError:
    return DomainError("RENDER_FORMAL_EXPORT_REQUIRED", "请使用导出 MP4 操作处理正式成片", 409)


def export_snapshot(row: RenderManifest, facts: RenderFacts) -> ReviewSnapshot:
    return ReviewSnapshot(
        project_id=row.project_id,
        episode_id=facts.snapshot.timeline.episode_id,
        run_id=None,
        thread_id=f"render:{row.id}",
        kind="EXPORT",
        purpose="PRODUCTION" if facts.snapshot.output.name == "FORMAL" else "PREVIEW",
        object_id=row.id,
        version_id=row.id,
        content_sha256=row.content_sha256,
        input_sha256=facts.input_sha256,
        source_version_ids=facts.source_version_ids,
    )


def read_export(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    row = db.get(RenderManifest, frozen.version_id, populate_existing=True)
    if row is None or row.project_id != frozen.project_id:
        raise DomainError("REVIEW_INPUT_CHANGED", "精确渲染清单不存在或范围已变化", 409)
    return export_snapshot(row, admission_facts(db, row))


def apply_export(
    db: Session, row: HumanReviewRequest, decision: HumanReviewDecision, actor: str
) -> dict:
    # The only mutation is FLOW-03's append-only resume receipt in the caller's
    # transaction. Approval never creates a delivery or an asynchronous render.
    answer = HumanDecision.model_validate(decision.decision_json)
    return {"status": "APPROVED" if answer.decision == "APPROVE" else "REJECTED"}


EXPORT_HANDLERS = {EXPORT_HANDLER: ReviewHandler(read_export, apply_export)}


def require_manual_export_audio(db: Session, facts: RenderFacts) -> None:
    from arclight.application.short_drama_delivery import audio_in_scope

    for source in facts.snapshot.sources:
        if source.asset_type == "AUDIO":
            version = db.get(AssetVersion, source.asset_version_id)
            if version is None or not audio_in_scope(db, version, None):
                raise DomainError(
                    "SHORT_DRAMA_AUDIO_RIGHTS", "该配音的使用授权限于已确认的自动短剧", 409
                )


def require_export_approval(
    db: Session,
    row: RenderManifest,
    facts: RenderFacts,
    *,
    submission: tuple[str, UUID] | None = None,
) -> tuple[UUID | None, UUID | None]:
    if facts.snapshot.output.name == "PREVIEW":
        return None, None
    from arclight.application.short_drama_delivery import (
        render_key,
        require_automatic_export,
    )

    automatic = require_automatic_export(db, row, facts)
    if automatic is not None:
        if submission is not None and submission != (row.created_by, render_key(automatic.id)):
            raise DomainError(
                "SHORT_DRAMA_EXPORT_SCOPE", "本清单的自动导出仅用于获授权的短剧任务", 409
            )
        return None, None
    require_manual_export_audio(db, facts)
    identity = export_review_id(row)
    request = db.get(HumanReviewRequest, identity, populate_existing=True)
    if request is None:
        raise DomainError("RENDER_EXPORT_REVIEW_REQUIRED", "正式导出须先审核本精确渲染清单", 409)
    expected = export_snapshot(row, facts)
    if (
        request.handler != EXPORT_HANDLER
        or request.project_id != row.project_id
        or request.episode_id != expected.episode_id
        or request.run_id is not None
        or request.snapshot_json != expected.model_dump(mode="json")
        or request.request_sha256 != digest(request.snapshot_json)
    ):
        raise DomainError("REVIEW_INPUT_CHANGED", "正式导出批准的精确清单或输入已变化", 409)
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == identity)
    )
    receipt = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == identity))
    if decision is None or receipt is None:
        raise DomainError("RENDER_EXPORT_REVIEW_REQUIRED", "正式导出须保存批准决定并独立恢复", 409)
    try:
        answer = HumanDecision.model_validate(decision.decision_json)
        handoff = ReviewHandoff.model_validate(receipt.result_json)
        valid = (
            answer.decision == "APPROVE"
            and answer.version_id == row.id
            and answer.request_sha256 == request.request_sha256
            and receipt.decision_id == decision.id
            and handoff.status == "APPROVED"
            and handoff.version_id == row.id
            and handoff.run_id is None
            and handoff.thread_id == expected.thread_id
        )
    except (ValidationError, ValueError, TypeError):
        valid = False
    if not valid:
        raise DomainError("RENDER_EXPORT_NOT_APPROVED", "该精确清单未取得正式导出批准", 409)
    require_review_actor(db, row.project_id, decision.actor_id, owner=True)
    return request.id, decision.id
