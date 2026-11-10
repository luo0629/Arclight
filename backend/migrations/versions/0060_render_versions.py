"""Register append-only render versions while preserving legacy manual receipts."""

import sqlalchemy as sa
from alembic import context, op

revision = "0060"
down_revision = "0059"
branch_labels = None
depends_on = None


def json_text(column, path):
    if op.get_bind().dialect.name == "sqlite":
        return f"json_extract({column}, '$.{path}')"
    return f"({column} #>> '{{{path.replace('.', ',')}}}')"


def json_integer_matches(column, path, value):
    if op.get_bind().dialect.name == "sqlite":
        return f"json_type({column}, '$.{path}')='integer' AND {json_text(column, path)}={value}"
    location = path.replace(".", ",")
    return (
        f"jsonb_typeof({column} #> '{{{location}}}')='number' "
        f"AND {json_text(column, path)}=CAST({value} AS TEXT)"
    )


def uuid_matches(column, value):
    if op.get_bind().dialect.name == "sqlite":
        return f"{column}=replace({value},'-','')"
    return f"CAST({column} AS TEXT)={value}"


def artifact_condition(registered):
    condition = (
        "NOT EXISTS (SELECT 1 FROM render_manifests m JOIN stored_files f "
        "ON f.project_id=m.project_id WHERE m.id=NEW.manifest_id AND f.id=NEW.file_id "
        "AND f.media_type='video/mp4' AND f.object_key=NEW.object_key "
        "AND f.checksum=NEW.checksum AND f.byte_size=NEW.byte_size) "
        "OR EXISTS (SELECT 1 FROM render_artifacts WHERE id=NEW.id OR file_id=NEW.file_id "
    )
    if not registered:
        return condition + "OR manifest_id=NEW.manifest_id)"
    condition += (
        "OR (NEW.job_id IS NULL AND job_id IS NULL AND manifest_id=NEW.manifest_id) "
        "OR job_id=NEW.job_id OR asset_version_id=NEW.asset_version_id) "
    )
    lease = (
        "julianday(j.lease_expires_at)>julianday('now')"
        if op.get_bind().dialect.name == "sqlite"
        else "j.lease_expires_at>clock_timestamp()"
    )
    duration = json_text("i.result_json", "measurement.video.duration_us")
    metadata = uuid_matches("d.id", json_text("v.metadata_json", "dependency_manifest_id"))
    episode = uuid_matches("e.id", json_text("s.metadata_json", "episode_id"))
    references = (
        ("render_job_id", "j.id"),
        ("render_manifest_id", "m.id"),
        ("timeline_id", "t.id"),
        ("inspection_id", "i.id"),
        ("episode_id", "e.id"),
    )
    trace = " AND ".join(
        uuid_matches(column, json_text("v.metadata_json", name)) for name, column in references
    )
    bucket = json_text("v.metadata_json", "storage_bucket")
    trace += (
        f" AND {json_text('v.metadata_json', 'render_manifest_sha256')}=m.content_sha256"
        f" AND {json_text('v.metadata_json', 'timeline_sha256')}="
        f"{json_text('m.snapshot_json', 'timeline_sha256')}"
        f" AND {json_text('v.metadata_json', 'profile')}="
        f"{json_text('m.snapshot_json', 'output.name')}"
        f" AND {json_text('v.metadata_json', 'storage_backend')}=f.storage_backend"
        f" AND ({bucket}=f.bucket OR ({bucket} IS NULL AND f.bucket IS NULL))"
        f" AND {json_text('v.metadata_json', 'media.media_type')}='video/mp4'"
        f" AND {json_integer_matches('v.metadata_json', 'media.byte_size', 'NEW.byte_size')}"
        f" AND {json_integer_matches('v.metadata_json', 'media.duration_ms', 'NEW.duration_ms')}"
    )
    condition += (
        "OR (NEW.job_id IS NOT NULL AND NOT EXISTS ("
        "SELECT 1 FROM render_jobs j JOIN render_executions r ON r.id=j.id "
        "JOIN render_manifests m ON m.id=j.manifest_id "
        "JOIN timelines t ON t.id=m.timeline_id JOIN episodes e ON e.id=t.episode_id "
        "JOIN render_attempts a ON a.id=j.lease_token AND a.run_id=j.id "
        "JOIN render_inspections i ON i.attempt_id=a.id "
        "JOIN stored_files f ON f.id=NEW.file_id "
        "JOIN asset_versions v ON v.id=NEW.asset_version_id "
        "JOIN visual_assets s ON s.id=v.asset_id "
        "JOIN dependency_outputs o ON o.asset_version_id=v.id "
        "JOIN dependency_manifests d ON d.id=o.dependency_manifest_id "
        "WHERE j.id=NEW.job_id AND j.manifest_id=NEW.manifest_id "
        "AND j.manifest_sha256=m.content_sha256 AND r.project_id=j.project_id "
        "AND e.project_id=j.project_id AND r.status='RUNNING' AND a.status='RUNNING' "
        f"AND j.cancel_requested_at IS NULL AND {lease} "
        "AND a.attempt_no=j.attempt_count AND a.output_key=NEW.object_key "
        "AND i.passed AND i.manifest_id=j.manifest_id "
        "AND i.manifest_sha256=j.manifest_sha256 AND i.input_sha256=a.input_sha256 "
        "AND i.output_sha256=NEW.checksum AND i.byte_size=NEW.byte_size "
        f"AND NEW.duration_ms=CAST((CAST({duration} AS BIGINT)+500)/1000 AS BIGINT) "
        "AND f.project_id=j.project_id AND f.asset_version_id=v.id AND f.rights_confirmed "
        "AND v.object_key=NEW.object_key AND v.checksum=NEW.checksum AND v.creation_key=j.id "
        "AND NOT v.stale AND v.status='DRAFT' AND s.asset_type='RENDER' "
        "AND s.project_id=j.project_id AND d.project_id=j.project_id AND d.target_asset_id=s.id "
        f"AND {metadata} AND {episode} AND {trace}))"
    )
    return condition


