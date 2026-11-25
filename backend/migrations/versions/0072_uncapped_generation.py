"""Allow execution without a deadline or a cost estimate."""

import sqlalchemy as sa
from alembic import context, op

revision = "0072"
down_revision = "0071"
branch_labels = None
depends_on = None


def _triggers(table: str) -> list[str]:
    connection = op.get_bind()
    if connection.dialect.name != "sqlite":
        return []
    return list(
        connection.scalars(
            sa.text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table"),
            {"table": table},
        )
    )


def _apply(*, uncapped: bool) -> None:
    controls = "agent_run_limits"
    reservations = "generation_budget_reservations"
    schedules = "generation_schedules"
    triggers = _triggers(controls) + _triggers(reservations) + _triggers(schedules)
    with op.batch_alter_table(controls) as batch:
        batch.alter_column(
            "deadline_at", existing_type=sa.DateTime(timezone=True), nullable=uncapped
        )
        batch.drop_constraint("agent_limit_depth", type_="check")
        batch.create_check_constraint(
            "agent_limit_depth", "depth >= 0" if uncapped else "depth BETWEEN 0 AND 8"
        )
    with op.batch_alter_table(reservations) as batch:
        batch.alter_column("amount", existing_type=sa.Numeric(18, 6), nullable=uncapped)
    with op.batch_alter_table(schedules) as batch:
        batch.alter_column(
            "deadline_at", existing_type=sa.DateTime(timezone=True), nullable=uncapped
        )
    # SQLite recreates the tables and drops their attached history guards.
    for trigger in triggers:
        op.get_bind().exec_driver_sql(trigger)


def upgrade() -> None:
    _apply(uncapped=True)
    op.create_table(
        "workflow_recovery_counters",
        sa.Column("event_id", sa.Uuid(), sa.ForeignKey("outbox_events.id"), primary_key=True),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.CheckConstraint("failure_count > 0", name="workflow_recovery_failure_count"),
    )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Downgrade requires an online check of execution and billing history")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE agent_run_limits, generation_budget_reservations, generation_schedules, "
            "workflow_recovery_counters IN ACCESS EXCLUSIVE MODE"
        )
    if (
        connection.scalar(
            sa.text("SELECT 1 FROM agent_run_limits WHERE deadline_at IS NULL OR depth > 8 LIMIT 1")
        )
        or connection.scalar(
            sa.text("SELECT 1 FROM generation_budget_reservations WHERE amount IS NULL LIMIT 1")
        )
        or connection.scalar(
            sa.text("SELECT 1 FROM generation_schedules WHERE deadline_at IS NULL LIMIT 1")
        )
        or connection.scalar(sa.text("SELECT 1 FROM workflow_recovery_counters LIMIT 1"))
    ):
        raise RuntimeError("Cannot downgrade without discarding execution or billing history")
    op.drop_table("workflow_recovery_counters")
    _apply(uncapped=False)
