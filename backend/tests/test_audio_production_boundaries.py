"""Authorization, exact invalidation, migration and concurrent history boundaries."""

from copy import deepcopy
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError
from test_audio_production import audio_source, output_audio, prepare, snapshot
from test_collaboration import register

from arclight.persistence.models import AssetVersion, Episode, Project, ProjectMember

__all__ = ["audio_source"]


def test_authorization_revocation_and_archive_cover_all_audio_endpoints(client, audio_source):
    episode, script, row, lines = audio_source
    base = f"/api/v1/audio-snapshots/{row['id']}"
    line_url, asset, manifest = prepare(client, audio_source)
    audio_id = output_audio(client, audio_source, asset, manifest)
    payload = {"asset_version_id": audio_id}
    inputs = {"target_asset_id": asset, "creation_key": str(uuid4())}
    owner_header = client.headers["Authorization"]
    member = register(client, "audio-viewer@example.com")
    paths = [
        base,
        base + "/lines",
        line_url + "/bindings",
        f"/api/v1/episodes/{episode['id']}/audio-snapshots",
    ]
    for path in paths:
        assert client.get(path).status_code == 404
    assert client.post(line_url + "/bindings", json=payload).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        member_id = membership.id
    for path in paths:
        assert client.get(path).status_code == 200
    assert snapshot(client, episode, script).status_code == 403
    assert client.post(line_url + "/inputs", json=inputs).status_code == 403
    assert client.post(line_url + "/bindings", json=payload).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, member_id).role = "editor"
    assert client.post(line_url + "/bindings", json=payload).status_code == 201
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, member_id))
    assert client.get(base).status_code == 404
    assert client.post(line_url + "/bindings", json=payload).status_code == 404
    client.headers["Authorization"] = owner_header
    for model, identity, field, value, original in [
        (Episode, episode["id"], "status", "ARCHIVED", "DRAFT"),
        (Project, episode["project_id"], "archived", True, False),
    ]:
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), field, value)
        assert client.get(base).json() == row
        assert snapshot(client, episode, script).status_code == 409
        assert client.post(line_url + "/inputs", json=inputs).status_code == 409
        assert client.post(line_url + "/bindings", json=payload).status_code == 409
        with client.app.state.session_factory.begin() as db:
            setattr(db.get(model, UUID(identity)), field, original)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).deleted_at = datetime.now(UTC)
    for path in paths:
        assert client.get(path).status_code == 404


def test_approved_dialogue_edit_invalidates_only_its_audio_and_late_outputs(client, audio_source):
    episode, script, row, lines = audio_source
    pairs = [prepare(client, audio_source, index) for index in (0, 1)]
    versions = [output_audio(client, audio_source, asset, manifest) for _, asset, manifest in pairs]
    for (url, _, _), identity in zip(pairs, versions, strict=True):
        assert (
            client.post(url + "/bindings", json={"asset_version_id": identity}).status_code == 201
        )
    late_url, late_asset, late_manifest = prepare(client, audio_source)
    content = deepcopy(script["content_json"])
    content["scenes"][0]["dialogue"][0]["text"] = "新对白。"
    prefix = f"/api/v1/episodes/{episode['id']}/scripts"
    response = client.post(
        prefix, json={"expected_version_no": 1, "base_version_id": script["id"], "content": content}
    )
    assert response.status_code == 201, response.text
    second = response.json()
    approved = client.post(prefix + f"/{second['id']}/review", json={"decision": "APPROVE"})
    assert approved.status_code == 200, approved.text
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, UUID(versions[0])).stale
        assert not db.get(AssetVersion, UUID(versions[1])).stale
    late_id = output_audio(client, audio_source, late_asset, late_manifest)
    with client.app.state.session_factory() as db:
        assert db.get(AssetVersion, UUID(late_id)).stale
    assert (
        client.post(late_url + "/bindings", json={"asset_version_id": late_id}).status_code == 409
    )
    assert client.get(f"/api/v1/audio-snapshots/{row['id']}/lines").json() == lines
    assert len(client.get(pairs[0][0] + "/bindings").json()) == 1


