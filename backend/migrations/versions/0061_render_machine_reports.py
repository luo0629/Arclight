"""Bind immutable machine reports to the exact inspected formal render."""

import sqlalchemy as sa
from alembic import context, op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def json_value(column, path):
    if op.get_bind().dialect.name == "sqlite":
        return f"json_extract({column}, '$.{path}')"
    return f"({column} #> '{{{path.replace('.', ',')}}}')"


def json_text(column, path):
    if op.get_bind().dialect.name == "sqlite":
        return json_value(column, path)
    return f"({column} #>> '{{{path.replace('.', ',')}}}')"


def json_type(column, path, kind):
    if op.get_bind().dialect.name == "sqlite":
        return f"json_type({column}, '$.{path}')='{kind}'"
    kinds = {"text": "string", "integer": "number", "true": "boolean"}
    return f"jsonb_typeof({json_value(column, path)})='{kinds.get(kind, kind)}'"


def json_equal(left, right):
    if op.get_bind().dialect.name != "sqlite":
        return f"({left})=({right})"
    # Compare paths, types and scalar values rather than object-key serialization order.
    columns = "fullkey,type,atom"
    return (
        f"NOT EXISTS (SELECT {columns} FROM json_tree({left}) "
        f"EXCEPT SELECT {columns} FROM json_tree({right})) AND "
        f"NOT EXISTS (SELECT {columns} FROM json_tree({right}) "
        f"EXCEPT SELECT {columns} FROM json_tree({left}))"
    )


def uuid_matches(column, value):
    if op.get_bind().dialect.name == "sqlite":
        return f"{column}=replace({value},'-','')"
    return f"CAST({column} AS TEXT)={value}"


def machine_condition():
    report = "NEW.machine_report_json"
    checks = [
        json_type(report, "schema_version", "integer"),
        f"{json_value(report, 'schema_version')}=1"
        if op.get_bind().dialect.name == "sqlite"
        else f"{json_text(report, 'schema_version')}='1'",
        json_type(report, "details", "object"),
        f"{json_text(report, 'details.schema_version')}='render-report/1'",
        f"{json_text(report, 'details.checker')}='arclight-delivery-v1'",
        f"{json_text(report, 'details.render_status')}='passed'",
        f"{json_text(report, 'details.profile')}='FORMAL'",
        f"{json_type(report, 'details.font', 'null')} "
        f"OR {json_type(report, 'details.font', 'object')}",
        f"{json_text('m.snapshot_json', 'output.name')}='FORMAL'",
    ]
    identities = {
        "manifest_id": "m.id",
        "timeline_id": "m.timeline_id",
        "output_file_id": "a.file_id",
        "details.job_id": "j.id",
        "details.attempt_id": "t.id",
        "details.artifact_id": "a.id",
        "details.asset_version_id": "a.asset_version_id",
        "details.manifest_id": "m.id",
        "details.timeline_id": "m.timeline_id",
        "details.inspection_id": "i.id",
        "details.output_file_id": "a.file_id",
    }
    for path, column in identities.items():
        checks.extend(
            (json_type(report, path, "text"), uuid_matches(column, json_text(report, path)))
        )
    for path, column in {
        "manifest_sha256": "m.content_sha256",
        "output_sha256": "a.checksum",
        "details.manifest_sha256": "m.content_sha256",
        "details.timeline_sha256": json_text("m.snapshot_json", "timeline_sha256"),
        "details.input_sha256": "NEW.input_sha256",
        "details.output_sha256": "a.checksum",
    }.items():
        checks.extend((json_type(report, path, "text"), f"{json_text(report, path)}={column}"))
    for path, column in {
        "details.byte_size": "a.byte_size",
        "details.duration_ms": "a.duration_ms",
        "details.timeline_version": "l.version_no",
        "details.checks.missing_required_shots": "0",
        "details.checks.missing_audio_lines": "0",
        "details.checks.stale_dependencies": "0",
    }.items():
        checks.extend(
            (
                json_type(report, path, "integer"),
                f"{json_text(report, path)}=CAST({column} AS TEXT)"
                if op.get_bind().dialect.name != "sqlite"
                else f"{json_text(report, path)}={column}",
            )
        )
    for path in ("video_decodable", "audio_stream_present", "subtitle_range_valid"):
        location = f"details.checks.{path}"
        checks.append(json_type(report, location, "true"))
        checks.append(
            f"{json_text(report, location)}="
            + ("1" if op.get_bind().dialect.name == "sqlite" else "'true'")
        )
    for path in ("sources", "font", "software", "output"):
        checks.append(
            json_equal(json_value(report, f"details.{path}"), json_value("m.snapshot_json", path))
        )
    empty_array = "'[]'" if op.get_bind().dialect.name == "sqlite" else "'[]'::jsonb"
    checks.append(
        json_equal(
            json_value(report, "details.line_audio_decisions"),
            f"coalesce({json_value('m.snapshot_json', 'line_audio_decisions')},{empty_array})",
        )
    )
    checks.append(json_equal(json_value(report, "details.checks.technical"), "i.result_json"))
    checks.append(warning_condition(report))
    lease = (
        "julianday(j.lease_expires_at)>julianday('now')"
        if op.get_bind().dialect.name == "sqlite"
        else "j.lease_expires_at>clock_timestamp()"
    )
    valid = (
        "SELECT 1 FROM render_artifacts a JOIN render_jobs j ON j.id=a.job_id "
        "JOIN render_executions r ON r.id=j.id JOIN render_manifests m ON m.id=j.manifest_id "
        "JOIN timelines l ON l.id=m.timeline_id "
        "JOIN render_attempts t ON t.id=j.lease_token AND t.run_id=j.id "
        "JOIN render_inspections i ON i.attempt_id=t.id "
        "WHERE a.id=NEW.artifact_id AND a.manifest_id=m.id "
        "AND i.id=NEW.inspection_id AND i.passed AND i.manifest_id=m.id "
        "AND i.manifest_sha256=m.content_sha256 AND j.manifest_sha256=m.content_sha256 "
        "AND i.input_sha256=NEW.input_sha256 AND t.input_sha256=NEW.input_sha256 "
        "AND i.output_sha256=a.checksum AND i.byte_size=a.byte_size "
        "AND j.project_id=m.project_id AND r.project_id=j.project_id "
        "AND r.status='RUNNING' AND t.status='RUNNING' AND t.attempt_no=j.attempt_count "
        f"AND j.cancel_requested_at IS NULL AND {lease} AND "
        + " AND ".join(f"({item})" for item in checks)
    )
    present = (
        "NEW.inspection_id IS NOT NULL OR NEW.input_sha256 IS NOT NULL "
        "OR NEW.machine_report_json IS NOT NULL"
    )
    return f"({present}) AND NOT EXISTS ({valid})"


