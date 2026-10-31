"""Append-only component registrations and exact execution/output provenance."""

from uuid import UUID

from sqlalchemy import CheckConstraint, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from arclight.persistence.base import JSON_TYPE, Base, Entity


class ProductionVersion(Entity, Base):
    __tablename__ = "production_versions"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    family: Mapped[str] = mapped_column(String(16))
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(160))
    version: Mapped[str] = mapped_column(String(160))
    digest: Mapped[str] = mapped_column(String(64))
    definition_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "family",
            "kind",
            "name",
            "version",
            "digest",
            name="uq_production_version",
        ),
        CheckConstraint("family IN ('TEXT','ART','REVIEW','MEDIA')", name="version_family"),
        CheckConstraint("kind IN ('AGENT','PROMPT','SCHEMA','MODEL')", name="version_kind"),
    )


class VersionBinding(Entity, Base):
    __tablename__ = "version_bindings"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    resource_id: Mapped[UUID] = mapped_column(index=True)
    snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        UniqueConstraint("project_id", "resource_type", "resource_id", name="uq_version_binding"),
    )
