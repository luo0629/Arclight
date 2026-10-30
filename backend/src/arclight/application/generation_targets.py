"""One active generation per registered target, with frozen selection expectations."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.domain.generation import ACTIVE_STATUSES
from arclight.persistence.generation import GenerationJob


def require_available_target(
    db: Session, asset_id: UUID | None, *, exclude_job_id: UUID | None = None
) -> None:
    if asset_id is None:
        return
    statement = select(GenerationJob.id).where(
        GenerationJob.target_asset_id == asset_id, GenerationJob.status.in_(ACTIVE_STATUSES)
    )
    if exclude_job_id is not None:
        statement = statement.where(GenerationJob.id != exclude_job_id)
    if db.scalar(statement.limit(1)) is not None:
        raise DomainError(
            "GENERATION_TARGET_BUSY", "此资产已有未结束的生成任务，请先完成或确认取消", 409
        )
