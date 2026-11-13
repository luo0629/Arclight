"""Media byte receipts widen without replacing immutable history or narrowing it."""

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from render_job_helpers import job_case, render_case, review_case, timeline_case
from sqlalchemy import event, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DBAPIError
from test_render_inspection_migrations import inspection_case, measured_result

from arclight.persistence.models import (
    CleanupItem,
    CleanupPlan,
    RenderArtifact,
    RenderInspection,
    RenderReportLink,
    StoredFile,
    UploadIntent,
)

__all__ = [
    "inspection_case",
    "job_case",
    "measured_result",
    "render_case",
    "review_case",
    "timeline_case",
]

TABLES = (
    "cleanup_items",
    "render_artifacts",
    "render_inspections",
    "render_report_links",
    "stored_files",
    "upload_intents",
)


@pytest.fixture
def seed_receipts(client, episode, inspection_case):
    """Insert DB receipts only; multi-GiB values do not claim real media decoding."""
    manifest, _, inspection_fields = inspection_case
    factory = client.app.state.session_factory
    project_id = UUID(episode["project_id"])

    def seed(target=None, byte_size=127):
        def size(table):
            return byte_size if target == table else 127

        def file(db, amount, media_type):
            row = StoredFile(
                project_id=project_id,
                storage_backend="LOCAL",
                object_key=f"migration-receipts/{uuid4()}",
                checksum="a" * 64,
                byte_size=amount,
                media_type=media_type,
                original_name="database-receipt",
                source_note="DB migration receipt; no media object published",
                rights_note="Original deterministic migration fixture",
                rights_confirmed=True,
                created_by="service-owner",
            )
            db.add(row)
            db.flush()
            return row

        now = datetime.now(UTC)
        with factory.begin() as db:
            standalone = file(db, size("stored_files"), "video/mp4")
            video = file(db, size("render_artifacts"), "video/mp4")
            report_file = file(db, size("render_report_links"), "application/json")
            upload = UploadIntent(
                project_id=project_id,
                bucket="migration-fixtures",
                object_key=f"migration-upload/{uuid4()}",
                purpose="video",
                original_name="receipt.mp4",
                declared_media_type="video/mp4",
                byte_size=size("upload_intents"),
                checksum="b" * 64,
                expires_at=now + timedelta(hours=1),
                created_by="service-owner",
            )
            plan = CleanupPlan(
                project_id=project_id,
                namespace="migration",
                storage_identity="c" * 64,
                scanned_at=now,
                execute_after=now + timedelta(hours=1),
                expires_at=now + timedelta(days=1),
                scanned_count=1,
                retained_count=0,
                created_by="service-owner",
            )
            db.add_all([upload, plan])
            db.flush()
            cleanup = CleanupItem(
                plan_id=plan.id,
                object_key=f"migration-cleanup/{uuid4()}",
                byte_size=size("cleanup_items"),
                modified_at=now,
                object_token="d" * 64,
                reason="ORPHAN",
            )
            artifact = RenderArtifact(
                manifest_id=UUID(manifest["id"]),
                file_id=video.id,
                object_key=video.object_key,
                checksum=video.checksum,
                byte_size=video.byte_size,
                duration_ms=500,
            )
            fields = deepcopy(inspection_fields)
            fields["byte_size"] = size("render_inspections")
            fields["result_json"]["byte_size"] = fields["byte_size"]
            inspection = RenderInspection(**fields)
            db.add_all([cleanup, artifact, inspection])
            db.flush()
            report = RenderReportLink(
                artifact_id=artifact.id,
                file_id=report_file.id,
                object_key=report_file.object_key,
                checksum=report_file.checksum,
                byte_size=report_file.byte_size,
            )
            db.add(report)
            db.flush()
            return {
                row.__tablename__: row.id
                for row in (cleanup, artifact, inspection, report, standalone, upload)
            }

    return seed