def warning_condition(report):
    path = "details.warnings"
    value = json_value(report, path)
    sqlite = op.get_bind().dialect.name == "sqlite"
    warning = "w.value" if sqlite else "w"
    decision = "d.value" if sqlite else "d"
    source = "s.value" if sqlite else "s"
    decisions = json_value("m.snapshot_json", "line_audio_decisions")
    sources = json_value("m.snapshot_json", "sources")
    rows = f"json_each({value})" if sqlite else f"jsonb_array_elements({value})"
    decision_rows = f"json_each({decisions})" if sqlite else f"jsonb_array_elements({decisions})"
    source_rows = f"json_each({sources})" if sqlite else f"jsonb_array_elements({sources})"
    length = "json_array_length" if sqlite else "jsonb_array_length"
    code = json_text(warning, "code")
    identity = json_text(warning, "id")
    reason = json_text(warning, "reason")
    hexadecimal = (
        f"length({identity})=64 AND {identity} NOT GLOB '*[^0-9a-f]*'"
        if sqlite
        else f"{identity} ~ '^[0-9a-f]{{64}}$'"
    )
    line_match = " AND ".join(
        [
            f"{code}=CASE WHEN {json_text(decision, 'mode')}='SILENT' "
            "THEN 'EXPLICIT_SILENCE' ELSE 'SUBTITLE_ONLY' END",
            f"{json_text(warning, 'source_version_id')}={json_text(decision, 'script_version_id')}",
            f"{json_text(warning, 'line_id')}={json_text(decision, 'line_id')}",
            f"{reason}={json_text(decision, 'reason')}",
        ]
    )
    provenance = (
        "human_review_request_id",
        "human_review_decision_id",
        "human_review_request_sha256",
    )
    line_warning = (
        f"{code} IN ('EXPLICIT_SILENCE','SUBTITLE_ONLY') AND "
        + " AND ".join(json_type(warning, field, "null") for field in provenance)
        + f" AND EXISTS (SELECT 1 FROM {decision_rows} d WHERE {line_match})"
    )
    media_warning = (
        f"{code}='MEDIA_EXCEPTION' AND {json_type(warning, 'line_id', 'null')} AND "
        + " AND ".join(json_type(warning, field, "text") for field in provenance)
        + f" AND EXISTS (SELECT 1 FROM {source_rows} s WHERE "
        + f"{json_text(source, 'asset_type')} IN ('IMAGE','SHOT_IMAGE') AND "
        + f"{json_text(source, 'asset_version_id')}={json_text(warning, 'source_version_id')})"
        + " AND EXISTS (SELECT 1 FROM human_review_requests q "
        "JOIN human_review_decisions h ON h.request_id=q.id WHERE "
        + uuid_matches("q.id", json_text(warning, "human_review_request_id"))
        + " AND "
        + uuid_matches("h.id", json_text(warning, "human_review_decision_id"))
        + " AND q.project_id=m.project_id AND q.episode_id=l.episode_id "
        + "AND q.handler='continuity_shot' "
        + f"AND {json_text('q.snapshot_json', 'kind')}='EXCEPTIONAL_SHOT' "
        + f"AND {json_text('q.snapshot_json', 'purpose')}='PRODUCTION' "
        + f"AND {json_text('q.snapshot_json', 'version_id')}="
        + json_text(warning, "source_version_id")
        + f" AND {json_text('h.decision_json', 'version_id')}="
        + json_text(warning, "source_version_id")
        + f"AND q.request_sha256={json_text(warning, 'human_review_request_sha256')}"
        + f" AND {json_text('h.decision_json', 'request_sha256')}=q.request_sha256"
        + f" AND {json_text('h.decision_json', 'decision')}='APPROVE'"
        + f" AND {json_text('h.decision_json', 'comment')}={reason})"
    )
    valid = " AND ".join(
        [
            json_type(report, path, "array"),
            f"{length}({value})<=7000",
            json_type(warning, "id", "text"),
            hexadecimal,
            json_type(warning, "source_version_id", "text"),
            json_type(warning, "reason", "text"),
            f"length({reason})<=2000 AND length(trim({reason}))>=1",
            f"(({line_warning}) OR ({media_warning}))",
        ]
    )
    return (
        f"{json_type(report, path, 'array')} AND {length}({value})<=7000 "
        f"AND NOT EXISTS (SELECT 1 FROM {rows} w WHERE NOT coalesce(({valid}),false)) "
        f"AND (SELECT count(DISTINCT {identity}) FROM {rows} w)={length}({value}) "
        f"AND (SELECT count(*) FROM {rows} w WHERE {code}<>'MEDIA_EXCEPTION')="
        f"coalesce({length}({decisions}),0) "
        f"AND NOT EXISTS (SELECT 1 FROM {decision_rows} d WHERE NOT EXISTS "
        f"(SELECT 1 FROM {rows} w WHERE {line_match}))"
    )


