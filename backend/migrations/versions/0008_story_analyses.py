"""Preserve immutable story extractions and their exact source identity."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_analyses",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("story_sources.id"), nullable=True),
        sa.Column("source_name", sa.String(160), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=True),
        sa.Column("story_sha256", sa.String(64), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column(
            "content_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False
        ),
        sa.UniqueConstraint("run_id"),
        sa.UniqueConstraint("episode_id", "version_no"),
        sa.CheckConstraint("version_no > 0", name="analysis_version_positive"),
    )
    op.create_index("ix_story_analyses_episode_id", "story_analyses", ["episode_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Story analysis downgrade requires an online data check")
    analyses = sa.table("story_analyses", sa.column("id", sa.Uuid()))
    runs = sa.table("workflow_runs", sa.column("workflow_type", sa.String()))
    connection = op.get_bind()
    if (
        connection.scalar(sa.select(analyses.c.id).limit(1)) is not None
        or connection.scalar(
            sa.select(runs.c.workflow_type).where(runs.c.workflow_type == "story_analysis").limit(1)
        )
        is not None
    ):
        raise RuntimeError("Story analysis data exists; back up and migrate before downgrade")
    op.drop_index("ix_story_analyses_episode_id", table_name="story_analyses")
    op.drop_table("story_analyses")
