"""Read-only cost projections. Media gateway calls never form a second ledger."""

from decimal import Decimal
from uuid import UUID

from sqlalchemy import Numeric, Uuid, case, cast, func, literal, select, true, union_all
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.core.errors import DomainError
from arclight.domain.costs import (
    CostDimension,
    CostEntries,
    CostEntry,
    CostGroup,
    CostReport,
    CostTotals,
)
from arclight.persistence.base import utcnow
from arclight.persistence.generation import GenerationAttempt as Attempt
from arclight.persistence.generation import GenerationJob as Job
from arclight.persistence.generation_accounting import GenerationBudgetReservation as Reservation
from arclight.persistence.generation_accounting import GenerationBudgetSettlement as Settlement
from arclight.persistence.generation_budget import GenerationBudgetQuote as Quote
from arclight.persistence.model_gateway import GatewayCall as Call
from arclight.persistence.model_gateway import GatewayReceipt as Receipt
from arclight.persistence.models import Episode, WorkflowRun


def cost_facts(project_id: UUID):
    """One fact per media attempt (including a queued one), or text gateway call."""
    empty_uuid = cast(literal(None), Uuid())
    empty_money = cast(literal(None), Numeric(18, 6))
    zero = cast(literal(0), Numeric(18, 6))
    ranked = (
        select(
            Reservation.id,
            func.row_number()
            .over(
                partition_by=(Reservation.job_id, Reservation.attempt_no),
                order_by=Reservation.sequence.desc(),
            )
            .label("position"),
        )
        .join(Job, Job.id == Reservation.job_id)
        .where(Job.project_id == project_id)
        .subquery()
    )
    media = (
        select(
            Reservation.id.label("id"),
            literal("MEDIA").label("source"),
            Job.id.label("task_id"),
            Job.task_type.label("task_type"),
            Job.episode_id,
            Job.provider,
            Job.model,
            Attempt.id.label("attempt_id"),
            Reservation.attempt_no,
            empty_uuid.label("call_id"),
            Job.input_manifest_id,
            cast(literal(None), WorkflowRun.trace_id.type).label("trace_id"),
            func.coalesce(Attempt.status, Job.status).label("status"),
            Settlement.source.label("billing_source"),
            Reservation.created_at,
            Reservation.amount.label("estimated_cost"),
            Settlement.cost.label("actual_cost"),
            case((Settlement.id.is_(None), Reservation.amount), else_=zero).label(
                "pending_estimate"
            ),
        )
        .join(ranked, (ranked.c.id == Reservation.id) & (ranked.c.position == 1))
        .join(Job, Job.id == Reservation.job_id)
        .outerjoin(Settlement, Settlement.reservation_id == Reservation.id)
        .outerjoin(
            Attempt,
            (Attempt.job_id == Job.id) & (Attempt.attempt_no == Reservation.attempt_no),
        )
        .where(Job.project_id == project_id)
    )
    # Historical calls with no reservation retain their recorded cost, even when
    # a trustworthy quote is unavailable. Never manufacture a zero price.
    legacy = (
        select(
            Attempt.id,
            literal("MEDIA"),
            Job.id,
            Job.task_type,
            Job.episode_id,
            Attempt.provider,
            Attempt.model,
            Attempt.id,
            Attempt.attempt_no,
            empty_uuid,
            Attempt.input_manifest_id,
            cast(literal(None), WorkflowRun.trace_id.type),
            Attempt.status,
            case((Attempt.cost.is_not(None), "LEGACY")),
            Attempt.created_at,
            empty_money,
            Attempt.cost,
            zero,
        )
        .join(Job, Job.id == Attempt.job_id)
        .where(
            Job.project_id == project_id,
            ~select(Reservation.id)
            .where(Reservation.job_id == Job.id, Reservation.attempt_no == Attempt.attempt_no)
            .exists(),
        )
    )
    unstarted = (
        select(
            Job.id,
            literal("MEDIA"),
            Job.id,
            Job.task_type,
            Job.episode_id,
            Job.provider,
            Job.model,
            empty_uuid,
            literal(1),
            empty_uuid,
            Job.input_manifest_id,
            cast(literal(None), WorkflowRun.trace_id.type),
            Job.status,
            literal(None),
            Job.created_at,
            Quote.maximum_cost,
            empty_money,
            func.coalesce(Quote.maximum_cost, zero),
        )
        .outerjoin(Quote, Quote.job_id == Job.id)
        .where(
            Job.project_id == project_id,
            ~select(Reservation.id).where(Reservation.job_id == Job.id).exists(),
            ~select(Attempt.id).where(Attempt.job_id == Job.id).exists(),
        )
    )
    receipts = (
        select(
            Receipt.id,
            func.row_number()
            .over(partition_by=Receipt.call_id, order_by=Receipt.kind.desc())
            .label("position"),
        )
        .join(Call, Call.id == Receipt.call_id)
        .where(Call.project_id == project_id, Call.generation_attempt_id.is_(None))
        .subquery()
    )
    final_receipts = (
        select(Receipt)
        .join(receipts, (receipts.c.id == Receipt.id) & (receipts.c.position == 1))
        .subquery()
    )
    text = (
        select(
            Call.id,
            literal("TEXT"),
            func.coalesce(Call.workflow_id, Call.id),
            Call.task,
            Call.episode_id,
            Call.route_json["provider"].as_string(),
            Call.route_json["model"].as_string(),
            empty_uuid,
            Call.sequence,
            Call.id,
            empty_uuid,
            WorkflowRun.trace_id,
            func.coalesce(final_receipts.c.status, "RESULT_UNKNOWN"),
            final_receipts.c.kind,
            Call.created_at,
            Call.maximum_cost,
            final_receipts.c.actual_cost,
            case(
                (final_receipts.c.actual_cost.is_(None), func.coalesce(Call.maximum_cost, zero)),
                else_=zero,
            ),
        )
        .outerjoin(final_receipts, final_receipts.c.call_id == Call.id)
        .outerjoin(WorkflowRun, WorkflowRun.id == Call.workflow_id)
        .where(Call.project_id == project_id, Call.generation_attempt_id.is_(None))
    )
    return union_all(media, legacy, unstarted, text).subquery()


