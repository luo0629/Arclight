"""Version imported and inline sources without rewriting existing artifacts or runs."""

import sqlalchemy as sa
from alembic import context, op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "story_source_revisions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("story_sources.id"), nullable=False),
        sa.Column("root_source_id", sa.Uuid(), sa.ForeignKey("story_sources.id"), nullable=False),
        sa.Column("parent_source_id", sa.Uuid(), sa.ForeignKey("story_sources.id")),
        sa.Column("version_no", sa.Integer(), nullable=False),
        sa.Column("change_note", sa.String(1000), nullable=False),
        sa.Column("origin_kind", sa.String(16), nullable=False),
        sa.Column("rights_status", sa.String(16), nullable=False),
        sa.Column("storage_backend", sa.String(16), nullable=False),
        sa.Column("object_key", sa.String(160), nullable=False),
        sa.UniqueConstraint("source_id"),
        sa.UniqueConstraint("object_key"),
        sa.UniqueConstraint("root_source_id", "version_no", name="uq_source_revision_root_version"),
        sa.UniqueConstraint("parent_source_id", name="uq_source_revision_parent"),
        sa.CheckConstraint(
            "(parent_source_id IS NULL AND source_id = root_source_id AND version_no = 1) OR "
            "(parent_source_id IS NOT NULL AND source_id <> root_source_id AND version_no > 1)",
            name="source_revision_shape",
        ),
        sa.CheckConstraint("origin_kind IN ('FILE', 'INLINE', 'TEXT')", name="source_origin_kind"),
        sa.CheckConstraint(
            "rights_status IN ('CONFIRMED', 'UNDECLARED')", name="source_rights_status"
        ),
        sa.CheckConstraint("storage_backend = 'database'", name="source_storage_backend"),
    )
    op.create_index(
        "ix_story_source_revisions_root_source_id", "story_source_revisions", ["root_source_id"]
    )
    sources = sa.table(
        "story_sources",
        sa.column("id", sa.Uuid()),
        sa.column("created_at"),
        sa.column("updated_at"),
    )
    revisions = sa.table(
        "story_source_revisions",
        sa.column("id", sa.Uuid()),
        sa.column("created_at"),
        sa.column("updated_at"),
        sa.column("source_id", sa.Uuid()),
        sa.column("root_source_id", sa.Uuid()),
        sa.column("version_no"),
        sa.column("change_note"),
        sa.column("origin_kind"),
        sa.column("rights_status"),
        sa.column("storage_backend"),
        sa.column("object_key"),
    )
    op.execute(
        revisions.insert().from_select(
            list(revisions.c.keys()),
            sa.select(
                sources.c.id,
                sources.c.created_at,
                sources.c.updated_at,
                sources.c.id,
                sources.c.id,
                sa.literal(1),
                sa.literal("既有文件导入"),
                sa.literal("FILE"),
                sa.literal("CONFIRMED"),
                sa.literal("database"),
                sa.literal("story-sources/") + sa.cast(sources.c.id, sa.String()) + "/original",
            ),
        )
    )
    op.create_table(
        "story_source_bindings",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_id", sa.Uuid(), sa.ForeignKey("story_sources.id"), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.UniqueConstraint("run_id"),
    )
    op.create_index("ix_story_source_bindings_source_id", "story_source_bindings", ["source_id"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Source revision downgrade requires an online provenance check")
    db = op.get_bind()
    revisions = sa.table("story_source_revisions", sa.column("id"))
    bindings = sa.table("story_source_bindings", sa.column("id"))
    if (
        db.scalar(sa.select(bindings.c.id).limit(1)) is not None
        or db.scalar(sa.select(revisions.c.id).limit(1)) is not None
    ):
        raise RuntimeError("Source provenance exists; back up and migrate before downgrade")
    op.drop_index("ix_story_source_bindings_source_id", table_name="story_source_bindings")
    op.drop_table("story_source_bindings")
    op.drop_index("ix_story_source_revisions_root_source_id", table_name="story_source_revisions")
    op.drop_table("story_source_revisions")
