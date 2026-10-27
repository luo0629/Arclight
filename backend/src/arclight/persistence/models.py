from datetime import datetime
from decimal import Decimal
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from arclight.domain.enums import AssetStatus, RunStatus
from arclight.persistence.base import JSON_TYPE, Base, Entity


class Project(Entity, Base):
    __tablename__ = "projects"
    name: Mapped[str] = mapped_column(String(160))
    owner_id: Mapped[str] = mapped_column(String(100), index=True)
    story_settings: Mapped[dict] = mapped_column(JSON_TYPE, default=dict, server_default="{}")
    style_guide: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    output_profile: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    archived: Mapped[bool] = mapped_column(default=False)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    restore_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deletion_id: Mapped[UUID | None] = mapped_column(nullable=True)
    budget_limit: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    __table_args__ = (CheckConstraint("budget_limit >= 0", name="budget_nonnegative"),)


class StoryEntry(Entity, Base):
    __tablename__ = "story_entries"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    name: Mapped[str] = mapped_column(String(100))
    data_json: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    archived: Mapped[bool] = mapped_column(default=False)
    revision: Mapped[int] = mapped_column(default=0)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        CheckConstraint("revision >= 0", name="story_revision_nonnegative"),
        CheckConstraint(
            "kind IN ('WORLD', 'CHARACTER', 'LOCATION', 'PROP', 'SCENE')", name="story_kind"
        ),
    )


class CharacterConfirmation(Entity, Base):
    """Append-only human identity decisions for an exact immutable source version."""

    __tablename__ = "character_confirmations"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    script_id: Mapped[UUID | None] = mapped_column(ForeignKey("scripts.id"))
    analysis_id: Mapped[UUID | None] = mapped_column(ForeignKey("story_analyses.id"))
    source_name: Mapped[str] = mapped_column(String(100))
    entry_id: Mapped[UUID] = mapped_column(ForeignKey("story_entries.id"))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    snapshot_json: Mapped[dict] = mapped_column(JSON_TYPE)
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("script_id", "source_name", name="uq_character_confirmations_script_name"),
        UniqueConstraint(
            "analysis_id", "source_name", name="uq_character_confirmations_analysis_name"
        ),
        CheckConstraint(
            "(script_id IS NOT NULL AND analysis_id IS NULL) OR "
            "(script_id IS NULL AND analysis_id IS NOT NULL)",
            name="character_confirmation_source",
        ),
    )


class Episode(Entity, Base):
    __tablename__ = "episodes"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_no: Mapped[int]
    title: Mapped[str] = mapped_column(String(160))
    target_duration_ms: Mapped[int] = mapped_column(default=30000)
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    __table_args__ = (
        UniqueConstraint("project_id", "episode_no"),
        CheckConstraint("episode_no > 0", name="episode_positive"),
        CheckConstraint("target_duration_ms > 0", name="duration_positive"),
    )


class StorySource(Entity, Base):
    """Append-only imported source; exact bytes and text are loaded only on detail reads."""

    __tablename__ = "story_sources"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    import_no: Mapped[int]
    import_key: Mapped[UUID]
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    original_name: Mapped[str] = mapped_column(String(160))
    source_type: Mapped[str] = mapped_column(String(16))
    media_type: Mapped[str] = mapped_column(String(64))
    encoding: Mapped[str] = mapped_column(String(16))
    byte_size: Mapped[int]
    sha256: Mapped[str] = mapped_column(String(64))
    raw_bytes: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    text: Mapped[str] = mapped_column(Text, deferred=True)
    source_note: Mapped[str] = mapped_column(String(1000))
    rights_note: Mapped[str] = mapped_column(String(1000))
    created_by: Mapped[str] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint("episode_id", "import_key", name="uq_story_sources_episode_import_key"),
        UniqueConstraint("episode_id", "import_no", name="uq_story_sources_episode_import_no"),
        CheckConstraint("import_no > 0", name="source_import_positive"),
        CheckConstraint("byte_size > 0 AND byte_size <= 262144", name="source_byte_size"),
        CheckConstraint("source_type IN ('TXT', 'MARKDOWN')", name="source_type"),
    )


