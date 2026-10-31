"""Durable media admission, retry policy, dead letters and replay lineage."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects import postgresql

revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None

TABLES = (
    "generation_schedules",
    "generation_calls",
    "generation_dead_letters",
    "generation_replays",
)


def entity():
    return [
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    ]


def fk(name, target, nullable=False):
    return sa.Column(name, sa.Uuid(), sa.ForeignKey(target), nullable=nullable)


def upgrade() -> None:
    json_type = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
    op.create_table(
        "generation_scheduler_gate",
        sa.Column("id", sa.String(16), primary_key=True),
        sa.CheckConstraint("id = 'global'", name="generation_scheduler_singleton"),
    )
    op.execute("INSERT INTO generation_scheduler_gate (id) VALUES ('global')")
    op.create_table(
        "generation_schedules",
        *entity(),
        fk("job_id", "generation_jobs.id"),
        sa.Column("policy_json", json_type, nullable=False),
        sa.Column("deadline_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("blocked_code", sa.String(64)),
        sa.UniqueConstraint("job_id"),
    )
    op.create_index(
        "ix_generation_schedules_next_attempt_at", "generation_schedules", ["next_attempt_at"]
    )
    op.create_table(
        "generation_calls",
        *entity(),
        fk("job_id", "generation_jobs.id"),
        fk("attempt_id", "generation_attempts.id"),
        fk("project_id", "projects.id"),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("released_at", sa.DateTime(timezone=True)),
        sa.Column("outcome", sa.String(64)),
        sa.CheckConstraint("kind IN ('SUBMIT','QUERY','CANCEL')", name="generation_call_kind"),
        sa.CheckConstraint(
            "(completed_at IS NULL AND outcome IS NULL) OR "
            "(completed_at IS NOT NULL AND outcome IS NOT NULL)",
            name="generation_call_outcome",
        ),
    )
    for column in ("job_id", "started_at"):
        op.create_index(f"ix_generation_calls_{column}", "generation_calls", [column])
    op.create_index(
        "ix_generation_calls_provider_active",
        "generation_calls",
        ["provider", "kind", "released_at"],
    )
    op.create_index(
        "ix_generation_calls_actor_started", "generation_calls", ["actor", "started_at"]
    )
    op.create_table(
        "generation_dead_letters",
        *entity(),
        fk("job_id", "generation_jobs.id"),
        fk("attempt_id", "generation_attempts.id", True),
        sa.Column("reason", sa.String(32), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("diagnostic_json", json_type, nullable=False),
        sa.UniqueConstraint("job_id"),
        sa.CheckConstraint(
            "reason IN ('ATTEMPT_LIMIT','TIME_LIMIT','NOT_RETRYABLE','RESULT_UNKNOWN')",
            name="generation_dead_letter_reason",
        ),
    )
    op.create_table(
        "generation_replays",
        *entity(),
        fk("project_id", "projects.id"),
        fk("source_job_id", "generation_jobs.id"),
        fk("dead_letter_id", "generation_dead_letters.id"),
        fk("new_job_id", "generation_jobs.id"),
        sa.Column("creation_key", sa.String(160), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.UniqueConstraint("new_job_id"),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_generation_replay_key"),
    )
    op.create_index("ix_generation_replays_source_job_id", "generation_replays", ["source_job_id"])
    sqlite = op.get_bind().dialect.name == "sqlite"
    operator = "IS NOT" if sqlite else "IS DISTINCT FROM"

    def changed(names):
        return " OR ".join(f'OLD."{name}" {operator} NEW."{name}"' for name in names.split())

    call_identity = "id created_at job_id attempt_id project_id provider actor kind started_at"
    guards = {
        "generation_scheduler_gate": ("TRUE", "id = NEW.id"),
        "generation_schedules": (
            changed("id created_at job_id policy_json deadline_at"),
            "id = NEW.id OR job_id = NEW.job_id",
        ),
        "generation_calls": (
            f"({changed(call_identity)}) "
            f"OR (OLD.completed_at IS NOT NULL AND ({changed('completed_at outcome')})) "
            f"OR (OLD.released_at IS NOT NULL AND ({changed('released_at')}))",
            "id = NEW.id",
        ),
        "generation_dead_letters": ("TRUE", "id = NEW.id OR job_id = NEW.job_id"),
        "generation_replays": (
            "TRUE",
            "id = NEW.id OR new_job_id = NEW.new_job_id OR "
            "(project_id = NEW.project_id AND creation_key = NEW.creation_key)",
        ),
    }
    for table, (condition, duplicate) in guards.items():
        name = f"immutable_{table}"
        if sqlite:
            op.execute(
                f"CREATE TRIGGER {name}_update BEFORE UPDATE ON {table} WHEN {condition} "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END"
            )
            op.execute(
                f"CREATE TRIGGER {name}_delete BEFORE DELETE ON {table} "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table}'); END"
            )
            op.execute(
                f"CREATE TRIGGER {name}_insert BEFORE INSERT ON {table} "
                f"WHEN EXISTS (SELECT 1 FROM {table} WHERE {duplicate}) "
                f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {table} replacement'); END"
            )
        else:
            op.execute(f"""CREATE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN
                    IF TG_OP IN ('DELETE','TRUNCATE') THEN
                        RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                    END IF;
                    IF {condition} THEN
                        RAISE EXCEPTION 'IMMUTABLE_HISTORY: {table}' USING ERRCODE = '23514';
                    END IF;
                    RETURN NEW;
                END; $$""")
            op.execute(
                f"CREATE TRIGGER {name}_write BEFORE UPDATE OR DELETE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {name}_guard()"
            )
            op.execute(
                f"CREATE TRIGGER {name}_truncate BEFORE TRUNCATE ON {table} "
                f"FOR EACH STATEMENT EXECUTE FUNCTION {name}_guard()"
            )


def downgrade() -> None:
    if context.is_offline_mode():
        raise RuntimeError("Generation scheduling downgrade requires an online preservation check")
    connection = op.get_bind()
    if connection.dialect.name == "postgresql":
        op.execute(f"LOCK TABLE {', '.join(TABLES)} IN ACCESS EXCLUSIVE MODE")
    else:
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")
    for table in TABLES:
        if connection.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")):
            raise RuntimeError(
                "Generation history exists; preserve scheduling diagnostics and lineage"
            )
    for table in (*reversed(TABLES), "generation_scheduler_gate"):
        op.drop_table(table)
        if connection.dialect.name == "postgresql":
            op.execute(f"DROP FUNCTION immutable_{table}_guard()")
