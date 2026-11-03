"""Shared model call governance and immutable usage history.

Revision ID: 0040
Revises: 0039
"""

import sqlalchemy as sa
from alembic import context, op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def upgrade():
    op.create_table("model_gateway_gate", sa.Column("id", sa.String(16), primary_key=True))
    op.execute("INSERT INTO model_gateway_gate (id) VALUES ('global')")
    op.create_table(
        "model_gateway_circuits",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("failures", sa.Integer(), nullable=False),
        sa.Column("opened_until", sa.DateTime(timezone=True)),
        sa.Column("probe_until", sa.DateTime(timezone=True)),
        sa.Column("probe_call_id", sa.Uuid()),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
    )
    op.create_index(
        "ix_model_gateway_circuits_project_id", "model_gateway_circuits", ["project_id"]
    )
    json_type = sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql")
    op.create_table(
        "model_gateway_calls",
        *entity(),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("episode_id", sa.Uuid(), sa.ForeignKey("episodes.id")),
        sa.Column("workflow_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id")),
        sa.Column("generation_attempt_id", sa.Uuid(), sa.ForeignKey("generation_attempts.id")),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("operation_key", sa.String(200), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("task", sa.String(40), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column(
            "circuit_id", sa.String(64), sa.ForeignKey("model_gateway_circuits.id"), nullable=False
        ),
        sa.Column("route_json", json_type, nullable=False),
        sa.Column("parameters_json", json_type, nullable=False),
        sa.Column("maximum_cost", sa.Numeric(18, 6), nullable=False),
        sa.UniqueConstraint("project_id", "operation_key", "sequence"),
    )
    for column in ("project_id", "workflow_id", "actor"):
        op.create_index(f"ix_model_gateway_calls_{column}", "model_gateway_calls", [column])
    op.create_table(
        "model_gateway_receipts",
        *entity(),
        sa.Column("call_id", sa.Uuid(), sa.ForeignKey("model_gateway_calls.id"), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("usage_json", json_type, nullable=False),
        sa.Column("actual_cost", sa.Numeric(18, 6)),
        sa.Column("evidence", sa.String(1000)),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.UniqueConstraint("call_id", "kind"),
    )
    op.create_index("ix_model_gateway_receipts_call_id", "model_gateway_receipts", ["call_id"])
    dialect = op.get_bind().dialect.name
    if dialect == "postgresql":
        op.execute(
            "CREATE FUNCTION immutable_gateway_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: model gateway'; END; $$"
        )
    for table in ("model_gateway_calls", "model_gateway_receipts"):
        if dialect == "sqlite":
            for action in ("UPDATE", "DELETE"):
                op.execute(
                    f"CREATE TRIGGER immutable_{table}_{action.lower()} "
                    f"BEFORE {action} ON {table} BEGIN SELECT RAISE(ABORT, "
                    "'IMMUTABLE_HISTORY: model gateway'); END"
                )
            identity = (
                "id=NEW.id OR (project_id=NEW.project_id "
                "AND operation_key=NEW.operation_key AND sequence=NEW.sequence)"
                if table.endswith("calls")
                else "id=NEW.id OR (call_id=NEW.call_id AND kind=NEW.kind)"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {identity}) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: model gateway'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                "FOR EACH ROW EXECUTE FUNCTION immutable_gateway_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION immutable_gateway_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Gateway downgrade requires online history preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(
            "LOCK TABLE model_gateway_calls, model_gateway_receipts IN ACCESS EXCLUSIVE MODE"
        )
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    if connection.scalar(sa.text("SELECT 1 FROM model_gateway_calls LIMIT 1")):
        if connection.scalar(
            sa.text(
                "SELECT 1 FROM model_gateway_calls WHERE generation_attempt_id IS NOT NULL LIMIT 1"
            )
        ):
            raise RuntimeError("Generation history exists; model gateway usage cannot be discarded")
        raise RuntimeError("Model gateway history exists; cannot discard usage facts")
    for table in (
        "model_gateway_receipts",
        "model_gateway_calls",
        "model_gateway_circuits",
        "model_gateway_gate",
    ):
        op.drop_table(table)
    if connection.dialect.name == "postgresql":
        op.execute("DROP FUNCTION immutable_gateway_guard()")