class StorySourceRevision(Entity, Base):
    """Identity and immutable storage contract for one exact source version."""

    __tablename__ = "story_source_revisions"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("story_sources.id"), unique=True)
    root_source_id: Mapped[UUID] = mapped_column(ForeignKey("story_sources.id"), index=True)
    parent_source_id: Mapped[UUID | None] = mapped_column(ForeignKey("story_sources.id"))
    version_no: Mapped[int]
    change_note: Mapped[str] = mapped_column(String(1000))
    origin_kind: Mapped[str] = mapped_column(String(16))
    rights_status: Mapped[str] = mapped_column(String(16))
    storage_backend: Mapped[str] = mapped_column(String(16), default="database")
    object_key: Mapped[str] = mapped_column(String(160), unique=True)
    __table_args__ = (
        UniqueConstraint("root_source_id", "version_no", name="uq_source_revision_root_version"),
        UniqueConstraint("parent_source_id", name="uq_source_revision_parent"),
        CheckConstraint(
            "(parent_source_id IS NULL AND source_id = root_source_id AND version_no = 1) OR "
            "(parent_source_id IS NOT NULL AND source_id <> root_source_id AND version_no > 1)",
            name="source_revision_shape",
        ),
        CheckConstraint("origin_kind IN ('FILE', 'INLINE', 'TEXT')", name="source_origin_kind"),
        CheckConstraint(
            "rights_status IN ('CONFIRMED', 'UNDECLARED')", name="source_rights_status"
        ),
        CheckConstraint("storage_backend = 'database'", name="source_storage_backend"),
    )


class StorySourceBinding(Entity, Base):
    __tablename__ = "story_source_bindings"
    source_id: Mapped[UUID] = mapped_column(ForeignKey("story_sources.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), unique=True)


class StoryAnalysis(Entity, Base):
    __tablename__ = "story_analyses"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), unique=True)
    version_no: Mapped[int]
    source_id: Mapped[UUID | None] = mapped_column(ForeignKey("story_sources.id"), nullable=True)
    source_name: Mapped[str] = mapped_column(String(160))
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    story_sha256: Mapped[str] = mapped_column(String(64))
    created_by: Mapped[str] = mapped_column(String(100))
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    __table_args__ = (
        UniqueConstraint("episode_id", "version_no"),
        CheckConstraint("version_no > 0", name="analysis_version_positive"),
    )


class StoryOutline(Entity, Base):
    __tablename__ = "story_outlines"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"))
    run_id: Mapped[UUID | None] = mapped_column(ForeignKey("workflow_runs.id"), unique=True)
    base_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("story_outlines.id"))
    version_no: Mapped[int]
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    source_snapshot: Mapped[dict] = mapped_column(JSON_TYPE, deferred=True)
    created_by: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="DRAFT")
    confirmed_by: Mapped[str | None] = mapped_column(String(100))
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    save_key: Mapped[str | None] = mapped_column(String(128))
    request_fingerprint: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint("project_id", "version_no", name="uq_story_outlines_project_version"),
        UniqueConstraint("project_id", "save_key", name="uq_story_outlines_project_save_key"),
        CheckConstraint("version_no > 0", name="outline_version_positive"),
        CheckConstraint("status IN ('DRAFT', 'APPROVED')", name="outline_status"),
        CheckConstraint(
            "(status = 'DRAFT' AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'APPROVED' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="outline_confirmation",
        ),
    )


class WorkflowRun(Entity, Base):
    __tablename__ = "workflow_runs"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    workflow_type: Mapped[str] = mapped_column(String(64), default="script_storyboard")
    workflow_version: Mapped[str] = mapped_column(String(32), default="1.0")
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.QUEUED, index=True)
    trace_id: Mapped[str] = mapped_column(String(64))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    input_json: Mapped[dict] = mapped_column(JSON_TYPE)
    pending_review: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    resume_json: Mapped[dict | None] = mapped_column(JSON_TYPE, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    revision: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("episode_id", "idempotency_key"),)


class Script(Entity, Base):
    __tablename__ = "scripts"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id"), unique=True, nullable=True
    )
    version_no: Mapped[int]
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(32), default=AssetStatus.READY_FOR_REVIEW)
    __table_args__ = (UniqueConstraint("episode_id", "version_no"),)


class Storyboard(Entity, Base):
    __tablename__ = "storyboards"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), index=True)
    run_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("workflow_runs.id"), unique=True, nullable=True
    )
    script_version_id: Mapped[UUID] = mapped_column(ForeignKey("scripts.id"))
    version_no: Mapped[int]
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(32), default=AssetStatus.READY_FOR_REVIEW)
    __table_args__ = (UniqueConstraint("episode_id", "version_no"),)