def snapshot(engine):
    inspector = inspect(engine)
    with engine.connect() as db:
        rows = {
            table: db.execute(text(f"SELECT * FROM {table} ORDER BY id")).mappings().all()
            for table in TABLES
        }
        if engine.dialect.name == "sqlite":
            guards = db.execute(
                text("SELECT name, sql FROM sqlite_master WHERE type='trigger' ORDER BY name")
            ).all()
            roots = db.execute(
                text("SELECT name, rootpage FROM sqlite_master WHERE type='table' ORDER BY name")
            ).all()
            assert db.exec_driver_sql("PRAGMA writable_schema").scalar_one() == 0
            assert db.exec_driver_sql("PRAGMA integrity_check").scalars().all() == ["ok"]
        else:
            guards = db.execute(
                text(
                    "SELECT t.tgname, pg_get_triggerdef(t.oid) FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid=t.tgrelid "
                    "JOIN pg_namespace n ON n.oid=c.relnamespace "
                    "WHERE n.nspname=current_schema() AND NOT t.tgisinternal "
                    "ORDER BY c.relname, t.tgname"
                )
            ).all()
            roots = None
    return {
        "rows": rows,
        "guards": guards,
        "roots": roots,
        "foreign_keys": {table: inspector.get_foreign_keys(table) for table in TABLES},
    }


def schema(engine):
    inspector = inspect(engine)
    with engine.connect() as db:
        revision = db.scalar(text("SELECT version_num FROM alembic_version"))
    return {
        "revision": revision,
        "columns": {
            table: [
                {**column, "type": str(column["type"])} for column in inspector.get_columns(table)
            ]
            for table in TABLES
        },
        "checks": inspector.get_check_constraints("render_inspections"),
    }


def assert_types(engine, expected):
    for table in TABLES:
        column = next(c for c in inspect(engine).get_columns(table) if c["name"] == "byte_size")
        assert str(column["type"]).upper() == expected
    source = next(
        c for c in inspect(engine).get_columns("story_sources") if c["name"] == "byte_size"
    )
    assert str(source["type"]).upper() == "INTEGER"


def test_nonempty_roundtrip_keeps_receipts_guards_and_table_identity(
    client, migrated, seed_receipts
):
    ids = seed_receipts()
    engine = client.app.state.engine
    before = snapshot(engine)
    assert_types(engine, "INTEGER" if engine.dialect.name == "sqlite" else "BIGINT")
    command.check(migrated)
    command.downgrade(migrated, "0064")
    assert snapshot(engine) == before
    assert_types(engine, "INTEGER")
    checks = inspect(engine).get_check_constraints("render_inspections")
    assert any("134217728" in row["sqltext"] for row in checks)
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert snapshot(engine) == before
    assert_types(engine, "INTEGER" if engine.dialect.name == "sqlite" else "BIGINT")
    assert all(
        "134217728" not in row["sqltext"]
        for row in inspect(engine).get_check_constraints("render_inspections")
    )
    for table in TABLES[:-1]:
        with pytest.raises(DBAPIError), engine.begin() as db:
            db.execute(
                text(f"UPDATE {table} SET byte_size=byte_size+1 WHERE id=:id"),
                {"id": ids[table].hex if engine.dialect.name == "sqlite" else ids[table]},
            )
    assert snapshot(engine) == before


@pytest.mark.parametrize("table", TABLES)
def test_large_receipt_refuses_narrowing_before_any_schema_mutation(
    client, migrated, seed_receipts, table
):
    ids = seed_receipts(table, 2**31)
    engine = client.app.state.engine
    before, before_schema = snapshot(engine), schema(engine)
    with engine.connect() as db:
        actual = db.scalar(
            text(f"SELECT byte_size FROM {table} WHERE id=:id"),
            {"id": ids[table].hex if engine.dialect.name == "sqlite" else ids[table]},
        )
    assert actual == 2**31
    statements = []

    def collect(_connection, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().upper())

    event.listen(Engine, "before_cursor_execute", collect)
    try:
        with pytest.raises(RuntimeError, match=f"Cannot narrow media byte size history: {table}"):
            command.downgrade(migrated, "0064")
    finally:
        event.remove(Engine, "before_cursor_execute", collect)
    assert not any(
        statement.startswith(("ALTER ", "CREATE ", "DROP ", "UPDATE SQLITE_MASTER"))
        or statement == "PRAGMA WRITABLE_SCHEMA=ON"
        for statement in statements
    )
    assert snapshot(engine) == before
    assert schema(engine) == before_schema
    command.check(migrated)


