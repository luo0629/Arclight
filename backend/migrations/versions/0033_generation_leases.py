"""Fenced media execution leases; preserve existing dispatch and attempt history."""

import sqlalchemy as sa
from alembic import context, op

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "generation_leases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("job_id", sa.Uuid(), sa.ForeignKey("generation_jobs.id"), nullable=False),
        sa.Column("attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id"), nullable=False),
        sa.Column("token", sa.Uuid(), nullable=False),
        sa.Column("epoch", sa.Integer(), nullable=False),
        sa.Column("owner", sa.String(100), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("job_id"),
        sa.CheckConstraint("epoch > 0", name="generation_lease_epoch"),
        sa.CheckConstraint("expires_at > heartbeat_at", name="generation_lease_expiry"),
    )
    op.create_index("ix_generation_leases_expires_at", "generation_leases", ["expires_at"])


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation leases downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE generation_leases IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.scalar(sa.text("SELECT 1 FROM generation_leases LIMIT 1")):
        raise RuntimeError("Generation history exists; preserve execution leases")
    op.drop_table("generation_leases")
