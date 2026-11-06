"""Proposal control rows and append-only proposal, selection and approval history."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class ArtProposalRecord(Entity, Base):
    __tablename__ = "art_proposals"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    version_no: Mapped[int] = mapped_column(default=1)
    revision: Mapped[int] = mapped_column(default=0)
    archived: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (
        UniqueConstraint("project_id", "creation_key"),
        CheckConstraint("revision >= 0 AND version_no > 0", name="art_proposal_revision"),
    )


class ArtProposalVersion(Entity, Base):
    __tablename__ = "art_proposal_versions"
    proposal_id: Mapped[UUID] = mapped_column(ForeignKey("art_proposals.id"), index=True)
    version_no: Mapped[int]
    creation_key: Mapped[UUID]
    request_sha256: Mapped[str] = mapped_column(String(64))
    proposal_json: Mapped[dict] = mapped_column(JSON_TYPE)
    comparison_json: Mapped[dict] = mapped_column(JSON_TYPE)
    baseline_json: Mapped[dict] = mapped_column(JSON_TYPE)
    input_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("proposal_id", "version_no", name="uq_art_proposal_version_number"),
        UniqueConstraint("proposal_id", "creation_key", name="uq_art_proposal_version_creation"),
        CheckConstraint("version_no > 0", name="art_proposal_version_positive"),
    )


class ArtProposalReview(Entity, Base):
    __tablename__ = "art_proposal_reviews"
    version_id: Mapped[UUID] = mapped_column(ForeignKey("art_proposal_versions.id"), unique=True)
    confirmation_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))


class ArtApprovedVersion(Entity, Base):
    """Immutable directory design versions, and receipts for existing Bible versions.

    Directory revisions are not image versions: no synthetic object or image selection
    is created. Each approval retains the entire before/after directory content.
    """

    __tablename__ = "art_approved_versions"
    proposal_version_id: Mapped[UUID] = mapped_column(ForeignKey("art_proposal_versions.id"))
    design_id: Mapped[UUID]
    target_type: Mapped[str] = mapped_column(String(32))
    asset_id: Mapped[UUID | None] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    visual_bible_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("visual_bible_versions.id")
    )
    version_no: Mapped[int]
    before_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    content_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("proposal_version_id", "design_id"),
        UniqueConstraint("asset_id", "version_no"),
        CheckConstraint(
            "(asset_id IS NULL) <> (visual_bible_version_id IS NULL)", name="art_output_target"
        ),
    )