def test_inspection_old_check_refuses_downgrade_before_any_schema_mutation(
    client, migrated, seed_receipts
):
    seed_receipts("render_inspections", 134217729)
    engine = client.app.state.engine
    before, before_schema = snapshot(engine), schema(engine)
    with pytest.raises(
        RuntimeError, match="Cannot restore the old render inspection byte size limit"
    ):
        command.downgrade(migrated, "0064")
    assert snapshot(engine) == before
    assert schema(engine) == before_schema
    command.check(migrated)


@pytest.mark.parametrize("direction", ["upgrade", "downgrade"])
def test_sqlite_schema_edit_failure_rolls_back_and_can_retry(
    client, migrated, seed_receipts, direction
):
    engine = client.app.state.engine
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite schema editor transaction recovery")
    seed_receipts()
    if direction == "upgrade":
        command.downgrade(migrated, "0064")
    before, before_schema = snapshot(engine), schema(engine)
    with engine.connect() as db:
        original_sql = db.scalar(
            text("SELECT sql FROM sqlite_master WHERE name='render_inspections'")
        )
        original_version = db.exec_driver_sql("PRAGMA schema_version").scalar_one()

    def fail_after_schema_edit(_connection, _cursor, statement, _parameters, _context, _many):
        if statement.upper() == "PRAGMA INTEGRITY_CHECK":
            raise RuntimeError("Injected failure after schema rewrite")

    event.listen(Engine, "before_cursor_execute", fail_after_schema_edit)
    try:
        with pytest.raises(RuntimeError, match="Injected failure after schema rewrite"):
            if direction == "upgrade":
                command.upgrade(migrated, "head")
            else:
                command.downgrade(migrated, "0064")
    finally:
        event.remove(Engine, "before_cursor_execute", fail_after_schema_edit)
    assert snapshot(engine) == before
    assert schema(engine) == before_schema
    with engine.connect() as db:
        assert (
            db.scalar(text("SELECT sql FROM sqlite_master WHERE name='render_inspections'"))
            == original_sql
        )
        assert db.exec_driver_sql("PRAGMA schema_version").scalar_one() == original_version
    if direction == "upgrade":
        command.upgrade(migrated, "head")
    else:
        command.downgrade(migrated, "0064")
        command.upgrade(migrated, "head")
    assert snapshot(engine) == before
    command.check(migrated)


def test_postgres_offline_widens_all_six_receipts_but_refuses_unchecked_downgrade(monkeypatch):
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    monkeypatch.setitem(
        config.attributes, "database_url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0064:0065", sql=True)
    sql = output.getvalue()
    drop = sql.index("DROP TRIGGER immutable_cleanup_items_write ON cleanup_items")
    recreate = sql.index("CREATE TRIGGER immutable_cleanup_items_write BEFORE UPDATE OF")
    assert sql.index("IN ACCESS EXCLUSIVE MODE") < drop
    for table in TABLES:
        alteration = sql.index(f"ALTER TABLE {table} ALTER COLUMN byte_size TYPE BIGINT")
        assert drop < alteration < recreate
    assert (
        "id,plan_id,object_key,byte_size,modified_at,object_token,reason,created_at "
        "OR DELETE ON cleanup_items FOR EACH ROW "
        "EXECUTE FUNCTION immutable_stored_files_guard()"
    ) in sql
    assert "immutable_cleanup_items_truncate" not in sql
    assert "CHECK (byte_size > 0)" in sql
    assert "134217728" not in sql
    with pytest.raises(RuntimeError, match="requires an online range check"):
        command.downgrade(config, "0065:0064", sql=True)


def test_sqlite_offline_refuses_schema_edits_without_inspecting_live_schema(monkeypatch):
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=StringIO())
    monkeypatch.setitem(config.attributes, "database_url", "sqlite:///offline.db")
    with pytest.raises(RuntimeError, match="requires an online schema check"):
        command.upgrade(config, "0064:0065", sql=True)
