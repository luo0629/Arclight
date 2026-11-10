"""Append-only technical checks of fenced, exact render candidates."""

import sqlalchemy as sa
from alembic import context, op

revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None


def result_matches():
    names = (
        "'decoding','tracks','resolution','frame_rate','video_codec',"
        "'pixel_format','audio_codec','audio_format','duration'"
    )
    if op.get_bind().dialect.name == "sqlite":
        return (
            "json_extract(NEW.result_json,'$.schema_version')=1 "
            "AND json_extract(NEW.result_json,'$.checker')='arclight-ffmpeg-decode-v1' "
            "AND json_extract(NEW.result_json,'$.output_sha256')=NEW.output_sha256 "
            "AND json_extract(NEW.result_json,'$.byte_size')=NEW.byte_size "
            "AND json_type(NEW.result_json,'$.checks')='array' "
            "AND json_array_length(NEW.result_json,'$.checks')=9 "
            "AND (SELECT count(DISTINCT json_extract(c.value,'$.name')) "
            "FROM json_each(NEW.result_json,'$.checks') c)=9 "
            "AND NOT EXISTS (SELECT 1 FROM json_each(NEW.result_json,'$.checks') c "
            f"WHERE json_extract(c.value,'$.name') NOT IN ({names}) "
            "OR coalesce(json_type(c.value,'$.passed'),'null') NOT IN ('true','false')) "
            "AND NEW.passed=(NOT EXISTS (SELECT 1 "
            "FROM json_each(NEW.result_json,'$.checks') c "
            "WHERE json_extract(c.value,'$.passed')=0)) "
            "AND (NOT NEW.passed OR (json_type(NEW.result_json,'$.measurement')='object' "
            "AND json_type(NEW.result_json,'$.failure')='null'))"
        )
    return (
        "NEW.result_json->>'schema_version'='1' "
        "AND NEW.result_json->>'checker'='arclight-ffmpeg-decode-v1' "
        "AND NEW.result_json->>'output_sha256'=NEW.output_sha256 "
        "AND NEW.result_json->>'byte_size'=NEW.byte_size::text "
        "AND jsonb_typeof(NEW.result_json->'checks')='array' "
        "AND jsonb_array_length(NEW.result_json->'checks')=9 "
        "AND (SELECT count(DISTINCT c->>'name') "
        "FROM jsonb_array_elements(NEW.result_json->'checks') c)=9 "
        "AND NOT EXISTS (SELECT 1 FROM jsonb_array_elements(NEW.result_json->'checks') c "
        f"WHERE c->>'name' NOT IN ({names}) "
        "OR coalesce(c->'passed','null'::jsonb) NOT IN ('true'::jsonb,'false'::jsonb)) "
        "AND NEW.passed=(NOT EXISTS (SELECT 1 "
        "FROM jsonb_array_elements(NEW.result_json->'checks') c "
        "WHERE c->'passed'='false'::jsonb)) "
        "AND (NOT NEW.passed OR (jsonb_typeof(NEW.result_json->'measurement')='object' "
        "AND NEW.result_json->'failure'='null'::jsonb))"
    )


