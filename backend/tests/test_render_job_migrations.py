"""Both database dialects preserve render history and reject scope forgeries."""

from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from render_admission_helpers import ready_manifest
from render_job_helpers import job_case, read, render_case, review_case, run, timeline_case
from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DBAPIError
from test_concurrency import require_postgres

from arclight.application.render_leases import claim
from arclight.persistence.models import RenderAttempt, RenderExecution, RenderJob

__all__ = ["job_case", "render_case", "review_case", "timeline_case"]
TABLES = ("render_jobs", "render_attempts")


def test_empty_job_migration_roundtrip_preserves_existing_manifest(client, render_case, migrated):
    manifest, _ = ready_manifest(client, render_case)
    command.downgrade(migrated, "0057")
    assert set(TABLES).isdisjoint(inspect(client.app.state.engine).get_table_names())
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case


def test_nonempty_job_downgrade_refuses_before_touching_schema(client, job_case, migrated):
    manifest, value, _ = job_case
    tables = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard render job history"):
        command.downgrade(migrated, "0057")
    assert set(inspect(client.app.state.engine).get_table_names()) == tables
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert read(client, value) == value
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    command.check(migrated)


def test_input_guards_and_finished_attempts_are_immutable(client, job_case):
    _, value, _ = job_case
    factory = client.app.state.session_factory
    assert run(client, value) == "SUCCEEDED"
    final = read(client, value)
    statements = (
        "UPDATE render_jobs SET request_sha256='forged'",
        "UPDATE render_jobs SET progress=0",
        "UPDATE render_attempts SET output_key=output_key",
        "UPDATE render_attempts SET input_sha256='forged'",
        "UPDATE render_attempts SET status='RUNNING'",
        "UPDATE render_executions SET created_by='forged'",
        "UPDATE render_executions SET status='RUNNING'",
        "DELETE FROM render_jobs",
        "DELETE FROM render_attempts",
        "DELETE FROM render_executions",
    )
    for statement in statements:
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.execute(text(statement))
    # Cleanup receipts are deliberately writable after terminal execution.
    with factory.begin() as db:
        row = db.scalar(select(RenderAttempt))
        row.cleanup_error_code = "RENDER_CLEANUP_PROTECTED"
    assert read(client, value) == final


def test_job_insert_requires_matching_manifest_hash_project_and_execution(client, job_case):
    _, value, _ = job_case
    factory = client.app.state.session_factory
    with factory() as db:
        original_job = db.get(RenderJob, UUID(value["id"]))
        original_run = db.get(RenderExecution, original_job.id)
        job_values = {c.name: getattr(original_job, c.name) for c in RenderJob.__table__.columns}
        run_values = {
            c.name: getattr(original_run, c.name) for c in RenderExecution.__table__.columns
        }
    for changes in (
        {"manifest_sha256": "0" * 64},
        {"manifest_id": uuid4()},
        {"project_id": uuid4()},
    ):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            run_id = uuid4()
            db.add(
                RenderExecution(**{**run_values, "id": run_id, "intermediate_key": str(uuid4())})
            )
            db.flush()
            db.add(RenderJob(**{**job_values, "id": run_id, "creation_key": uuid4(), **changes}))
            db.flush()


def test_attempt_insert_requires_current_fencing_token_and_owned_candidate_path(client, job_case):
    manifest, value, _ = job_case
    factory, run_id, token = client.app.state.session_factory, UUID(value["id"]), uuid4()
    with factory.begin() as db:
        db.get(RenderExecution, run_id).status = "RUNNING"
        job = db.get(RenderJob, run_id)
        job.attempt_count, job.lease_token = 1, token
    expected = f"rendered/{manifest['project_id']}/{token}"
    values = {"id": token, "run_id": run_id, "attempt_no": 1, "output_key": expected}
    for changes in (
        {"id": uuid4()},
        {"run_id": uuid4()},
        {"attempt_no": 2},
        {"output_key": f"rendered/{uuid4()}/{token}"},
        {"output_key": "../../private/output.mp4"},
    ):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
            db.add(RenderAttempt(**{**values, **changes}))
            db.flush()
    with factory.begin() as db:
        db.add(RenderAttempt(**values))


def test_sqlite_replace_cannot_rewrite_job_attempt_or_execution(client, job_case, settings):
    if not settings.database_url.startswith("sqlite"):
        pytest.skip("SQLite INSERT OR REPLACE syntax")
    _, value, _ = job_case
    assert claim(client.app.state.session_factory, UUID(value["id"]))
    for table in (*TABLES, "render_executions"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"))


def test_postgres_truncate_cannot_remove_job_history(client, settings, request):
    require_postgres(settings)
    _, value, _ = request.getfixturevalue("job_case")
    assert claim(client.app.state.session_factory, UUID(value["id"]))
    for table in (*TABLES, "render_executions"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(f"TRUNCATE {table} CASCADE"))


def test_offline_postgres_installs_all_guards_and_refuses_unchecked_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0057:0058", sql=True)
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"BEFORE TRUNCATE ON {table}" in sql
        assert f"CREATE FUNCTION {table}_scope_guard" in sql
    assert "render_execution_history_guard" in sql and "render_attempts_terminal_guard" in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0058:0057", sql=True)
