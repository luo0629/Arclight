"""Durable Agent threads, action intents and human decisions.

Revision ID: 0046
Revises: 0045
"""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None

TABLES = (
    "agent_loops",
    "agent_loop_actions",
    "agent_loop_questions",
    "agent_loop_decisions",
    "agent_loop_stamps",
)


def _entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def _json(name, nullable=False):
    return sa.Column(
        name, sa.JSON().with_variant(postgresql.JSONB(), "postgresql"), nullable=nullable
    )


def upgrade():
    op.create_table(
        "agent_loops",
        *_entity(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("workflow_runs.id"), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("actor_id", sa.String(100), nullable=False),
        *(
            sa.Column(name, sa.String(64), nullable=False)
            for name in (
                "operation_sha256",
                "request_sha256",
                "input_sha256",
                "result_schema_sha256",
            )
        ),
        sa.Column("thread_id", sa.String(100), nullable=False),
        _json("seed_json"),
        sa.Column("status", sa.String(32), nullable=False),
        _json("outcome_json", True),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "operation_sha256"),
        sa.UniqueConstraint("thread_id"),
    )
    for name in ("run_id", "project_id"):
        op.create_index(f"ix_agent_loops_{name}", "agent_loops", [name])
    op.create_table(
        "agent_loop_actions",
        *_entity(),
        sa.Column("loop_id", sa.Uuid(), sa.ForeignKey("agent_loops.id"), nullable=False),
        sa.Column("action_key", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        _json("request_json"),
        sa.Column("gateway_call_id", sa.Uuid(), sa.ForeignKey("model_gateway_calls.id")),
        _json("result_json", True),
        sa.UniqueConstraint("loop_id", "action_key"),
    )
    op.create_table(
        "agent_loop_questions",
        *_entity(),
        sa.Column("loop_id", sa.Uuid(), sa.ForeignKey("agent_loops.id"), nullable=False),
        sa.Column("model_turn", sa.Integer(), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        _json("question_json"),
        sa.UniqueConstraint("loop_id", "model_turn"),
    )
    op.create_table(
        "agent_loop_decisions",
        *_entity(),
        sa.Column(
            "question_id", sa.Uuid(), sa.ForeignKey("agent_loop_questions.id"), nullable=False
        ),
        sa.Column("actor_id", sa.String(100), nullable=False),
        _json("decision_json"),
        sa.UniqueConstraint("question_id"),
    )
    op.create_table(
        "agent_loop_stamps",
        *_entity(),
        sa.Column("loop_id", sa.Uuid(), sa.ForeignKey("agent_loops.id"), nullable=False),
        sa.Column("state_sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("loop_id", "state_sha256"),
    )
    for table in ("agent_loop_actions", "agent_loop_questions", "agent_loop_stamps"):
        op.create_index(f"ix_{table}_loop_id", table, ["loop_id"])
    _guards()


def _guards():
    dialect = op.get_bind().dialect.name
    different = "IS DISTINCT FROM" if dialect == "postgresql" else "IS NOT"
    fixed = {
        "agent_loops": (
            "id",
            "created_at",
            "run_id",
            "project_id",
            "actor_id",
            "operation_sha256",
            "request_sha256",
            "input_sha256",
            "result_schema_sha256",
            "thread_id",
            "seed_json",
        ),
        "agent_loop_actions": (
            "id",
            "created_at",
            "loop_id",
            "action_key",
            "kind",
            "request_sha256",
            "request_json",
        ),
    }
    identities = {
        "agent_loops": "run_id = NEW.run_id AND operation_sha256 = NEW.operation_sha256 "
        "OR thread_id = NEW.thread_id",
        "agent_loop_actions": "loop_id = NEW.loop_id AND action_key = NEW.action_key",
        "agent_loop_questions": "loop_id = NEW.loop_id AND model_turn = NEW.model_turn",
        "agent_loop_decisions": "question_id = NEW.question_id",
        "agent_loop_stamps": "loop_id = NEW.loop_id AND state_sha256 = NEW.state_sha256",
    }
    for table in TABLES:
        change = (
            " OR ".join(f"NEW.{name} {different} OLD.{name}" for name in fixed.get(table, ()))
            or "TRUE"
        )
        if table == "agent_loop_actions":
            change += (
                " OR (OLD.result_json IS NOT NULL AND OLD.result_json <> 'null' AND ("
                f"NEW.result_json {different} OLD.result_json OR "
                f"NEW.gateway_call_id {different} OLD.gateway_call_id))"
            )
        if dialect == "sqlite":
            op.execute(
                f"CREATE TRIGGER immutable_{table}_update BEFORE UPDATE ON {table} WHEN {change} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: Agent loop'); END"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: Agent loop'); END"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_replace BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE id = NEW.id OR ({identities[table]})) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: Agent loop'); END"
            )
        else:
            op.execute(
                f"CREATE FUNCTION immutable_{table}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN IF TG_OP <> 'UPDATE' THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: Agent loop'; "
                f"ELSIF {change} THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: Agent loop'; "
                "END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION immutable_{table}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION immutable_{table}_guard()"
            )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Agent loop downgrade requires online history preservation check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE " + ", ".join(TABLES) + " IN ACCESS EXCLUSIVE MODE")
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Agent loop history exists; cannot discard it")
    for table in reversed(TABLES):
        op.drop_table(table)
        if db.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