def create_guard(name, table, condition, message):
    action = "UPDATE" if table == "render_executions" else "INSERT"
    if op.get_bind().dialect.name == "sqlite":
        op.execute(
            f"CREATE TRIGGER {name} BEFORE {action} ON {table} WHEN {condition} "
            f"BEGIN SELECT RAISE(ABORT, 'IMMUTABLE_HISTORY: {message}'); END"
        )
    else:
        op.execute(
            f"CREATE FUNCTION {name}_guard() RETURNS trigger LANGUAGE plpgsql AS $$ "
            f"BEGIN IF {condition} THEN RAISE EXCEPTION 'IMMUTABLE_HISTORY: {message}'; "
            "END IF; RETURN NEW; END; $$"
        )
        op.execute(
            f"CREATE TRIGGER {name} BEFORE {action} ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION {name}_guard()"
        )


def sqlite_history(remove=False):
    if op.get_bind().dialect.name != "sqlite":
        return []
    db = op.get_bind()
    if remove:
        rows = db.execute(
            sa.text(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='render_report_links' AND name LIKE 'immutable_%' ORDER BY name"
            )
        ).all()
        for name, _ in rows:
            op.execute(f'DROP TRIGGER "{name}"')
        return [sql for _, sql in rows]
    return []


def upgrade():
    old_guards = sqlite_history(remove=True)
    with op.batch_alter_table("render_report_links") as batch:
        batch.add_column(sa.Column("inspection_id", sa.Uuid(), nullable=True))
        batch.add_column(sa.Column("input_sha256", sa.String(64), nullable=True))
        batch.add_column(
            sa.Column(
                "machine_report_json",
                sa.JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql"),
                nullable=True,
            )
        )
        batch.create_foreign_key(
            "fk_render_report_links_inspection_id_render_inspections",
            "render_inspections",
            ["inspection_id"],
            ["id"],
        )
        batch.create_unique_constraint("uq_render_report_links_inspection_id", ["inspection_id"])
        batch.create_check_constraint(
            "render_report_machine_provenance",
            "(inspection_id IS NULL AND input_sha256 IS NULL AND machine_report_json IS NULL) OR "
            "(inspection_id IS NOT NULL AND input_sha256 IS NOT NULL "
            "AND machine_report_json IS NOT NULL)",
        )
    for sql in old_guards:
        op.execute(sql)
    create_guard(
        "render_report_machine_insert",
        "render_report_links",
        machine_condition(),
        "exact machine render report required",
    )
    formal = json_text("m.snapshot_json", "output.name")
    condition = (
        "OLD.status<>'SUCCEEDED' AND NEW.status='SUCCEEDED' AND EXISTS ("
        "SELECT 1 FROM render_jobs j JOIN render_manifests m ON m.id=j.manifest_id "
        "JOIN render_artifacts a ON a.job_id=j.id WHERE j.id=NEW.id "
        f"AND {formal}='FORMAL' AND a.file_id=NEW.output_file_id) AND NOT EXISTS ("
        "SELECT 1 FROM render_artifacts a JOIN render_report_links p ON p.artifact_id=a.id "
        "JOIN render_inspections i ON i.id=p.inspection_id "
        "JOIN render_attempts t ON t.id=i.attempt_id JOIN render_jobs j ON j.id=a.job_id "
        "WHERE a.job_id=NEW.id AND a.file_id=NEW.output_file_id "
        "AND t.run_id=j.id AND t.attempt_no=j.attempt_count AND i.passed "
        "AND p.machine_report_json IS NOT NULL AND p.input_sha256=i.input_sha256 "
        "AND i.output_sha256=a.checksum AND i.byte_size=a.byte_size)"
    )
    create_guard(
        "render_execution_machine_report",
        "render_executions",
        condition,
        "machine formal render report required",
    )


