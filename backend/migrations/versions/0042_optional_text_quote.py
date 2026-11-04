"""Allow unpriced text calls without inventing a zero-cost quote.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def alter_quote(nullable: bool) -> None:
    connection = op.get_bind()
    triggers = []
    if connection.dialect.name == "sqlite":
        triggers = list(
            connection.scalars(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='model_gateway_calls' AND sql IS NOT NULL"
                )
            )
        )
    with op.batch_alter_table("model_gateway_calls") as batch:
        batch.alter_column("maximum_cost", existing_type=sa.Numeric(18, 6), nullable=nullable)
    # SQLite rebuilds this table for nullability changes. Preserve all guards.
    for statement in triggers:
        op.execute(statement)


def upgrade():
    alter_quote(True)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Text quote downgrade requires online history preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE model_gateway_calls IN ACCESS EXCLUSIVE MODE")
    if connection.scalar(
        sa.text("SELECT 1 FROM model_gateway_calls WHERE maximum_cost IS NULL LIMIT 1")
    ):
        raise RuntimeError("Unpriced text history exists; cannot replace unknown costs")
    alter_quote(False)
