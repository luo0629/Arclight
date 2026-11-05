"""Immutable source notifications and their atomic coordination receipts."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class ProductionEvent(Entity, Base):
    __tablename__ = "production_events"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    source_kind: Mapped[str] = mapped_column(String(16))
    source_id: Mapped[UUID]
    source_revision: Mapped[int]
    request_sha256: Mapped[str] = mapped_column(String(64))
    event_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        UniqueConstraint(
            "source_kind", "source_id", "source_revision", name="uq_production_event_source"
        ),
        CheckConstraint("source_kind IN ('TEXT', 'MEDIA')", name="production_event_kind"),
        CheckConstraint("source_revision >= 0", name="production_event_revision"),
    )


class ProductionEventReceipt(Entity, Base):
    __tablename__ = "production_event_receipts"
    event_id: Mapped[UUID] = mapped_column(ForeignKey("production_events.id"), index=True)
    evaluation_key: Mapped[UUID]
    result_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (UniqueConstraint("event_id", "evaluation_key"),)
