"""Append-only requests, authenticated decisions and transactional resume receipts."""

from uuid import UUID

from sqlalchemy import ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class HumanReviewRequest(Entity, Base):
    __tablename__ = "human_review_requests"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID | None] = mapped_column(ForeignKey("episodes.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    creation_key: Mapped[str] = mapped_column(String(160))
    handler: Mapped[str] = mapped_column(String(64))
    requested_by: Mapped[str] = mapped_column(String(100))
    request_sha256: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (UniqueConstraint("project_id", "creation_key"),)


class HumanReviewDecision(Entity, Base):
    __tablename__ = "human_review_decisions"
    request_id: Mapped[UUID] = mapped_column(ForeignKey("human_review_requests.id"), unique=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    decision_json: Mapped[dict] = mapped_column(JSON_TYPE)


class HumanReviewResume(Entity, Base):
    __tablename__ = "human_review_resumes"
    request_id: Mapped[UUID] = mapped_column(ForeignKey("human_review_requests.id"), unique=True)
    decision_id: Mapped[UUID] = mapped_column(ForeignKey("human_review_decisions.id"), unique=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    result_json: Mapped[dict] = mapped_column(JSON_TYPE)
