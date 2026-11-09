from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from test_continuity_reviews import review_case
from test_timeline_saves import save_url, saved_case, saved_timeline
from test_timeline_visuals import visual_case
from test_timelines import timeline_case

__all__ = ["review_case", "saved_case", "timeline_case", "visual_case"]


def test_visual_upgrade_roundtrip_preserves_original_schema1_bytes_and_checksum(
    client, saved_case, migrated
):
    original, legacy_body, _ = saved_case
    legacy = saved_timeline(client, original, legacy_body)
    command.downgrade(migrated, "0055")
    assert "timeline_clip_visuals" not in inspect(client.app.state.engine).get_table_names()
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT count(*) FROM timelines")) == 2
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(legacy)).json() == legacy
    assert saved_timeline(client, original, legacy_body) == legacy


def test_nonempty_visual_downgrade_keeps_exact_head_schema_and_all_history(
    client, visual_case, migrated
):
    original, body = visual_case
    value = saved_timeline(client, original, body)
    before = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard timeline visual history"):
        command.downgrade(migrated, "0055")
    assert set(inspect(client.app.state.engine).get_table_names()) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(value)).json() == value
    command.check(migrated)


def test_offline_postgres_has_visual_scope_sealing_and_immutable_guards():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0055:0056", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE timeline_clip_visuals" in sql
    assert "FOREIGN KEY(id) REFERENCES timeline_clips" in sql
    assert "t.kind='VIDEO' AND NOT l.sealed" in sql
    assert "BEFORE TRUNCATE" in sql
    assert "incomplete timeline visuals" in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0056:0055", sql=True)
