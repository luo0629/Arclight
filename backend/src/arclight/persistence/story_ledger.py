"""Versioned narrative ledger, separate from mutable story identities."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class StoryLedgerVersion(Entity, Base):
    __tablename__ = "story_ledger_versions"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    version_no: Mapped[int]
    base_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("story_ledger_versions.id"), nullable=True
    )
    creation_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    content_sha256: Mapped[str] = mapped_column(String(64))
    source_manifest_json: Mapped[dict] = mapped_column(JSON_TYPE)
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    receipt_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("project_id", "version_no", name="uq_story_ledger_version"),
        UniqueConstraint("project_id", "creation_key", name="uq_story_ledger_creation"),
        CheckConstraint("version_no > 0", name="story_ledger_version_positive"),
    )


class StoryLedgerApproval(Entity, Base):
    __tablename__ = "story_ledger_approvals"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    version_id: Mapped[UUID] = mapped_column(ForeignKey("story_ledger_versions.id"))
    creation_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    content_sha256: Mapped[str] = mapped_column(String(64))
    source_manifest_sha256: Mapped[str] = mapped_column(String(64))
    receipt_json: Mapped[dict] = mapped_column(JSON_TYPE)
    approved_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("version_id", name="uq_story_ledger_approval_version"),
        UniqueConstraint("project_id", "creation_key", name="uq_story_ledger_approval_creation"),
    )


class StoryLedgerHead(Entity, Base):
    __tablename__ = "story_ledger_heads"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), unique=True)
    revision: Mapped[int] = mapped_column(default=0, server_default="0")
    latest_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("story_ledger_versions.id"), nullable=True
    )
    approved_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("story_ledger_versions.id"), nullable=True
    )
    approval_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("story_ledger_approvals.id"), nullable=True
    )
    __table_args__ = (CheckConstraint("revision >= 0", name="story_ledger_head_revision"),)