def insert_guard(condition):
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER immutable_render_artifacts_insert BEFORE INSERT ON render_artifacts "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: render scope or replacement'); END"
        )
    else:
        op.execute(
            "CREATE OR REPLACE FUNCTION immutable_render_artifacts_insert_guard() "
            f"RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: render scope or replacement'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER immutable_render_artifacts_insert BEFORE INSERT ON render_artifacts "
            "FOR EACH ROW EXECUTE FUNCTION immutable_render_artifacts_insert_guard()"
        )


def sqlite_history_guards():
    for event in ("UPDATE", "DELETE"):
        op.execute(
            f"CREATE TRIGGER immutable_render_artifacts_{event.lower()} "
            f"BEFORE {event} ON render_artifacts BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: render history'); END"
        )


def success_guard():
    # Defer failed/missing inspection to 0059's original error contract.
    passed = (
        "SELECT 1 FROM render_jobs j "
        "JOIN render_attempts a ON a.run_id=j.id AND a.attempt_no=j.attempt_count "
        "JOIN render_inspections i ON i.attempt_id=a.id "
        "JOIN stored_files f ON f.object_key=a.output_key "
        "WHERE j.id=NEW.id AND i.passed AND i.manifest_id=j.manifest_id "
        "AND i.manifest_sha256=j.manifest_sha256 AND i.input_sha256=a.input_sha256 "
        "AND f.id=NEW.output_file_id AND i.output_sha256=f.checksum "
        "AND i.byte_size=f.byte_size AND f.project_id=j.project_id"
    )
    condition = (
        "OLD.status<>'SUCCEEDED' AND NEW.status='SUCCEEDED' "
        f"AND EXISTS ({passed}) AND NOT EXISTS ("
        "SELECT 1 FROM render_artifacts a JOIN stored_files f ON f.id=a.file_id "
        "WHERE a.job_id=NEW.id AND a.file_id=NEW.output_file_id "
        "AND f.asset_version_id=a.asset_version_id)"
    )
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            "CREATE TRIGGER render_execution_registration BEFORE UPDATE ON render_executions "
            f"WHEN {condition} BEGIN SELECT RAISE(ABORT, "
            "'IMMUTABLE_HISTORY: registered render version required'); END"
        )
    else:
        op.execute(
            "CREATE FUNCTION render_execution_registration_guard() "
            f"RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF {condition} THEN "
            "RAISE EXCEPTION 'IMMUTABLE_HISTORY: registered render version required'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            "CREATE TRIGGER render_execution_registration BEFORE UPDATE ON render_executions "
            "FOR EACH ROW EXECUTE FUNCTION render_execution_registration_guard()"
        )