def _filtered(
    db: Session,
    project_id: UUID,
    actor: str,
    episode_id,
    task_id,
    provider,
    model,
    unassigned_episode: bool,
):
    project = require_project(db, project_id, actor)
    if episode_id is not None:
        episode = db.get(Episode, episode_id)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "剧集不存在", 404)
    facts = cost_facts(project_id)
    statement = select(facts)
    if unassigned_episode:
        if episode_id is not None:
            raise DomainError("COST_SCOPE_INVALID", "未指定剧集筛选不能同时指定剧集", 422)
        statement = statement.where(facts.c.episode_id.is_(None))
    for name, value in (
        ("episode_id", episode_id),
        ("task_id", task_id),
        ("provider", provider),
        ("model", model),
    ):
        if value is not None:
            statement = statement.where(facts.c[name] == value)
    if (
        task_id is not None
        and db.scalar(select(facts.c.id).where(facts.c.task_id == task_id).limit(1)) is None
    ):
        raise DomainError("NOT_FOUND", "费用任务不存在", 404)
    return project, statement.subquery()


def _aggregates(c):
    return (
        func.count().label("entry_count"),
        func.sum(case((c.actual_cost.is_not(None), 1), else_=0)).label("confirmed_count"),
        func.sum(case((c.actual_cost.is_(None) & c.estimated_cost.is_not(None), 1), else_=0)).label(
            "estimated_count"
        ),
        func.sum(case((c.actual_cost.is_(None) & c.estimated_cost.is_(None), 1), else_=0)).label(
            "unpriced_count"
        ),
        func.count(c.estimated_cost).label("quoted_count"),
        func.sum(c.estimated_cost).label("estimated_cost"),
        func.sum(c.actual_cost).label("actual_cost"),
        func.sum(c.pending_estimate).label("pending_estimate"),
    )


