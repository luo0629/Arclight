"""Project-scoped narrative identities, typed links and optimistic revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "story_entries",
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("name", sa.String(100), nullable=False),
        sa.Column("data_json", sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=False),
        sa.Column("archived", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("revision >= 0", name="story_revision_nonnegative"),
        sa.CheckConstraint(
            "kind IN ('WORLD', 'CHARACTER', 'LOCATION', 'PROP', 'SCENE')", name="story_kind"
        ),
    )
    op.create_index("ix_story_entries_project_id", "story_entries", ["project_id"])


def downgrade() -> None:
    if not context.is_offline_mode():
        entries = sa.table("story_entries", sa.column("id", sa.Uuid()))
        if op.get_bind().scalar(sa.select(entries.c.id).limit(1)) is not None:
            raise RuntimeError("Story entries exist; back up and migrate before downgrade")
    op.drop_index("ix_story_entries_project_id", table_name="story_entries")
    op.drop_table("story_entries")
