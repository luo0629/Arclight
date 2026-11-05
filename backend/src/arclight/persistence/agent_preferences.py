"""Confirmed project preferences are append-only versions, not Agent working state."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import Base, Entity


class AgentPreference(Entity, Base):
    __tablename__ = "agent_preferences"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    preference_name: Mapped[str] = mapped_column(String(40))
    revision: Mapped[int]
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_by: Mapped[str] = mapped_column(String(100))
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint(
            "project_id", "preference_name", "revision", name="uq_agent_preferences_revision"
        ),
        UniqueConstraint("project_id", "creation_key", name="uq_agent_preferences_creation"),
        CheckConstraint("revision > 0", name="agent_preference_revision_positive"),
        CheckConstraint(
            "preference_name IN ('narrative_tone', 'dialogue_style', 'pacing', 'visual_tone')",
            name="agent_preference_content_key",
        ),
    )
