"""A deterministic renderer execution, separate from text and provider workflows."""

from datetime import datetime
from uuid import UUID

from sqlalchemy import CheckConstraint, DateTime, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class RenderExecution(Entity, Base):
    __tablename__ = "render_executions"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    input_file_id: Mapped[UUID] = mapped_column(ForeignKey("stored_files.id"))
    retry_of_id: Mapped[UUID | None] = mapped_column(ForeignKey("render_executions.id"))
    created_by: Mapped[str] = mapped_column(String(100))
    storage_identity: Mapped[str] = mapped_column(String(64))
    intermediate_key: Mapped[str] = mapped_column(String(512), unique=True)
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    output_file_id: Mapped[UUID | None] = mapped_column(ForeignKey("stored_files.id"))
    error_code: Mapped[str | None] = mapped_column(String(64))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        CheckConstraint(
            "status IN ('QUEUED','RUNNING','SUCCEEDED','FAILED','CANCELLED')", name="render_status"
        ),
    )
