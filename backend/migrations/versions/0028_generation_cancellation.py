"""Persist cancellation intent separately from a confirmed provider outcome."""

import sqlalchemy as sa
from alembic import context, op

revision = "0028"
down_revision = "0027"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_jobs", sa.Column("cancel_requested_at", sa.DateTime(timezone=True)))


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Cancellation downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "sqlite":
        if not connection.connection.driver_connection.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    else:
        connection.exec_driver_sql("LOCK TABLE generation_jobs IN ACCESS EXCLUSIVE MODE")
    if connection.scalar(
        sa.text("SELECT 1 FROM generation_jobs WHERE cancel_requested_at IS NOT NULL LIMIT 1")
    ):
        raise RuntimeError("Generation cancellation history exists; preserve cancellation intent")
    if connection.scalar(sa.text("SELECT 1 FROM generation_jobs LIMIT 1")):
        raise RuntimeError("Generation history exists; preserve media job schema")
    with op.batch_alter_table("generation_jobs") as batch:
        batch.drop_column("cancel_requested_at")
