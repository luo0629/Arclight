"""Render manifest migration preserves timelines and refuses loss of render history."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from render_manifest_helpers import (
    render_case,
    report_case,
    result_case,
    review_case,
    timeline_case,
)
from sqlalchemy import inspect, text

__all__ = ["render_case", "report_case", "result_case", "review_case", "timeline_case"]
TABLES = {"render_manifests", "render_artifacts", "render_report_links"}


def test_empty_render_migration_roundtrip_preserves_exact_existing_timeline(
    client, render_case, migrated
):
    command.downgrade(migrated, "0056")
    assert TABLES.isdisjoint(inspect(client.app.state.engine).get_table_names())
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case


def test_nonempty_render_downgrade_is_atomic_and_preserves_all_receipts(
    client, report_case, migrated
):
    value, artifact, report = report_case
    tables = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard render manifest history"):
        command.downgrade(migrated, "0055")
    assert set(inspect(client.app.state.engine).get_table_names()) == tables
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value
    results = client.get(f"/api/v1/render-manifests/{value['id']}/results").json()
    assert results["artifact"] == artifact.model_dump(mode="json")
    assert results["report"] == report.model_dump(mode="json")
    command.check(migrated)


def test_offline_postgres_has_exact_scope_append_only_and_truncate_guards():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0056:0057", sql=True)
    sql = output.getvalue()
    for table in TABLES:
        assert f"CREATE TABLE {table}" in sql
        assert f"BEFORE UPDATE OR DELETE ON {table}" in sql
        assert f"BEFORE TRUNCATE ON {table}" in sql
        assert f"CREATE FUNCTION immutable_{table}_insert_guard" in sql
    assert "t.sealed" in sql and "d.id=NEW.dependency_manifest_id" in sql
    assert "f.rights_confirmed" in sql and "f.media_type IN ('font/ttf','font/otf')" in sql
    assert "f.object_key=NEW.object_key AND f.checksum=NEW.checksum" in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0057:0056", sql=True)
