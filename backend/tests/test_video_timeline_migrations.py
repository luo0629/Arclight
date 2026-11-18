from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from render_manifest_helpers import manifest, render_case, review_case, timeline_case
from sqlalchemy import text

from arclight.domain.versions import digest

__all__ = ["render_case", "review_case", "timeline_case"]


def test_still_timeline_and_manifest_from_0069_survive_0070_roundtrip(
    client, render_case, migrated
):
    command.downgrade(migrated, "0069")
    frozen, _ = manifest(client, render_case)
    assert frozen["snapshot"]["software"]["renderer_version"] == "arclight-pyav/3"
    assert frozen["content_sha256"] == digest(frozen["snapshot"])
    assert all("video" not in clip for clip in frozen["snapshot"]["timeline"]["tracks"][0]["clips"])

    for direction, revision in ((command.upgrade, "0070"), (command.downgrade, "0069")):
        direction(migrated, revision)
        with client.app.state.session_factory() as db:
            assert db.scalar(text("SELECT version_num FROM alembic_version")) == revision
        assert client.get(f"/api/v1/timelines/{render_case['id']}").json() == render_case
        assert client.get(f"/api/v1/render-manifests/{frozen['id']}").json() == frozen
    command.upgrade(migrated, "head")
    command.check(migrated)


def test_video_guards_render_offline_postgres_but_downgrade_requires_history_check():
    output = StringIO()
    config = Config(str(Path(__file__).parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://offline:offline@localhost/offline"
    command.upgrade(config, "0069:0070", sql=True)
    sql = output.getvalue()
    for fragment in (
        "CREATE OR REPLACE FUNCTION immutable_timeline_clips_insert_guard()",
        "CAST(NEW.shot_id AS TEXT)=(v.metadata_json #>> '{video_source,shot_id}')",
        "j.episode_id=e.id",
        "a.current_version_id=v.id AND NOT v.stale",
        "CREATE OR REPLACE FUNCTION render_report_machine_insert_guard()",
        "#>> '{asset_type}')='VIDEO' AND q.handler='video_temporal_review'",
        "#>> '{asset_type}') IN ('IMAGE','SHOT_IMAGE') AND q.handler='continuity_shot'",
    ):
        assert fragment in sql
    with pytest.raises(RuntimeError, match="requires an online history check"):
        command.downgrade(config, "0070:0069", sql=True)
