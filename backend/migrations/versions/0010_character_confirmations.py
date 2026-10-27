"""Exact-source character identity confirmations; aliases remain in story entry JSON."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "character_confirmations",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("script_id", sa.Uuid(), sa.ForeignKey("scripts.id")),
        sa.Column("analysis_id", sa.Uuid(), sa.ForeignKey("story_analyses.id")),
        sa.Column("source_name", sa.String(100), nullable=False),
        sa.Column("entry_id", sa.Uuid(), sa.ForeignKey("story_entries.id"), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column(
            "snapshot_json",
            sa.JSON().with_variant(postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.UniqueConstraint(
            "script_id", "source_name", name="uq_character_confirmations_script_name"
        ),
        sa.UniqueConstraint(
            "analysis_id", "source_name", name="uq_character_confirmations_analysis_name"
        ),
        sa.CheckConstraint(
            "(script_id IS NOT NULL AND analysis_id IS NULL) OR "
            "(script_id IS NULL AND analysis_id IS NOT NULL)",
            name="character_confirmation_source",
        ),
    )
    op.create_index(
        "ix_character_confirmations_project_id", "character_confirmations", ["project_id"]
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Character confirmation downgrade requires an online data check")
    db = op.get_bind()
    confirmations = sa.table("character_confirmations", sa.column("id", sa.Uuid()))
    entries = sa.table("story_entries", sa.column("data_json", sa.JSON()))
    if db.scalar(sa.select(confirmations.c.id).limit(1)) is not None or any(
        value.get("aliases") for value in db.scalars(sa.select(entries.c.data_json))
    ):
        raise RuntimeError("Character identity data exists; back up and migrate before downgrade")
    op.drop_index("ix_character_confirmations_project_id", table_name="character_confirmations")
    op.drop_table("character_confirmations")
