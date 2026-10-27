"""Immutable season outlines with explicit confirmation and exact source snapshots."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "story_outlines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=True),
        sa.Column("base_version_id", sa.Uuid(), sa.ForeignKey("story_outlines.id"), nullable=True),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("content_json", json_type, nullable=False),
        sa.Column("source_snapshot", json_type, nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("confirmed_by", sa.String(100), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("save_key", sa.String(128), nullable=True),
        sa.Column("request_fingerprint", sa.String(64), nullable=True),
        sa.UniqueConstraint("run_id"),
        sa.UniqueConstraint("project_id", "version_no", name="uq_story_outlines_project_version"),
        sa.UniqueConstraint("project_id", "save_key", name="uq_story_outlines_project_save_key"),
        sa.CheckConstraint("version_no > 0", name="outline_version_positive"),
        sa.CheckConstraint("status IN ('DRAFT', 'APPROVED')", name="outline_status"),
        sa.CheckConstraint(
            "(status = 'DRAFT' AND confirmed_by IS NULL AND confirmed_at IS NULL) OR "
            "(status = 'APPROVED' AND confirmed_by IS NOT NULL AND confirmed_at IS NOT NULL)",
            name="outline_confirmation",
        ),
    )
    op.create_index("ix_story_outlines_project_id", "story_outlines", ["project_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Story outline downgrade requires an online data check")
    outlines = sa.table("story_outlines", sa.column("id", sa.Uuid()))
    runs = sa.table("workflow_runs", sa.column("workflow_type", sa.String()))
    db = op.get_bind()
    if (
        db.scalar(sa.select(outlines.c.id).limit(1)) is not None
        or db.scalar(
            sa.select(runs.c.workflow_type).where(runs.c.workflow_type == "story_outline").limit(1)
        )
        is not None
    ):
        raise RuntimeError("Story outline data exists; back up and migrate before downgrade")
    op.drop_index("ix_story_outlines_project_id", table_name="story_outlines")
    op.drop_table("story_outlines")