def _totals(row, prefix="") -> CostTotals:
    values = {
        key: row[prefix + key] or 0
        for key in (
            "entry_count",
            "confirmed_count",
            "estimated_count",
            "unpriced_count",
            "quoted_count",
        )
    }
    pending = row[prefix + "pending_estimate"] or Decimal("0")
    actual = row[prefix + "actual_cost"] if values["confirmed_count"] else None
    return CostTotals(
        **{key: values[key] for key in values if key != "quoted_count"},
        actual_cost=actual,
        estimated_cost=row[prefix + "estimated_cost"] if values["quoted_count"] else None,
        pending_estimate=pending,
        projected_cost=None if values["unpriced_count"] else (actual or Decimal("0")) + pending,
        cost_is_estimate=bool(values["estimated_count"] or values["unpriced_count"]),
    )


def read_costs(
    db: Session,
    project_id: UUID,
    actor: str,
    *,
    group_by: CostDimension = "project",
    episode_id: UUID | None = None,
    task_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    unassigned_episode: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> CostReport:
    project, facts = _filtered(
        db, project_id, actor, episode_id, task_id, provider, model, unassigned_episode
    )
    c = facts.c
    dimensions = {
        "project": (literal(str(project_id)).label("project_key"),),
        "episode": (c.episode_id, Episode.episode_no, Episode.title),
        "task": (c.source, c.task_id),
        "model": (c.provider, c.model),
    }[group_by]
    groups = (
        select(*dimensions, *_aggregates(c))
        .select_from(facts)
        .outerjoin(Episode, Episode.id == c.episode_id)
        .group_by(*dimensions)
        .subquery()
    )
    page = (
        select(groups)
        .order_by(*[groups.c[col.key] for col in dimensions])
        .limit(limit)
        .offset(offset)
        .subquery()
    )
    summary = select(*_aggregates(c)).select_from(facts).subquery()
    # Totals, page and group count share one database statement/snapshot.
    rows = (
        db.execute(
            select(
                *[col.label("total_" + col.key) for col in summary.c],
                *page.c,
                select(func.count()).select_from(groups).scalar_subquery().label("total_groups"),
            ).select_from(summary.outerjoin(page, true()))
        )
        .mappings()
        .all()
    )
    result = []
    for row in rows:
        if row["entry_count"] is None:
            continue
        if group_by == "episode":
            identity = {"episode_id": row["episode_id"]}
            key = str(row["episode_id"] or "unassigned")
            label = (
                f"第 {row['episode_no']} 集 · {row['title']}"
                if row["episode_id"]
                else "项目级任务（未指定剧集）"
            )
        elif group_by == "task":
            identity = {"task_id": row["task_id"]}
            key = str(row["task_id"])
            label = ("媒体" if row["source"] == "MEDIA" else "文字") + "任务"
        elif group_by == "model":
            identity = {"provider": row["provider"], "model": row["model"]}
            key = f"{row['provider']}/{row['model']}"
            label = key
        else:
            identity = {}
            key, label = str(project_id), project.name
        result.append(CostGroup(key=key, label=label, **identity, **_totals(row).model_dump()))
    return CostReport(
        project_id=project_id,
        budget_limit=project.budget_limit,
        generated_at=utcnow(),
        group_by=group_by,
        summary=_totals(rows[0], "total_"),
        groups=result,
        total_groups=rows[0]["total_groups"],
        limit=limit,
        offset=offset,
    )


def read_entries(
    db: Session,
    project_id: UUID,
    actor: str,
    *,
    episode_id: UUID | None = None,
    task_id: UUID | None = None,
    provider: str | None = None,
    model: str | None = None,
    unassigned_episode: bool = False,
    limit: int = 20,
    offset: int = 0,
) -> CostEntries:
    _, facts = _filtered(
        db, project_id, actor, episode_id, task_id, provider, model, unassigned_episode
    )
    rows = db.execute(
        select(facts).order_by(facts.c.created_at.desc(), facts.c.id).limit(limit).offset(offset)
    ).mappings()
    total = db.scalar(select(func.count()).select_from(facts)) or 0
    return CostEntries(
        items=[CostEntry(**row, cost_is_estimate=row["actual_cost"] is None) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )
