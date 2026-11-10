"""Both dialects bind append-only inspection evidence to an exact live attempt."""

from copy import deepcopy
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from render_inspection_helpers import movie
from render_job_helpers import expire, job_case, read, render_case, review_case, timeline_case
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_concurrency import require_postgres
from test_render_encoder import profile

from arclight.application.render_inspections import inspect_candidate
from arclight.application.render_leases import claim, finish
from arclight.persistence.models import (
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    StoredFile,
)

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]


@pytest.fixture(scope="module")
def measured_result():
    result = inspect_candidate(movie(), profile(), 500, observer=lambda _: None)
    assert result.passed
    return result.model_dump(mode="json", round_trip=True)


@pytest.fixture
def inspection_case(client, job_case, measured_result):
    manifest, value, _ = job_case
    factory = client.app.state.session_factory
    token = claim(factory, UUID(value["id"]))
    with factory.begin() as db:
        db.get(RenderAttempt, token).input_sha256 = "7" * 64
    fields = {
        "attempt_id": token,
        "manifest_id": UUID(manifest["id"]),
        "manifest_sha256": manifest["content_sha256"],
        "input_sha256": "7" * 64,
        "output_sha256": measured_result["output_sha256"],
        "byte_size": measured_result["byte_size"],
        "passed": True,
        "result_json": deepcopy(measured_result),
    }
    return manifest, value, fields


def insert(factory, fields):
    with factory.begin() as db:
        db.add(RenderInspection(**fields))


def output_file(db, manifest, token, report, **changes):
    attempt = db.get(RenderAttempt, token)
    row = StoredFile(
        project_id=UUID(manifest["project_id"]),
        object_key=attempt.output_key,
        storage_backend="LOCAL",
        checksum=report["output_sha256"],
        byte_size=report["byte_size"],
        media_type="video/mp4",
        original_name="candidate.mp4",
        source_note="Migration fixture; no object is published",
        rights_note="Offline generated fixture",
        rights_confirmed=True,
        created_by="service-owner",
    )
    for name, value in changes.items():
        setattr(row, name, value)
    db.add(row)
    db.flush()
    return row


@pytest.mark.parametrize(
    "change", ["attempt", "manifest", "manifest_hash", "input", "expired", "cancelled"]
)
def test_insert_requires_exact_scope_and_live_uncancelled_claim(client, inspection_case, change):
    _, value, fields = inspection_case
    factory = client.app.state.session_factory
    if change == "attempt":
        fields["attempt_id"] = uuid4()
    elif change == "manifest":
        fields["manifest_id"] = uuid4()
    elif change == "manifest_hash":
        fields["manifest_sha256"] = "0" * 64
    elif change == "input":
        fields["input_sha256"] = "0" * 64
    elif change == "expired":
        expire(client, value)
    else:
        with factory.begin() as db:
            from arclight.application.generation_leases import clock

            db.get(RenderJob, UUID(value["id"])).cancel_requested_at = clock(db)
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        insert(factory, fields)
    with factory() as db:
        assert db.scalar(select(RenderInspection)) is None


@pytest.mark.parametrize(
    "change",
    ["hash", "size", "passed", "checks", "duplicate", "missing", "measurement", "failure"],
)
def test_receipt_columns_and_complete_check_result_cannot_disagree(client, inspection_case, change):
    _, _, fields = inspection_case
    result = fields["result_json"]
    if change == "hash":
        fields["output_sha256"] = "0" * 64
    elif change == "size":
        fields["byte_size"] += 1
    elif change == "passed":
        fields["passed"] = False
    elif change == "checks":
        result["checks"][0]["passed"] = False
    elif change == "duplicate":
        result["checks"][0]["name"] = result["checks"][1]["name"]
    elif change == "missing":
        result.pop("checks")
    elif change == "measurement":
        result["measurement"] = None
    else:
        result["failure"] = "INVALID_MEDIA"
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        insert(client.app.state.session_factory, fields)


def test_inspection_is_append_only_and_duplicate_attempt_cannot_replace_it(client, inspection_case):
    _, value, fields = inspection_case
    factory = client.app.state.session_factory
    insert(factory, fields)
    before = client.get(f"/api/v1/render-jobs/{value['id']}/attempts").json()
    for statement in (
        "UPDATE render_inspections SET passed=passed",
        "UPDATE render_inspections SET output_sha256='forged'",
        "DELETE FROM render_inspections",
    ):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(text(statement))
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"):
        insert(factory, fields)
    assert client.get(f"/api/v1/render-jobs/{value['id']}/attempts").json() == before


def test_sqlite_replace_cannot_rewrite_inspection(client, inspection_case, settings):
    if not settings.database_url.startswith("sqlite"):
        pytest.skip("SQLite INSERT OR REPLACE syntax")
    insert(client.app.state.session_factory, inspection_case[2])
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(
            text("INSERT OR REPLACE INTO render_inspections SELECT * FROM render_inspections")
        )


def test_postgres_truncate_cannot_remove_inspection(client, settings, request):
    require_postgres(settings)
    insert(client.app.state.session_factory, request.getfixturevalue("inspection_case")[2])
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.execute(text("TRUNCATE render_inspections CASCADE"))


@pytest.mark.parametrize("mismatch", ["missing", "failed", "hash", "size", "old_attempt"])
def test_success_requires_passed_check_of_current_attempt_and_exact_file(
    client, inspection_case, mismatch
):
    manifest, value, fields = inspection_case
    factory = client.app.state.session_factory
    if mismatch == "failed":
        fields["passed"] = False
        fields["result_json"]["checks"][0]["passed"] = False
    if mismatch != "missing":
        insert(factory, fields)
    token = fields["attempt_id"]
    if mismatch == "old_attempt":
        expire(client, value)
        token = claim(factory, UUID(value["id"]))
        with factory.begin() as db:
            db.get(RenderAttempt, token).input_sha256 = fields["input_sha256"]
    with factory.begin() as db:
        changes = {"checksum": "0" * 64} if mismatch == "hash" else {}
        if mismatch == "size":
            changes["byte_size"] = fields["byte_size"] + 1
        file_id = output_file(db, manifest, token, fields["result_json"], **changes).id
    with (
        pytest.raises(DBAPIError, match="passed render inspection required"),
        factory.begin() as db,
        db.no_autoflush,
    ):
        run, job = db.get(RenderExecution, UUID(value["id"])), db.get(RenderJob, UUID(value["id"]))
        run.output_file_id = file_id
        finish(db, run, job, "SUCCEEDED", None)
    assert read(client, value)["status"] == "RUNNING"
    assert read(client, value)["output_file_id"] is None


def test_empty_migration_roundtrip_preserves_jobs_and_metadata(client, job_case, migrated):
    _, value, _ = job_case
    command.downgrade(migrated, "0058")
    assert "render_inspections" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert read(client, value) == value


def test_nonempty_downgrade_refuses_without_losing_history_or_schema(
    client, inspection_case, migrated
):
    _, value, fields = inspection_case
    insert(client.app.state.session_factory, fields)
    before = client.get(f"/api/v1/render-jobs/{value['id']}/attempts").json()
    tables = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard render inspection history"):
        command.downgrade(migrated, "0058")
    assert set(inspect(client.app.state.engine).get_table_names()) == tables
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == revision
    assert client.get(f"/api/v1/render-jobs/{value['id']}/attempts").json() == before
    command.check(migrated)


@pytest.mark.parametrize("status", ["SUCCEEDED", "FAILED"])
def test_upgrade_preserves_legacy_attempts_without_fabricating_checks(
    client, job_case, measured_result, migrated, status
):
    manifest, value, _ = job_case
    factory, run_id = client.app.state.session_factory, UUID(value["id"])
    command.downgrade(migrated, "0058")
    token = claim(factory, run_id)
    with factory.begin() as db, db.no_autoflush:
        run, job = db.get(RenderExecution, run_id), db.get(RenderJob, run_id)
        if status == "SUCCEEDED":
            run.output_file_id = output_file(db, manifest, token, measured_result).id
        finish(db, run, job, status, None if status == "SUCCEEDED" else "LEGACY_FAILURE")
    tables = ("render_executions", "render_jobs", "render_attempts", "stored_files")
    with factory() as db:
        before = {
            table: db.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
            for table in tables
        }
    command.upgrade(migrated, "head")
    with factory() as db:
        after = {
            table: db.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
            for table in tables
        }
    assert after == before
    result = read(client, value)
    assert result["status"] == status
    assert result["artifact_id"] is None and result["output_asset_version_id"] is None
    response = client.get(f"/api/v1/render-jobs/{value['id']}/attempts")
    assert response.status_code == 200, response.text
    assert len(response.json()) == 1 and response.json()[0]["inspection"] is None
    with factory.begin() as db:
        db.execute(text("UPDATE render_executions SET updated_at=updated_at"))
    command.check(migrated)


def test_offline_postgres_guards_and_online_only_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0058:0059", sql=True)
    sql = output.getvalue()
    for statement in (
        "CREATE TABLE render_inspections",
        "BEFORE TRUNCATE ON render_inspections",
        "CREATE FUNCTION render_inspection_scope_guard",
        "CREATE FUNCTION render_execution_inspection_guard",
        "clock_timestamp()",
        "jsonb_array_elements",
        "passed render inspection required",
    ):
        assert statement in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0059:0058", sql=True)