def downgrade():
    if context.is_offline_mode():
        raise RuntimeError("Machine report downgrade requires an online history check")
    db = op.get_bind()
    sqlite = db.dialect.name == "sqlite"
    if not sqlite:
        op.execute("LOCK TABLE render_report_links IN ACCESS EXCLUSIVE MODE")
    if db.scalar(
        sa.text(
            "SELECT 1 FROM render_report_links WHERE inspection_id IS NOT NULL "
            "OR input_sha256 IS NOT NULL OR machine_report_json IS NOT NULL LIMIT 1"
        )
    ):
        raise RuntimeError("Cannot discard machine render report history")
    for name, table in (
        ("render_execution_machine_report", "render_executions"),
        ("render_report_machine_insert", "render_report_links"),
    ):
        op.execute(f"DROP TRIGGER {name}" + ("" if sqlite else f" ON {table}"))
        if not sqlite:
            op.execute(f"DROP FUNCTION {name}_guard()")
    old_guards = sqlite_history(remove=True)
    with op.batch_alter_table("render_report_links") as batch:
        batch.drop_constraint(
            op.f("ck_render_report_links_render_report_machine_provenance"), type_="check"
        )
        batch.drop_constraint("uq_render_report_links_inspection_id", type_="unique")
        batch.drop_constraint(
            "fk_render_report_links_inspection_id_render_inspections", type_="foreignkey"
        )
        batch.drop_column("machine_report_json")
        batch.drop_column("input_sha256")
        batch.drop_column("inspection_id")
    for sql in old_guards:
        op.execute(sql)
