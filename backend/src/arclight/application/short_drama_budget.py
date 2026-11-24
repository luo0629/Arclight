"""Count exact text calls and media reservations against the same production grant."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import require_episode
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.production_versions import bind, find_binding
from arclight.core.errors import DomainError
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_accounting import (
    GenerationBudgetReservation,
    GenerationBudgetSettlement,
)
from arclight.persistence.model_gateway import GatewayCall
from arclight.persistence.models import WorkflowRun
from arclight.persistence.versions import VersionBinding

MEDIA_SCOPE = "SHORT_DRAMA_MEDIA_JOB"


def root_for_workflow(db: Session, workflow_id: UUID | None) -> WorkflowRun | None:
    if workflow_id is None:
        return None
    workflow = db.get(WorkflowRun, workflow_id)
    if workflow is None:
        return None
    if workflow.input_json.get("short_drama"):
        return workflow
    if workflow.workflow_type not in {
        "script_storyboard",
        "storyboard",
        "art_proposal",
        "continuity_review",
    } or not workflow.input_json.get("created_by"):
        return None
    control = db.get(AgentRunLimit, workflow_id)
    root = db.get(WorkflowRun, control.root_run_id) if control else None
    return root if root and root.input_json.get("short_drama") else None


def require_manual_workflow(db: Session, workflow_id: UUID | None) -> None:
    if root_for_workflow(db, workflow_id) is not None:
        raise DomainError("SHORT_DRAMA_AUTOMATIC", "本次短剧由后台自动推进", 409)


def root_for_job(db: Session, job: GenerationJob) -> WorkflowRun | None:
    scope = find_binding(db, job.project_id, MEDIA_SCOPE, job.id)
    if scope is None:
        return None
    root = db.get(WorkflowRun, UUID(scope.snapshot_json["run_id"]), populate_existing=True)
    if root is None or not root.input_json.get("short_drama"):
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "媒体任务缺少原短剧制作授权", 409)
    return root


def bind_media_job(db: Session, run_id: UUID, job: GenerationJob) -> None:
    if job.episode_id is None:
        raise DomainError("SHORT_DRAMA_MEDIA_SCOPE", "本次媒体任务必须属于原制作剧集", 409)
    episode = require_episode(db, job.episode_id, job.created_by, owner=True, write=True)
    root = db.get(WorkflowRun, run_id, populate_existing=True)
    if (
        root is None
        or root.workflow_type != "producer"
        or root.status != "RUNNING"
        or root.episode_id != episode.id
        or episode.project_id != job.project_id
        or root.input_json.get("created_by") != job.created_by
        or not root.input_json.get("short_drama")
    ):
        raise DomainError("SHORT_DRAMA_MEDIA_SCOPE", "媒体任务与本次短剧制作授权不一致", 409)
    bind(db, job.project_id, MEDIA_SCOPE, job.id, {"run_id": str(run_id)})


def check_budget(db: Session, root: WorkflowRun, project_id: UUID, amount: Decimal | None) -> None:
    from arclight.application.gateway_accounting import latest_receipt
    from arclight.application.model_gateway import utc
    from arclight.application.short_drama import require_exact_inputs
    from arclight.application.short_drama_delivery import require_run_authorization
    from arclight.persistence.base import utcnow

    lock_dependency_graph(db, project_id)
    db.refresh(root)
    control = db.get(AgentRunLimit, root.id)
    if (
        root.status != "RUNNING"
        or control is None
        or control.root_run_id != root.id
        or control.project_id != project_id
        or utcnow() >= utc(control.deadline_at)
    ):
        raise DomainError("SHORT_DRAMA_INACTIVE", "本次短剧制作已停止或超出执行时限", 409)
    episode, _, spec = require_run_authorization(db, root)
    if episode.project_id != project_id:
        raise DomainError("SHORT_DRAMA_INACTIVE", "本次短剧制作不属于此项目", 409)
    require_exact_inputs(db, episode, spec)
    if amount is None:
        raise DomainError("BUDGET_QUOTE_UNAVAILABLE", "短剧制作调用需要可信报价", 409)
    run_ids = select(AgentRunLimit.run_id).where(AgentRunLimit.root_run_id == root.id)
    total = amount
    for call in db.scalars(
        select(GatewayCall).where(
            GatewayCall.project_id == project_id,
            GatewayCall.workflow_id.in_(run_ids),
            GatewayCall.generation_attempt_id.is_(None),
        )
    ):
        receipt = latest_receipt(db, call.id)
        liability = (
            receipt.actual_cost
            if receipt and receipt.actual_cost is not None
            else call.maximum_cost
        )
        if liability is None:
            raise DomainError("BUDGET_UNPRICED_HISTORY", "本次制作尚有未经核账的调用", 409)
        total += liability
    media = db.execute(
        select(GenerationBudgetReservation.amount, GenerationBudgetSettlement.cost)
        .join(VersionBinding, VersionBinding.resource_id == GenerationBudgetReservation.job_id)
        .outerjoin(
            GenerationBudgetSettlement,
            GenerationBudgetSettlement.reservation_id == GenerationBudgetReservation.id,
        )
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == MEDIA_SCOPE,
            VersionBinding.snapshot_json["run_id"].as_string() == str(root.id),
        )
    )
    total += sum((cost if cost is not None else reserved for reserved, cost in media), Decimal(0))
    if total > Decimal(root.input_json["short_drama"]["maximum_cost"]):
        raise DomainError("SHORT_DRAMA_BUDGET_EXCEEDED", "继续制作将超过本次授权的费用上限", 409)
