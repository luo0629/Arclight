"""Media price estimates and execution accounting."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.generation_accounting import costs, reserve_next
from arclight.domain.generation_budget import CostQuote
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_budget import GenerationBudgetQuote
from arclight.providers import media_pricing


def quote_job(
    db: Session,
    payload: GenerationCreate,
    *,
    actor: str = "service-owner",
    frozen_service: dict | None = None,
) -> CostQuote | None:
    from arclight.application.media_services import quote_selection, select_for_spec

    spec = payload.spec
    selection = select_for_spec(db, actor, spec, payload.task_type, frozen_service)
    if selection is not None:
        return quote_selection(selection, spec)
    return media_pricing.quote(spec, payload.task_type)


def reserve_execution(db: Session, job: GenerationJob) -> None:
    from arclight.application.art_concept_jobs import validate_concept_execution
    from arclight.application.short_drama_budget import require_active_production, root_for_job

    validate_concept_execution(db, job)
    if (root := root_for_job(db, job)) is not None:
        require_active_production(db, root, job.project_id)
    quote = db.scalar(select(GenerationBudgetQuote).where(GenerationBudgetQuote.job_id == job.id))
    reserve_next(db, job, quote.maximum_cost if quote else None)


def usage(db: Session, project_id: UUID) -> dict:
    actual, pending = costs(db, project_id)
    return {
        "actual_cost": actual,
        "pending_estimate": pending,
        "currency": "USD",
    }
