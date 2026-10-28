"""Explicit immutable visual direction snapshots; legacy styles remain intact."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = sa.JSON().with_variant(JSONB(), "postgresql")
    op.create_table(
        "visual_bible_versions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("version_no", sa.Integer(), nullable=False),
        *[
            sa.Column(name, sa.Text(), nullable=False)
            for name in (
                "art_style",
                "line_style",
                "render_style",
                "palette",
                "lighting",
                "composition",
                "prohibited",
                "negative_prompt",
            )
        ],
        sa.Column("locked_fields", json_type, nullable=False),
        sa.Column("legacy_snapshot", json_type, nullable=False),
        sa.Column("change_kind", sa.String(16), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.UniqueConstraint("project_id", "version_no", name="uq_visual_bible_version_no"),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_visual_bible_creation"),
        sa.CheckConstraint("version_no > 0", name="visual_bible_positive"),
    )
    op.create_index("ix_visual_bible_versions_project_id", "visual_bible_versions", ["project_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Visual Bible downgrade requires an online preservation check")
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM visual_bible_versions")):
        raise RuntimeError("Visual Bible versions exist; preserve history before downgrade")
    op.drop_index("ix_visual_bible_versions_project_id", table_name="visual_bible_versions")
    op.drop_table("visual_bible_versions")
