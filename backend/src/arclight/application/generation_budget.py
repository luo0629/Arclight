"""Scoped budget admission against the immutable reservation and settlement ledger."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.generation_accounting import costs, reserve_next
from arclight.core.errors import DomainError
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationCreate, GenerationSpec
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_budget import (
    GenerationBudgetBatch,
    GenerationBudgetPolicy,
    GenerationBudgetQuote,
)
from arclight.persistence.models import DependencyManifest, Project
from arclight.providers import media_pricing

FINAL_STATUSES = {"SUCCEEDED", "FAILED", "CANCELLED"}


def policy_for(db: Session, project_id: UUID, *, lock: bool = False) -> GenerationBudgetPolicy:
    row = db.get(GenerationBudgetPolicy, project_id, with_for_update=lock, populate_existing=lock)
    if row is None:
        row = GenerationBudgetPolicy(
            project_id=project_id,
            revision=0,
            warning_percent=80,
            max_candidates_per_target=64,
        )
        if lock:
            db.add(row)
            db.flush()
    return row


def provider_quote(db: Session, spec: GenerationSpec, task_type: str) -> CostQuote | None:
    if spec.provider == "mock":
        return media_pricing.quote(spec, task_type)
    settings = db.info.get("settings")
    return media_pricing.quote(spec, task_type, settings.image_service if settings else None)


def quote_job(db: Session, project_id: UUID, payload: GenerationCreate) -> CostQuote:
    if payload.batch_id is not None:
        batch = db.get(GenerationBudgetBatch, payload.batch_id)
        if batch is None or batch.project_id != project_id:
            raise DomainError("BUDGET_BATCH_NOT_FOUND", "预算批次不存在", 404)
        if payload.episode_id != batch.episode_id:
            raise DomainError("BUDGET_BATCH_MISMATCH", "预算批次与剧集不匹配", 422)
    result = provider_quote(db, payload.spec, payload.task_type)
    if result is None:
        raise DomainError(
            "BUDGET_QUOTE_UNAVAILABLE", "收费 Provider 未提供可信报价，已拒绝创建任务", 422
        )
    return CostQuote.model_validate(result.model_dump(mode="json"))


def enforce_budget(
    db: Session,
    project_id: UUID,
    payload: GenerationCreate,
    maximum_cost: Decimal,
    *,
    exclude_pending_job: UUID | None = None,
    lock: bool = True,
) -> None:
    statement = (
        select(Project).where(Project.id == project_id).execution_options(populate_existing=True)
    )
    if lock:
        statement = statement.with_for_update()
    project = db.execute(statement).scalar_one()
    policy = policy_for(db, project_id, lock=lock)
    requested_candidates = payload.spec.batch_size
    if payload.task_type == "IMAGE" and payload.target_asset_id is not None:
        from arclight.persistence.models import AssetVersion

        requested_candidates += len(
            list(
                db.scalars(
                    select(AssetVersion.id).where(
                        AssetVersion.asset_id == payload.target_asset_id,
                    )
                )
            )
        )
        from arclight.persistence.models import DependencyManifest

        for job, manifest in db.execute(
            select(GenerationJob, DependencyManifest)
            .join(DependencyManifest, DependencyManifest.id == GenerationJob.input_manifest_id)
            .where(
                GenerationJob.target_asset_id == payload.target_asset_id,
                ~GenerationJob.status.in_(FINAL_STATUSES),
            )
        ):
            if job.id != exclude_pending_job:
                requested_candidates += manifest.snapshot_json["context"]["generation"].get(
                    "batch_size", 1
                )
    if requested_candidates > policy.max_candidates_per_target:
        raise DomainError("BUDGET_CANDIDATE_LIMIT", "候选数量超过项目上限", 409)
    # Free providers remain usable after a paid budget is exhausted.
    if maximum_cost == 0:
        return
    actual, pending = costs(db, project_id, exclude_pending_job=exclude_pending_job)
    projected = actual + pending + maximum_cost
    if projected > project.budget_limit:
        raise DomainError("BUDGET_EXCEEDED", "项目预算已达到上限，请申请调整额度", 409)
    if payload.episode_id is not None and policy.episode_limit is not None:
        episode_actual, episode_pending = costs(
            db, project_id, episode_id=payload.episode_id, exclude_pending_job=exclude_pending_job
        )
        if episode_actual + episode_pending + maximum_cost > policy.episode_limit:
            raise DomainError("BUDGET_EPISODE_EXCEEDED", "剧集预算已达到上限", 409)
    if policy.task_limit is not None and maximum_cost > policy.task_limit:
        raise DomainError("BUDGET_TASK_EXCEEDED", "单任务预算超过上限", 409)
    if exclude_pending_job is not None and policy.task_limit is not None:
        used, held = costs(
            db, project_id, job_id=exclude_pending_job, exclude_pending_job=exclude_pending_job
        )
        if used + held + maximum_cost > policy.task_limit:
            raise DomainError("BUDGET_TASK_EXCEEDED", "重试将超过单任务累计预算", 409)
    if payload.batch_id is not None:
        batch = db.get(GenerationBudgetBatch, payload.batch_id, with_for_update=lock)
        assert batch is not None
        used, pending = costs(
            db, project_id, batch_id=batch.id, exclude_pending_job=exclude_pending_job
        )
        limit = (
            min(batch.budget_limit, policy.batch_limit)
            if policy.batch_limit is not None
            else batch.budget_limit
        )
        if used + pending + maximum_cost > limit:
            raise DomainError("BUDGET_BATCH_EXCEEDED", "批次预算已达到上限", 409)
    elif policy.batch_limit is not None and maximum_cost > policy.batch_limit:
        raise DomainError("BUDGET_BATCH_EXCEEDED", "候选批量预算超过上限", 409)


def check_execution(
    db: Session, job: GenerationJob, spec: GenerationSpec, *, lock: bool = True
) -> None:
    """Shared budget rules; lock=False is advisory scheduler reading, never reservation."""
    captured = db.scalar(
        select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id)
    )
    # Do not fabricate historical quotes. Only provably free legacy jobs may proceed.
    live = provider_quote(db, spec, job.task_type)
    if live is None or (captured is None and live.maximum_cost != 0):
        raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "执行前缺少可信冻结报价", 409)
    live = CostQuote.model_validate(live.model_dump(mode="json"))
    if captured is not None and (
        captured.maximum_cost != live.maximum_cost
        or captured.currency != live.currency
        or captured.pricing_version != live.pricing_version
    ):
        raise DomainError("BUDGET_QUOTE_CHANGED", "报价已变化，请重新估价并创建新任务", 409)
    manifest = db.get(DependencyManifest, job.input_manifest_id)
    assert manifest is not None
    payload = GenerationCreate.model_validate(
        {
            "episode_id": job.episode_id,
            "batch_id": captured.batch_id if captured else None,
            "target_type": job.target_type,
            "task_type": job.task_type,
            "target_asset_id": job.target_asset_id,
            "expected_asset": manifest.snapshot_json["target"].get("expected_asset"),
            "spec": spec,
        }
    )
    enforce_budget(
        db, job.project_id, payload, live.maximum_cost, exclude_pending_job=job.id, lock=lock
    )
    if lock:
        reserve_next(db, job, live.maximum_cost)


def usage(db: Session, project_id: UUID) -> dict:
    project = db.get(Project, project_id)
    policy = policy_for(db, project_id)
    actual, pending = costs(db, project_id)
    projected = actual + pending
    warning = (
        project is not None
        and projected > 0
        and (projected * 100 >= project.budget_limit * policy.warning_percent)
    )
    return {
        "project_limit": project.budget_limit if project else Decimal("0"),
        "actual_cost": actual,
        "pending_estimate": pending,
        "projected_cost": projected,
        "warning": bool(warning),
        "warning_percent": policy.warning_percent,
        "policy_revision": policy.revision,
        "task_limit": policy.task_limit,
        "episode_limit": policy.episode_limit,
        "batch_limit": policy.batch_limit,
        "max_candidates_per_target": policy.max_candidates_per_target,
        "currency": "USD",
    }
