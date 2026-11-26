"""Prepare and advance one exact manual MP4 export through the existing journals."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.human_reviews import request_review, resume_review, submit_decision
from arclight.application.production_versions import find_binding
from arclight.application.render_admission import (
    RenderFacts,
    admission_facts,
    validate_render_objects,
)
from arclight.application.render_job_store import read_job
from arclight.application.render_jobs import create_job
from arclight.application.render_manifests import (
    checked_snapshot,
    create_render_manifest,
    manifest_row,
)
from arclight.application.render_plans import check_support
from arclight.application.render_reviews import (
    EXPORT_HANDLER,
    export_review_id,
    export_snapshot,
    formal_export_required,
    require_export_approval,
    require_manual_export_audio,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewResume
from arclight.domain.render_jobs import RenderJobCreate, RenderJobRead
from arclight.domain.render_manifests import (
    FormalExportCommand,
    FormalExportDecision,
    FormalExportRetry,
    RenderManifestCreate,
    RenderManifestRead,
    RenderManifestSettings,
)
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import RenderExecution, RenderJob
from arclight.persistence.render_manifests import RenderManifest


def _manual_manifest(db: Session, manifest_id: UUID, actor: str) -> RenderManifest:
    from arclight.application.short_drama_delivery import AUTOMATIC_EXPORT

    row = manifest_row(db, manifest_id, actor, write=True)
    if checked_snapshot(row).output.name != "FORMAL":
        raise formal_export_required()
    if find_binding(db, row.project_id, AUTOMATIC_EXPORT, row.id) is not None:
        raise DomainError("SHORT_DRAMA_EXPORT_SCOPE", "自动短剧清单须由原制作任务导出", 409)
    return row


def _request(db: Session, row: RenderManifest) -> HumanReviewRequest | None:
    prior = db.get(HumanReviewRequest, export_review_id(row), populate_existing=True)
    if prior is not None and (
        prior.handler != EXPORT_HANDLER or prior.project_id != row.project_id
    ):
        raise DomainError("REVIEW_SCOPE_INVALID", "正式导出审核不属于本精确清单", 409)
    return prior


def _facts(db: Session, row: RenderManifest) -> RenderFacts:
    facts = admission_facts(db, row)
    check_support(facts.snapshot)
    require_manual_export_audio(db, facts)
    return facts


def _recheck(db: Session, row: RenderManifest, checked: RenderFacts) -> RenderFacts:
    current = _facts(db, row)
    if current.input_sha256 != checked.input_sha256:
        raise DomainError("RENDER_INPUT_CHANGED", "导出素材在检查期间已变化，请重新准备", 409)
    return current


def prepare_formal_export(
    factory: sessionmaker[Session],
    settings: Settings,
    timeline_id: UUID,
    actor: str,
    payload: RenderManifestSettings,
) -> tuple[RenderManifestRead, UUID]:
    with factory.begin() as db:
        manifest = create_render_manifest(
            db,
            settings,
            timeline_id,
            actor,
            RenderManifestCreate(**payload.model_dump(), profile="FORMAL"),
        )
        row = _manual_manifest(db, manifest.id, actor)
        if prior := _request(db, row):
            return manifest, prior.id
        facts = _facts(db, row)
    # Full decoding takes place between short authorization/write transactions.
    with factory() as db:
        validate_render_objects(db, settings, facts)
    with factory.begin() as db:
        row = _manual_manifest(db, manifest.id, actor)
        current = _recheck(db, row, facts)
        review = request_review(
            db, actor, f"render:{row.id}", EXPORT_HANDLER, export_snapshot(row, current)
        )
        return manifest, review.id


def _checked_review(
    db: Session, row: RenderManifest, command: FormalExportCommand
) -> HumanReviewRequest:
    review = _request(db, row)
    if review is None:
        raise DomainError("RENDER_EXPORT_REVIEW_REQUIRED", "请先发起正式导出审核", 409)
    if (
        not isinstance(command, FormalExportRetry)
        and command.request_sha256 != review.request_sha256
    ):
        raise DomainError("REVIEW_INPUT_CHANGED", "确认须引用本次正式导出的精确审核", 409)
    return review


def _ensure_resumed(db: Session, review: HumanReviewRequest, actor: str) -> None:
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == review.id)
    )
    if decision is None:
        raise DomainError("RENDER_EXPORT_REVIEW_REQUIRED", "正式导出等待项目拥有者批准", 409)
    if HumanDecision.model_validate(decision.decision_json).decision != "APPROVE":
        raise DomainError("RENDER_EXPORT_NOT_APPROVED", "这次正式导出未获批准", 409)
    if (
        db.scalar(select(HumanReviewResume.id).where(HumanReviewResume.request_id == review.id))
        is None
    ):
        resume_review(
            db,
            review.id,
            actor,
            ReviewResume(decision_id=decision.id, request_sha256=review.request_sha256),
        )


def _existing_job(db: Session, row: RenderManifest, retry_of: UUID | None) -> UUID | None:
    if retry_of is not None:
        failed = db.get(RenderJob, retry_of, populate_existing=True)
        original = db.get(RenderExecution, retry_of, populate_existing=True)
        if failed is None or failed.manifest_id != row.id or original is None:
            raise DomainError("NOT_FOUND", "本清单的原渲染任务不存在", 404)
        if original.status not in {"FAILED", "CANCELLED"}:
            raise DomainError("RENDER_RETRY_CONFLICT", "只能重试失败或已取消的渲染任务", 409)
    return db.scalar(
        select(RenderJob.id)
        .join(RenderExecution, RenderExecution.id == RenderJob.id)
        .where(RenderJob.manifest_id == row.id, RenderExecution.retry_of_id == retry_of)
        .order_by(RenderExecution.created_at.desc(), RenderJob.id.desc())
        .limit(1)
    )


def apply_formal_export(
    factory: sessionmaker[Session],
    settings: Settings,
    manifest_id: UUID,
    actor: str,
    command: FormalExportCommand,
) -> tuple[UUID, RenderJobRead | None]:
    if isinstance(command, FormalExportDecision):
        with factory.begin() as db:
            row = _manual_manifest(db, manifest_id, actor)
            review = _checked_review(db, row, command)
            decision = submit_decision(
                db,
                review.id,
                actor,
                HumanDecision(
                    version_id=row.id,
                    request_sha256=command.request_sha256,
                    decision=command.action,
                    comment=command.comment,
                ),
            )
            if command.action == "REJECT":
                resume_review(
                    db,
                    review.id,
                    actor,
                    ReviewResume(decision_id=decision.id, request_sha256=review.request_sha256),
                )
                return review.id, None

    retry_of = command.retry_of if isinstance(command, FormalExportRetry) else None
    with factory.begin() as db:
        row = _manual_manifest(db, manifest_id, actor)
        review = _checked_review(db, row, command)
        if existing := _existing_job(db, row, retry_of):
            return review.id, read_job(db, existing, actor)
        _ensure_resumed(db, review, actor)
        facts = _facts(db, row)
        require_export_approval(db, row, facts)
    # A decision/resume stays durable if checking bytes or dispatch preparation fails.
    with factory() as db:
        validate_render_objects(db, settings, facts)
    with factory.begin() as db:
        row = _manual_manifest(db, manifest_id, actor)
        review = _checked_review(db, row, command)
        if existing := _existing_job(db, row, retry_of):
            return review.id, read_job(db, existing, actor)
        _recheck(db, row, facts)
        key = uuid5(
            row.id, f"manual-formal-export:retry:{retry_of}" if retry_of else "manual-formal-export"
        )
        job = create_job(
            db, settings, row.id, actor, RenderJobCreate(creation_key=key, retry_of=retry_of)
        )
        return review.id, job