def test_missing_line_and_bad_target_do_not_capture_inputs(client, audio_source):
    _, _, row, lines = audio_source
    url, asset, _ = prepare(client, audio_source)
    assert (
        client.post(
            url.replace(lines[0]["line_id"], "absent") + "/inputs",
            json={"creation_key": str(uuid4()), "target_asset_id": asset},
        ).status_code
        == 404
    )
    with client.app.state.session_factory.begin() as db:
        from arclight.persistence.models import VisualAsset

        image = VisualAsset(
            project_id=UUID(audio_source[0]["project_id"]), asset_type="IMAGE", name="非音频"
        )
        db.add(image)
        db.flush()
        asset = str(image.id)
    result = client.post(
        url + "/inputs", json={"creation_key": str(uuid4()), "target_asset_id": asset}
    )
    assert result.status_code == 422


def test_sqlite_replace_cannot_replace_snapshot_or_binding(client, audio_source):
    engine = client.app.state.session_factory.kw["bind"]
    if engine.dialect.name != "sqlite":
        pytest.skip("SQLite INSERT OR REPLACE boundary")
    url, asset, manifest = prepare(client, audio_source)
    identity = output_audio(client, audio_source, asset, manifest)
    assert client.post(url + "/bindings", json={"asset_version_id": identity}).status_code == 201
    for table in ("audio_snapshots", "audio_line_bindings"):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(text(f"INSERT OR REPLACE INTO {table} SELECT * FROM {table}"))


def test_postgres_truncate_preserves_audio_history(client, audio_source):
    engine = client.app.state.session_factory.kw["bind"]
    if engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL TRUNCATE boundary")
    for table in ("audio_snapshots", "audio_line_bindings"):
        with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), engine.begin() as db:
            db.execute(text(f"TRUNCATE {table} CASCADE"))


def test_offline_migration_emits_guards_and_refuses_unchecked_downgrade():
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+psycopg://unused:unused@localhost/unused"
    command.upgrade(config, "0061:0062", sql=True)
    sql = output.getvalue()
    for table in ("audio_snapshots", "audio_line_bindings"):
        assert f"CREATE TABLE {table}" in sql
        assert f"BEFORE TRUNCATE ON {table}" in sql
        assert f"CREATE FUNCTION immutable_{table}_insert_guard" in sql
    with pytest.raises(RuntimeError, match="online history check"):
        command.downgrade(config, "0062:0061", sql=True)


@pytest.mark.parametrize("count", [0, 105])
def test_snapshot_pagination_covers_empty_and_more_than_one_page(client, episode, count):
    from test_text_contract_v2 import legacy_script, save_script

    content = legacy_script()
    content["scenes"][0]["dialogue"] = [
        {"speaker": "主角", "text": f"第{index}句。"} for index in range(min(count, 60))
    ]
    if count > 60:
        second = deepcopy(content["scenes"][0])
        second["scene_no"] = 2
        second["dialogue"] = [
            {"speaker": "主角", "text": f"第{index}句。"} for index in range(60, count)
        ]
        content["scenes"].append(second)
    script = save_script(client, episode, content)
    response = snapshot(client, episode, script)
    assert response.status_code == 201, response.text
    row = response.json()
    assert row["line_count"] == count
    path = f"/api/v1/audio-snapshots/{row['id']}/lines"
    first = client.get(path).json()
    second = client.get(path + "?offset=100").json()
    assert len(first) == min(count, 100)
    assert len(second) == max(count - 100, 0)
    assert len({line["line_id"] for line in first + second}) == count
    assert [line["sequence_no"] for line in first + second] == (
        list(range(1, min(count, 60) + 1)) + list(range(1, max(count - 60, 0) + 1))
    )
