"""Fenced asynchronous render executions and preserved attempt/cleanup history."""

import sqlalchemy as sa
from alembic import context, op

revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None
TABLES = ("render_jobs", "render_attempts")


def common():
    return (
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )


def upgrade():
    op.create_table(
        "render_jobs",
        sa.Column("id", sa.Uuid(), sa.ForeignKey("render_executions.id"), primary_key=True),
        *common(),
        sa.Column("manifest_id", sa.Uuid(), sa.ForeignKey("render_manifests.id"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("project_id", sa.Uuid(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("creation_key", sa.Uuid(), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.Uuid()),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("started_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("project_id", "creation_key", name="uq_render_job_creation"),
        sa.CheckConstraint(
            "revision > 0 AND attempt_count BETWEEN 0 AND 3", name="render_job_count"
        ),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="render_job_progress"),
    )
    for field in ("project_id", "manifest_id", "lease_expires_at"):
        op.create_index(f"ix_render_jobs_{field}", "render_jobs", [field])
    op.create_table(
        "render_attempts",
        sa.Column("id", sa.Uuid(), primary_key=True),
        *common(),
        sa.Column("run_id", sa.Uuid(), sa.ForeignKey("render_jobs.id"), nullable=False),
        sa.Column("attempt_no", sa.Integer(), nullable=False),
        sa.Column("output_key", sa.String(512), nullable=False, unique=True),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("phase", sa.String(32), nullable=False),
        sa.Column("input_sha256", sa.String(64)),
        sa.Column("error_code", sa.String(64)),
        sa.Column("cleanup_status", sa.String(16), nullable=False),
        sa.Column("cleanup_error_code", sa.String(64)),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("run_id", "attempt_no", name="uq_render_attempt_number"),
        sa.CheckConstraint("attempt_no BETWEEN 1 AND 3", name="render_attempt_number"),
        sa.CheckConstraint("progress BETWEEN 0 AND 100", name="render_attempt_progress"),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED','CANCELLED','INTERRUPTED')",
            name="render_attempt_status",
        ),
        sa.CheckConstraint(
            "cleanup_status IN ('PENDING','CLEANED','FAILED','RETAINED')",
            name="render_attempt_cleanup",
        ),
    )
    op.create_index("ix_render_attempts_run_id", "render_attempts", ["run_id"])
    scope = {
        "render_jobs": (
            "NOT EXISTS (SELECT 1 FROM render_executions r "
            "JOIN render_manifests m ON m.project_id=r.project_id "
            "JOIN stored_files f ON f.id=r.input_file_id AND f.project_id=r.project_id "
            "WHERE r.id=NEW.id AND r.project_id=NEW.project_id "
            "AND m.id=NEW.manifest_id AND m.content_sha256=NEW.manifest_sha256 "
            "AND (r.retry_of_id IS NULL OR EXISTS (SELECT 1 FROM render_jobs p "
            "WHERE p.id=r.retry_of_id AND p.manifest_id=NEW.manifest_id)))"
        ),
        "render_attempts": (
            "NOT EXISTS (SELECT 1 FROM render_jobs j JOIN render_executions r ON r.id=j.id "
            "WHERE j.id=NEW.run_id AND r.status='RUNNING' AND j.lease_token=NEW.id "
            "AND j.attempt_count=NEW.attempt_no AND "
            "replace(NEW.output_key,'-','')='rendered/' || "
            "replace(CAST(j.project_id AS TEXT),'-','') || '/' || "
            "replace(CAST(NEW.id AS TEXT),'-',''))"
        ),
    }
    frozen = {
        "render_jobs": (
            "id,created_at,manifest_id,manifest_sha256,project_id,creation_key,request_sha256"
        ),
        "render_attempts": "id,created_at,run_id,attempt_no,output_key",
    }
    sqlite = op.get_bind().dialect.name == "sqlite"
    if not sqlite:
        op.execute(
            "CREATE FUNCTION render_job_history_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: render job history'; END; $$"
        )
    for table in TABLES:
        if sqlite:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_update BEFORE UPDATE OF {frozen[table]} "
                f"ON {table} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render job input'); END"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_delete BEFORE DELETE ON {table} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render job history'); END"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"WHEN {scope[table]} OR EXISTS (SELECT 1 FROM {table} WHERE id=NEW.id) "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render job scope'); END"
            )
        else:
            op.execute(
                f"CREATE TRIGGER immutable_{table}_write "
                f"BEFORE UPDATE OF {frozen[table]} OR DELETE "
                f"ON {table} FOR EACH ROW EXECUTE FUNCTION render_job_history_guard()"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_truncate BEFORE TRUNCATE ON {table} "
                "FOR EACH STATEMENT EXECUTE FUNCTION render_job_history_guard()"
            )
            op.execute(
                f"CREATE FUNCTION {table}_scope_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF {scope[table]} THEN "
                "RAISE EXCEPTION 'IMMUTABLE_HISTORY: render job scope'; "
                "END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER immutable_{table}_insert BEFORE INSERT ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {table}_scope_guard()"
            )
    # A persisted admission fingerprint and a finished attempt cannot be rewritten.
    condition = (
        "(OLD.input_sha256 IS NOT NULL AND NEW.input_sha256 IS DISTINCT FROM OLD.input_sha256) "
        "OR (OLD.status <> 'RUNNING' AND (NEW.status IS DISTINCT FROM OLD.status "
        "OR NEW.progress IS DISTINCT FROM OLD.progress OR NEW.phase IS DISTINCT FROM OLD.phase "
        "OR NEW.error_code IS DISTINCT FROM OLD.error_code "
        "OR NEW.finished_at IS DISTINCT FROM OLD.finished_at))"
    )
    if sqlite:
        op.execute(
            "CREATE TRIGGER render_attempts_terminal BEFORE UPDATE ON render_attempts "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render attempt'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION render_attempts_terminal_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF {condition} THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: render attempt'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER render_attempts_terminal BEFORE UPDATE ON render_attempts "
            "FOR EACH ROW EXECUTE FUNCTION render_attempts_terminal_guard()"
        )
    # Protect the shared execution ledger only for manifest jobs. Historical
    # single-image preview rows retain their existing lifecycle contract.
    fixed = (
        "id",
        "created_at",
        "project_id",
        "input_file_id",
        "retry_of_id",
        "created_by",
        "storage_identity",
        "intermediate_key",
    )
    terminal = ("status", "output_file_id", "error_code", "finished_at")

    def changed(fields):
        return " OR ".join(f"NEW.{field} IS DISTINCT FROM OLD.{field}" for field in fields)

    execution_condition = (
        "EXISTS (SELECT 1 FROM render_jobs j WHERE j.id=OLD.id) AND ("
        f"{changed(fixed)} OR "
        f"(OLD.status IN ('SUCCEEDED','FAILED','CANCELLED') AND ({changed(terminal)})) OR "
        "(OLD.status='QUEUED' AND NEW.status NOT IN ('QUEUED','RUNNING','CANCELLED')) OR "
        "(OLD.status='RUNNING' AND NEW.status='QUEUED') OR "
        "(NEW.status<>'SUCCEEDED' AND NEW.output_file_id IS NOT NULL) OR "
        "(NEW.status='SUCCEEDED' AND NOT EXISTS (SELECT 1 FROM stored_files f "
        "JOIN render_attempts a ON a.run_id=NEW.id AND a.output_key=f.object_key "
        "WHERE f.id=NEW.output_file_id AND f.project_id=NEW.project_id "
        "AND f.media_type='video/mp4')) OR "
        "(NEW.status IN ('SUCCEEDED','FAILED','CANCELLED') AND NEW.finished_at IS NULL))"
    )
    job_terminal = (
        "OLD.phase IN ('SUCCEEDED','FAILED','CANCELLED') AND ("
        + changed(
            (
                "revision",
                "attempt_count",
                "progress",
                "phase",
                "cancel_requested_at",
                "lease_token",
                "lease_expires_at",
                "started_at",
            )
        )
        + ")"
    )
    for name, table, predicate in (
        ("render_execution_history", "render_executions", execution_condition),
        ("render_job_terminal", "render_jobs", job_terminal),
    ):
        if sqlite:
            op.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} WHEN {predicate} "
                "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render execution'); END"
            )
        else:
            op.execute(
                f"CREATE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
                f"BEGIN IF {predicate} THEN "
                "RAISE EXCEPTION 'IMMUTABLE_HISTORY: render execution'; "
                "END IF; RETURN NEW; END; $$"
            )
            op.execute(
                f"CREATE TRIGGER {name} BEFORE UPDATE ON {table} "
                f"FOR EACH ROW EXECUTE FUNCTION {name}_guard()"
            )
    if sqlite:
        op.execute(
            "CREATE TRIGGER render_execution_replace BEFORE INSERT ON render_executions "
            "WHEN EXISTS (SELECT 1 FROM render_jobs WHERE id=NEW.id) "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render execution'); END"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Render job downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE render_jobs, render_attempts IN ACCESS EXCLUSIVE MODE")
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if any(db.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in TABLES):
        raise RuntimeError("Cannot discard render job history")
    sqlite = db.dialect.name == "sqlite"
    if sqlite:
        op.execute("DROP TRIGGER render_execution_replace")
    for name, table in (
        ("render_execution_history", "render_executions"),
        ("render_job_terminal", "render_jobs"),
    ):
        op.execute(f"DROP TRIGGER {name}" + (f" ON {table}" if not sqlite else ""))
        if not sqlite:
            op.execute(f"DROP FUNCTION {name}_guard()")
    for table in reversed(TABLES):
        op.drop_table(table)
    if db.dialect.name == "postgresql":
        for table in TABLES:
            op.execute(f"DROP FUNCTION {table}_scope_guard()")
        op.execute("DROP FUNCTION render_attempts_terminal_guard()")
        op.execute("DROP FUNCTION render_job_history_guard()")
