"""Freeze shared Agent limits and link actions to authoritative usage records.

Revision ID: 0045
Revises: 0044
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

TABLES = ("agent_run_limits", "agent_actions")


def upgrade():
    op.create_table(
        "agent_run_limits",
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("root_run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("parent_run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id")),
        sa.Column("actor_id", sa.String(100), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("depth", sa.Integer(), nullable=False),
        sa.Column(
            "limits_json",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("depth BETWEEN 0 AND 8", name="agent_limit_depth"),
        sa.CheckConstraint(
            "(depth = 0 AND parent_run_id IS NULL AND root_run_id = run_id) OR "
            "(depth > 0 AND parent_run_id IS NOT NULL AND root_run_id <> run_id)",
            name="agent_limit_parent",
        ),
    )
    for column in ("project_id", "root_run_id"):
        op.create_index(f"ix_agent_run_limits_{column}", "agent_run_limits", [column])
    op.create_table(
        "agent_actions",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("agent_run_limits.run_id"), nullable=False),
        sa.Column("root_run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("agent_name", sa.String(32), nullable=False),
        sa.Column("stage", sa.String(40), nullable=False),
        sa.Column("step_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("tool_name", sa.String(64)),
        sa.Column("gateway_call_id", sa.Uuid(), sa.ForeignKey("model_gateway_calls.id")),
        sa.Column("tool_invocation_id", sa.Uuid(), sa.ForeignKey("agent_tool_invocations.id")),
        sa.UniqueConstraint("gateway_call_id", name="uq_agent_action_gateway_call"),
        sa.UniqueConstraint("tool_invocation_id", name="uq_agent_action_tool_invocation"),
        sa.CheckConstraint("kind IN ('MODEL', 'TOOL')", name="agent_action_kind"),
        sa.CheckConstraint(
            "(kind = 'MODEL' AND tool_name IS NULL) OR (kind = 'TOOL' AND tool_name IS NOT NULL)",
            name="agent_action_tool",
        ),
        sa.CheckConstraint(
            "(gateway_call_id IS NOT NULL AND tool_invocation_id IS NULL) OR "
            "(gateway_call_id IS NULL AND tool_invocation_id IS NOT NULL AND kind = 'TOOL')",
            name="agent_action_source",
        ),
    )
    for column in ("run_id", "root_run_id"):
        op.create_index(f"ix_agent_actions_{column}", "agent_actions", [column])
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION immutable_agent_limit_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: agent limits'; END; $$"
        )
    for table in TABLES:
        if dialect == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} BEFORE {action} ON {table} "
                    "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: agent limits'); END"
                )
            identity = (
                "run_id = NEW.run_id"
                if table == "agent_run_limits"
                else "id = NEW.id OR gateway_call_id = NEW.gateway_call_id "
                "OR tool_invocation_id = NEW.tool_invocation_id"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {identity}) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: agent limits'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION immutable_agent_limit_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_agent_limit_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Agent limits downgrade requires online history preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute("LOCK TABLE agent_actions, agent_run_limits IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if any(connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Agent execution limit history exists; cannot discard it")
    op.drop_table("agent_actions")
    op.drop_table("agent_run_limits")
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_agent_limit_guard()")
