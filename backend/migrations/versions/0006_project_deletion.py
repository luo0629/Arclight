"""Add recoverable project deletion tombstones, without deleting any dependencies."""

import sqlalchemy as sa
from alembic import context, op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("projects", sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("restore_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("projects", sa.Column("deletion_id", sa.Uuid(), nullable=True))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Project deletion downgrade requires an online tombstone check")
    projects = sa.table("projects", sa.column("deleted_at", sa.DateTime(timezone=True)))
    if (
        op.get_bind().scalar(
            sa.select(projects.c.deleted_at).where(projects.c.deleted_at.is_not(None)).limit(1)
        )
        is not None
    ):
        raise RuntimeError("Deleted projects exist; restore or migrate them before downgrade")
    # Native DROP COLUMN avoids recreating a referenced projects table on SQLite.
    op.drop_column("projects", "deletion_id")
    op.drop_column("projects", "restore_until")
    op.drop_column("projects", "deleted_at")
