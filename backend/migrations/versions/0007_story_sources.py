"""Preserve immutable imported story text, bytes and provenance."""

import sqlalchemy as sa
from alembic import context, op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_sources",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id"), nullable=False),
        sa.Column("import_no", sa.Integer(), nullable=False),
        sa.Column("import_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("original_name", sa.String(160), nullable=False),
        sa.Column("source_type", sa.String(16), nullable=False),
        sa.Column("media_type", sa.String(64), nullable=False),
        sa.Column("encoding", sa.String(16), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("raw_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("source_note", sa.String(1000), nullable=False),
        sa.Column("rights_note", sa.String(1000), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint("episode_id", "import_key", name="uq_story_sources_episode_import_key"),
        sa.UniqueConstraint("episode_id", "import_no", name="uq_story_sources_episode_import_no"),
        sa.CheckConstraint("import_no > 0", name="source_import_positive"),
        sa.CheckConstraint("byte_size > 0 AND byte_size <= 262144", name="source_byte_size"),
        sa.CheckConstraint("source_type IN ('TXT', 'MARKDOWN')", name="source_type"),
    )
    op.create_index("ix_story_sources_episode_id", "story_sources", ["episode_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Story source downgrade requires an online source check")
    sources = sa.table("story_sources", sa.column("id", sa.Uuid()))
    if op.get_bind().scalar(sa.select(sources.c.id).limit(1)) is not None:
        raise RuntimeError("Story sources exist; back up and migrate before downgrade")
    op.drop_index("ix_story_sources_episode_id", table_name="story_sources")
    op.drop_table("story_sources")
