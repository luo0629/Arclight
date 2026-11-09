"""0055 preserves existing frozen timelines and refuses loss of save ancestry."""

from io import StringIO
from pathlib import Path
from uuid import UUID

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from test_continuity_reviews import review_case  # noqa: F401
from test_timeline_saves import save_url, saved_case, saved_timeline  # noqa: F401
from test_timelines import timeline_case  # noqa: F401

from arclight.persistence.timelines import Timeline

__all__ = ["review_case", "timeline_case", "saved_case"]


def test_0055_roundtrip_keeps_preexisting_timeline_and_frozen_children(
    client, saved_case, migrated
):
    original, _, _ = saved_case
    command.downgrade(migrated, "0054")
    assert "timeline_revisions" not in inspect(client.app.state.engine).get_table_names()
    with client.app.state.session_factory() as db:
        assert db.get(Timeline, UUID(original["id"])).sealed
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(save_url(original)).json() == original


def test_0055_nonempty_downgrade_keeps_full_schema_head_and_both_versions(
    client, saved_case, migrated
):
    original, body, _ = saved_case
    successor = saved_timeline(client, original, body)
    before = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard timeline revision history"):
        command.downgrade(migrated, "0054")
    assert set(inspect(client.app.state.engine).get_table_names()) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(successor)).json() == successor
    command.check(migrated)


def test_0055_offline_postgres_contains_scope_unique_and_immutable_guards():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0054:0055", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE timeline_revisions" in sql
    assert "UNIQUE (based_on_timeline_id)" in sql
    assert "BEFORE TRUNCATE ON timeline_revisions" in sql
    assert "n.version_no=b.version_no+1" in sql and "IMMUTABLE_HISTORY" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0055:0054", sql=True)
