"""Persist versioned Agent run state separately from the public run contract.

Revision ID: 0043
Revises: 0042
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0043"
down_revision = "0042"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    if context.is_offline_mode():
        op.add_column("workflow_runs", sa.Column("agent_state_json", json_type, nullable=True))
        return
    columns = {column["name"] for column in sa.inspect(op.get_bind()).get_columns("workflow_runs")}
    if "agent_state_json" not in columns:
        op.add_column("workflow_runs", sa.Column("agent_state_json", json_type, nullable=True))


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Agent runtime state downgrade requires online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql("LOCK TABLE workflow_runs IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    non_null_state = (
        "agent_state_json IS NOT NULL AND agent_state_json <> 'null'::jsonb"
        if connection.dialect.name == "postgresql"
        else "agent_state_json IS NOT NULL AND agent_state_json <> 'null'"
    )
    if connection.scalar(sa.text(f"SELECT 1 FROM workflow_runs WHERE {non_null_state} LIMIT 1")):
        raise RuntimeError("Agent runtime state exists; cannot discard resumable state")
    triggers = []
    if connection.dialect.name == "sqlite":
        triggers = list(
            connection.scalars(
                sa.text(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' "
                    "AND tbl_name='workflow_runs' AND sql IS NOT NULL"
                )
            )
        )
        with op.batch_alter_table("workflow_runs") as batch:
            batch.drop_column("agent_state_json")
        for statement in triggers:
            op.execute(statement)
    else:
        op.drop_column("workflow_runs", "agent_state_json")
