"""0054 preserves prior production history and refuses a lossy timeline downgrade."""

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, select
from test_api_workflow import start
from test_continuity_reviews import review_case  # noqa: F401
from test_timelines import created_timeline, timeline_case  # noqa: F401

from arclight.persistence.models import OutboxEvent
from arclight.persistence.timelines import Timeline

__all__ = ["timeline_case", "review_case"]


def test_empty_timeline_migration_roundtrip_preserves_existing_workflow(client, episode, migrated):
    start(client, episode)
    with client.app.state.session_factory() as db:
        event = db.scalar(select(OutboxEvent))
        identity, run_id, revision = event.id, event.run_id, event.revision
    command.downgrade(migrated, "0053")
    assert "timelines" not in inspect(client.app.state.engine).get_table_names()
    command.upgrade(migrated, "head")
    command.check(migrated)
    with client.app.state.session_factory() as db:
        event = db.get(OutboxEvent, identity)
        assert (event.run_id, event.revision) == (run_id, revision)


def test_nonempty_timeline_downgrade_is_rejected_before_any_table_is_dropped(
    client, episode, timeline_case, migrated
):
    body, _ = timeline_case
    original = created_timeline(client, episode, body)
    with pytest.raises(RuntimeError, match="Cannot discard timeline history"):
        command.downgrade(migrated, "0053")
    tables = inspect(client.app.state.engine).get_table_names()
    assert {"timelines", "timeline_tracks", "timeline_clips"} <= set(tables)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(Timeline)).sealed is True
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original
    command.check(migrated)


def test_offline_postgres_migration_contains_scope_immutability_and_truncate_guards():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0053:0054", sql=True)
    sql = output.getvalue()
    for table in ("timelines", "timeline_tracks", "timeline_clips"):
        assert f"CREATE TABLE {table}" in sql
        assert f"BEFORE TRUNCATE ON {table}" in sql
        assert f"CREATE FUNCTION immutable_{table}_insert_guard" in sql
    assert "IMMUTABLE_HISTORY" in sql and "source_asset_version_id" in sql
    assert "review_storyboard_id" in sql and "audio_version_id" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0054:0053", sql=True)