class Shot(Entity, Base):
    __tablename__ = "shots"
    storyboard_id: Mapped[UUID] = mapped_column(ForeignKey("storyboards.id"), index=True)
    shot_no: Mapped[int]
    duration_ms: Mapped[int]
    description: Mapped[str]
    content_json: Mapped[dict] = mapped_column(JSON_TYPE)
    status: Mapped[str] = mapped_column(String(32), default=AssetStatus.READY_FOR_REVIEW)
    __table_args__ = (
        UniqueConstraint("storyboard_id", "shot_no"),
        CheckConstraint("duration_ms > 0", name="duration_positive"),
    )


class Review(Entity, Base):
    __tablename__ = "reviews"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    resource_type: Mapped[str] = mapped_column(String(32))
    version_id: Mapped[UUID]
    reviewer_id: Mapped[str] = mapped_column(String(100))
    decision: Mapped[str] = mapped_column(String(16))
    comment: Mapped[str] = mapped_column(default="")
    __table_args__ = (UniqueConstraint("run_id", "resource_type", "version_id"),)


class WorkflowEvent(Entity, Base):
    __tablename__ = "workflow_events"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)


class OutboxEvent(Entity, Base):
    __tablename__ = "outbox_events"
    run_id: Mapped[UUID] = mapped_column(ForeignKey("workflow_runs.id"), index=True)
    event_type: Mapped[str] = mapped_column(String(64), default="workflow.execute")
    revision: Mapped[int]
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(default=0)
    __table_args__ = (UniqueConstraint("run_id", "revision"),)


class VisualAsset(Entity, Base):
    __tablename__ = "visual_assets"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    asset_type: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(160))
    status: Mapped[str] = mapped_column(String(32), default=AssetStatus.DRAFT)
    current_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("asset_versions.id", use_alter=True), nullable=True
    )
    lock_version: Mapped[int] = mapped_column(default=0)


class AssetVersion(Entity, Base):
    __tablename__ = "asset_versions"
    asset_id: Mapped[UUID] = mapped_column(ForeignKey("visual_assets.id"), index=True)
    version_no: Mapped[int]
    object_key: Mapped[str] = mapped_column(String(512))
    checksum: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    created_by: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(32), default="DRAFT")
    stale: Mapped[bool] = mapped_column(default=False)
    __table_args__ = (UniqueConstraint("asset_id", "version_no"),)


class AssetDependency(Entity, Base):
    __tablename__ = "asset_dependencies"
    source_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_versions.id"), index=True
    )
    target_asset_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("asset_versions.id"), index=True
    )
    dependency_type: Mapped[str] = mapped_column(String(32))
    __table_args__ = (
        UniqueConstraint("source_asset_version_id", "target_asset_version_id", "dependency_type"),
        CheckConstraint("source_asset_version_id != target_asset_version_id", name="not_self"),
    )


Index("ix_outbox_unpublished", OutboxEvent.published_at, OutboxEvent.created_at)


class User(Entity, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(254), unique=True)
    display_name: Mapped[str] = mapped_column(String(80))
    password_hash: Mapped[str] = mapped_column(String(512))
    disabled: Mapped[bool] = mapped_column(default=False)


class AuthSession(Entity, Base):
    __tablename__ = "auth_sessions"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(default=False)


class ProjectMember(Entity, Base):
    __tablename__ = "project_members"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    role: Mapped[str] = mapped_column(String(16))
    __table_args__ = (
        UniqueConstraint("project_id", "user_id"),
        CheckConstraint("role IN ('editor', 'viewer')", name="member_role"),
    )


class CanvasLayout(Entity, Base):
    __tablename__ = "canvas_layouts"
    episode_id: Mapped[UUID] = mapped_column(ForeignKey("episodes.id"), unique=True)
    revision: Mapped[int] = mapped_column(default=0)
    positions: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)
    viewport: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)


class AuditEvent(Entity, Base):
    __tablename__ = "audit_events"
    project_id: Mapped[UUID] = mapped_column(ForeignKey("projects.id"), index=True)
    actor_id: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(80))
    resource_id: Mapped[UUID]
    details_json: Mapped[dict] = mapped_column(JSON_TYPE, default=dict)


class ModelService(Entity, Base):
    __tablename__ = "model_services"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id"), index=True)
    kind: Mapped[str] = mapped_column(String(16))
    mode: Mapped[str] = mapped_column(String(16), default="server")
    base_url: Mapped[str] = mapped_column(String(500), default="")
    model: Mapped[str] = mapped_column(String(160), default="")
    encrypted_key: Mapped[str | None] = mapped_column(String(8192), nullable=True)
    revision: Mapped[int] = mapped_column(default=1)
    __table_args__ = (UniqueConstraint("user_id", "kind"),)