def upgrade():
    sqlite = op.get_bind().dialect.name == "sqlite"
    # SQLite validates triggers on other tables while renaming a rebuilt table.
    # Preserve their exact SQL rather than silently losing report-history guards.
    report_trigger = None
    if sqlite:
        report_trigger = op.get_bind().scalar(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='immutable_render_report_links_insert'"
            )
        )
        op.execute("DROP TRIGGER immutable_render_report_links_insert")
    op.execute(
        "DROP TRIGGER immutable_render_artifacts_insert"
        + ("" if sqlite else " ON render_artifacts")
    )
    with op.batch_alter_table("render_artifacts") as batch:
        batch.drop_constraint("uq_render_artifacts_manifest_id", type_="unique")
        batch.add_column(sa.Column("job_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("asset_version_id", sa.Uuid(), nullable=True))
        batch.create_foreign_key(
            "fk_render_artifacts_job_id_render_jobs", "render_jobs", ["job_id"], ["id"]
        )
        batch.create_foreign_key(
            "fk_render_artifacts_asset_version_id_asset_versions",
            "asset_versions",
            ["asset_version_id"],
            ["id"],
        )
        batch.create_unique_constraint("uq_render_artifacts_job_id", ["job_id"])
        batch.create_unique_constraint("uq_render_artifacts_asset_version_id", ["asset_version_id"])
        batch.create_check_constraint(
            "render_artifact_registration",
            "(job_id IS NULL AND asset_version_id IS NULL) OR "
            "(job_id IS NOT NULL AND asset_version_id IS NOT NULL)",
        )
    op.create_index("ix_render_artifacts_manifest_id", "render_artifacts", ["manifest_id"])
    op.create_index(
        "uq_render_artifact_manual_manifest",
        "render_artifacts",
        ["manifest_id"],
        unique=True,
        sqlite_where=sa.text("job_id IS NULL"),
        postgresql_where=sa.text("job_id IS NULL"),
    )
    if sqlite:
        sqlite_history_guards()
        assert report_trigger
        op.execute(report_trigger)
    insert_guard(artifact_condition(True))
    success_guard()


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Render version downgrade requires an online history check")
    db = op.get_bind()
    sqlite = db.dialect.name == "sqlite"
    if not sqlite:
        op.execute("LOCK TABLE render_artifacts IN ACCESS EXCLUSIVE MODE")
    if db.scalar(sa.text("SELECT 1 FROM render_artifacts WHERE job_id IS NOT NULL LIMIT 1")):
        raise RuntimeError("Cannot discard render version history")
    op.execute(
        "DROP TRIGGER render_execution_registration" + ("" if sqlite else " ON render_executions")
    )
    if not sqlite:
        op.execute("DROP FUNCTION render_execution_registration_guard()")
    report_trigger = None
    if sqlite:
        report_trigger = db.scalar(
            sa.text(
                "SELECT sql FROM sqlite_master WHERE type='trigger' "
                "AND name='immutable_render_report_links_insert'"
            )
        )
        op.execute("DROP TRIGGER immutable_render_report_links_insert")
    op.execute(
        "DROP TRIGGER immutable_render_artifacts_insert"
        + ("" if sqlite else " ON render_artifacts")
    )
    op.drop_index("uq_render_artifact_manual_manifest", table_name="render_artifacts")
    op.drop_index("ix_render_artifacts_manifest_id", table_name="render_artifacts")
    with op.batch_alter_table("render_artifacts") as batch:
        batch.drop_constraint(
            op.f("ck_render_artifacts_render_artifact_registration"), type_="check"
        )
        batch.drop_constraint("uq_render_artifacts_job_id", type_="unique")
        batch.drop_constraint("uq_render_artifacts_asset_version_id", type_="unique")
        batch.drop_constraint("fk_render_artifacts_job_id_render_jobs", type_="foreignkey")
        batch.drop_constraint(
            "fk_render_artifacts_asset_version_id_asset_versions", type_="foreignkey"
        )
        batch.drop_column("job_id")
        batch.drop_column("asset_version_id")
        batch.create_unique_constraint("uq_render_artifacts_manifest_id", ["manifest_id"])
    if sqlite:
        sqlite_history_guards()
        assert report_trigger
        op.execute(report_trigger)
    insert_guard(artifact_condition(False))