def upgrade():
    op.create_table(
        "render_inspections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "attempt_id",
            sa.Uuid(),
            sa.ForeignKey("render_attempts.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column("manifest_id", sa.Uuid(), sa.ForeignKey("render_manifests.id"), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("input_sha256", sa.String(64), nullable=False),
        sa.Column("output_sha256", sa.String(64), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column(
            "result_json",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "byte_size > 0 AND byte_size <= 134217728", name="render_inspection_size"
        ),
    )
    op.create_index("ix_render_inspections_manifest_id", "render_inspections", ["manifest_id"])
    live_lease = (
        "julianday(j.lease_expires_at)>julianday('now')"
        if op.get_bind().dialect.name == "sqlite"
        else "j.lease_expires_at>clock_timestamp()"
    )
    condition = (
        "NOT EXISTS (SELECT 1 FROM render_attempts a JOIN render_jobs j ON j.id=a.run_id "
        "JOIN render_executions r ON r.id=j.id WHERE a.id=NEW.attempt_id "
        "AND a.status='RUNNING' AND r.status='RUNNING' AND j.lease_token=a.id "
        f"AND j.cancel_requested_at IS NULL AND {live_lease} "
        "AND a.input_sha256=NEW.input_sha256 AND j.manifest_id=NEW.manifest_id "
        "AND j.manifest_sha256=NEW.manifest_sha256) "
        "OR EXISTS (SELECT 1 FROM render_inspections WHERE id=NEW.id OR attempt_id=NEW.attempt_id) "
        f"OR NOT coalesce(({result_matches()}), false)"
    )
    if op.get_bind().dialect.name == "sqlite":
        for event in ("UPDATE", "DELETE"):
            op.execute(
                f"CREATE TRIGGER immutable_render_inspections_{event.lower()} BEFORE {event} "
                "ON render_inspections BEGIN SELECT RAISE(ABORT, "
                "'IMMUTABLE_HISTORY: render inspection'); END"
            )
        op.execute(
            "CREATE TRIGGER immutable_render_inspections_insert BEFORE INSERT "
            f"ON render_inspections WHEN {condition} "
            "BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: render inspection scope'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION render_inspection_history_guard() "
            "RETURNS trigger LANGUAGE plpgsql AS $$ "
            "BEGIN RAISE EXCEPTION 'IMMUTABLE_HISTORY: render inspection'; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_render_inspections_write BEFORE UPDATE OR DELETE "
            "ON render_inspections "
            "FOR EACH ROW EXECUTE FUNCTION render_inspection_history_guard()"
        )
        op.execute(
            "CREATE TRIGGER immutable_render_inspections_truncate BEFORE TRUNCATE "
            "ON render_inspections "
            "FOR EACH STATEMENT EXECUTE FUNCTION render_inspection_history_guard()"
        )
        op.execute(
            "CREATE FUNCTION render_inspection_scope_guard() "
            f"RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: render inspection scope'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_render_inspections_insert BEFORE INSERT "
            "ON render_inspections "
            "FOR EACH ROW EXECUTE FUNCTION render_inspection_scope_guard()"
        )
    # Legacy successes retain their historical meaning; every new success must
    # refer to a passed inspection of that exact immutable file receipt.
    missing = (
        "OLD.status<>'SUCCEEDED' AND NEW.status='SUCCEEDED' "
        "AND EXISTS (SELECT 1 FROM render_jobs j WHERE j.id=NEW.id) "
        "AND NOT EXISTS (SELECT 1 FROM render_jobs j "
        "JOIN render_attempts a ON a.run_id=j.id AND a.attempt_no=j.attempt_count "
        "JOIN render_inspections i ON i.attempt_id=a.id "
        "JOIN stored_files f ON f.object_key=a.output_key "
        "WHERE j.id=NEW.id AND i.passed AND i.manifest_id=j.manifest_id "
        "AND i.manifest_sha256=j.manifest_sha256 AND i.input_sha256=a.input_sha256 "
        "AND f.id=NEW.output_file_id AND i.output_sha256=f.checksum "
        "AND i.byte_size=f.byte_size AND f.project_id=j.project_id)"
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER render_execution_inspection BEFORE UPDATE ON render_executions "
            f"WHEN {missing} BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: passed render inspection required'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION render_execution_inspection_guard() "
            f"RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {missing} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: passed render inspection required'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER render_execution_inspection BEFORE UPDATE ON render_executions "
            "FOR EACH ROW EXECUTE FUNCTION render_execution_inspection_guard()"
        )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Render inspection downgrade requires an online history check")
    db = op.get_bind()
    if db.dialect.name == "postgresql":
        op.execute("LOCK TABLE render_inspections IN ACCESS EXCLUSIVE MODE")
    else:
        driver = db.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            db.exec_driver_sql("BEGIN IMMEDIATE")
    if db.scalar(sa.text("SELECT 1 FROM render_inspections LIMIT 1")):
        raise RuntimeError("Cannot discard render inspection history")
    op.execute(
        "DROP TRIGGER render_execution_inspection"
        + (" ON render_executions" if db.dialect.name == "postgresql" else "")
    )
    op.drop_table("render_inspections")
    if db.dialect.name == "postgresql":
        op.execute("DROP FUNCTION render_execution_inspection_guard()")
        op.execute("DROP FUNCTION render_inspection_scope_guard()")
        op.execute("DROP FUNCTION render_inspection_history_guard()")
