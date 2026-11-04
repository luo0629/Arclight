"""Add user-scoped Agent tool grants and idempotent invocation records.

Revision ID: 0044
Revises: 0043
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0044"
down_revision = "0043"
branch_labels = None
depends_on = None


def upgrade():
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.create_table(
        "agent_tool_grants",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("allowed_tools_json", json_type, nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "revision >= 0", name=op.f("ck_agent_tool_grants_agent_tool_grant_revision_nonnegative")
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_agent_tool_grants_project_id_projects"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name="fk_agent_tool_grants_run_id_workflow_runs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tool_grants"),
        sa.UniqueConstraint("run_id", "agent_name", name="uq_agent_tool_grant_run_agent"),
    )
    op.create_index("ix_agent_tool_grants_project_id", "agent_tool_grants", ["project_id"])
    op.create_index("ix_agent_tool_grants_run_id", "agent_tool_grants", ["run_id"])

    op.create_table(
        "agent_tool_invocations",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("tool_name", sa.String(64), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("feedback_json", json_type, nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('RUNNING', 'SUCCEEDED', 'FAILED')",
            name=op.f("ck_agent_tool_invocations_agent_tool_invocation_status"),
        ),
        sa.CheckConstraint(
            "(status = 'RUNNING' AND feedback_json IS NULL) OR "
            "(status IN ('SUCCEEDED', 'FAILED') AND feedback_json IS NOT NULL)",
            name=op.f("ck_agent_tool_invocations_agent_tool_invocation_feedback"),
        ),
        sa.ForeignKeyConstraint(
            ["project_id"], ["projects.id"], name="fk_agent_tool_invocations_project_id_projects"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["workflow_runs.id"], name="fk_agent_tool_invocations_run_id_workflow_runs"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_agent_tool_invocations"),
        sa.UniqueConstraint("run_id", "idempotency_key", name="uq_agent_tool_invocation_run_key"),
    )
    op.create_index(
        "ix_agent_tool_invocations_project_id", "agent_tool_invocations", ["project_id"]
    )
    op.create_index("ix_agent_tool_invocations_run_id", "agent_tool_invocations", ["run_id"])


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Agent tool history downgrade requires online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        connection.exec_driver_sql(
            "LOCK TABLE agent_tool_grants, agent_tool_invocations IN ACCESS EXCLUSIVE MODE"
        )
    elif connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.scalar(sa.text("SELECT 1 FROM agent_tool_invocations LIMIT 1")):
        raise RuntimeError("Agent tool invocation history exists; cannot discard it")
    if connection.scalar(sa.text("SELECT 1 FROM agent_tool_grants LIMIT 1")):
        raise RuntimeError("Agent tool grants exist; cannot discard authorization history")
    op.drop_index("ix_agent_tool_invocations_run_id", table_name="agent_tool_invocations")
    op.drop_index("ix_agent_tool_invocations_project_id", table_name="agent_tool_invocations")
    op.drop_table("agent_tool_invocations")
    op.drop_index("ix_agent_tool_grants_run_id", table_name="agent_tool_grants")
    op.drop_index("ix_agent_tool_grants_project_id", table_name="agent_tool_grants")
    op.drop_table("agent_tool_grants")
