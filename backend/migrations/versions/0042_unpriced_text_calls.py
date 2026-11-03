"""Allow text calls without a server-side quotation.

Revision ID: 0042
Revises: 0041
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0042"
down_revision = "0041"
branch_labels = None
depends_on = None


def alter_maximum_cost(*, nullable: bool) -> None:
    with op.batch_alter_table("model_gateway_calls") as batch:
        batch.alter_column("maximum_cost", existing_type=sa.Numeric(18, 6), nullable=nullable)
    if op.get_bind().dialect.name == "sqlite":
        # SQLite recreates the table to change nullability, dropping its guards.
        # Receipt history and foreign keys continue to refer to the same table.
        for action in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_model_gateway_calls_{action.lower()} "
                f"BEFORE {action} ON model_gateway_calls BEGIN SELECT RAISE(ABORT, "
                "'IMMUTABLE_HISTORY: model gateway'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_model_gateway_calls_replace "
            "BEFORE INSERT ON model_gateway_calls WHEN EXISTS "
            "(SELECT 1 FROM model_gateway_calls WHERE id=NEW.id OR "
            "(project_id=NEW.project_id AND operation_key=NEW.operation_key "
            "AND sequence=NEW.sequence)) BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: model gateway'); END"
        )


def upgrade():
    alter_maximum_cost(nullable=True)


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Unpriced call downgrade requires online history preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE model_gateway_calls IN ACCESS EXCLUSIVE MODE")
    if connection.scalar(
        sa.text("SELECT 1 FROM model_gateway_calls WHERE maximum_cost IS NULL LIMIT 1")
    ):
        raise RuntimeError("Unpriced model gateway history exists; cannot restore mandatory quotes")
    alter_maximum_cost(nullable=False)
