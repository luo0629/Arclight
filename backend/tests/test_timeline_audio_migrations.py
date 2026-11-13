from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import inspect, text
from test_continuity_reviews import review_case
from test_timeline_audio import audio_timeline_case
from test_timeline_saves import save_url, saved_case, saved_timeline
from test_timelines import timeline_case

__all__ = ["review_case", "saved_case", "timeline_case", "audio_timeline_case"]


def test_0068_empty_audio_roundtrip_preserves_exact_legacy_history(client, saved_case, migrated):
    original, body, _ = saved_case
    saved = saved_timeline(client, original, body)
    command.downgrade(migrated, "0067")
    assert "timeline_clip_audio" not in inspect(client.app.state.engine).get_table_names()
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT count(*) FROM timelines")) == 2
    command.upgrade(migrated, "head")
    command.check(migrated)
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(saved)).json() == saved
    assert saved_timeline(client, original, body) == saved


def test_0068_nonempty_audio_cannot_be_downgraded_or_discarded(
    client, audio_timeline_case, migrated
):
    original, body, _ = audio_timeline_case
    saved = saved_timeline(client, original, body)
    before = set(inspect(client.app.state.engine).get_table_names())
    with client.app.state.session_factory() as db:
        head = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot discard timeline audio history"):
        command.downgrade(migrated, "0067")
    assert set(inspect(client.app.state.engine).get_table_names()) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == head
        assert db.scalar(text("SELECT count(*) FROM timeline_clip_audio")) == 3
    assert client.get(save_url(original)).json() == original
    assert client.get(save_url(saved)).json() == saved
    command.check(migrated)


def test_0068_offline_postgres_has_music_scope_seal_and_history_guards():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://offline:offline@localhost/offline"
    )
    command.upgrade(config, "0067:0068", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE timeline_clip_audio" in sql
    assert "FOREIGN KEY(id) REFERENCES timeline_clips" in sql
    assert "a.asset_type='MUSIC'" in sql
    assert "t.kind IN ('MUSIC','SFX') AND a.asset_type='AUDIO'" in sql
    assert "timeline_audio_clip_count" in sql
    assert "incomplete timeline audio" in sql
    assert "BEFORE TRUNCATE" in sql
    assert "PERFORM 1 FROM timelines WHERE id=NEW.timeline_id FOR UPDATE" in sql
    assert "CREATE OR REPLACE FUNCTION immutable_timeline_clips_insert_guard" in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0068:0067", sql=True)
