"""Approved audio preparation through the actual API and immutable dependency service."""

import hashlib
import io
import math
import struct
import wave
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from alembic import command
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_text_contract_v2 import save_script

from arclight.application.audio_production import validate_audio_line
from arclight.application.dependencies import attach_output
from arclight.core.errors import DomainError
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import AssetVersion, StoredFile, VisualAsset
from arclight.storage.objects import ObjectStorage


def snapshot(client, episode, script, **changes):
    return client.post(
        f"/api/v1/episodes/{episode['id']}/audio-snapshots",
        json={
            "creation_key": str(uuid4()),
            "script_version_id": script["id"],
            **changes,
        },
    )


@pytest.fixture
def audio_source(client, episode):
    script = save_script(client, episode)
    response = snapshot(client, episode, script)
    assert response.status_code == 201, response.text
    row = response.json()
    lines = client.get(f"/api/v1/audio-snapshots/{row['id']}/lines").json()
    return episode, script, row, lines


def wave_bytes():
    stream = io.BytesIO()
    with wave.open(stream, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(24000)
        output.writeframes(
            b"".join(struct.pack("<h", int(1000 * math.sin(i / 20))) for i in range(2400))
        )
    return stream.getvalue()


def prepare(client, source, index=0, **overrides):
    episode, _, row, lines = source
    with client.app.state.session_factory.begin() as db:
        asset = VisualAsset(project_id=UUID(episode["project_id"]), asset_type="AUDIO", name="台词")
        db.add(asset)
        db.flush()
        identity = str(asset.id)
    url = f"/api/v1/audio-snapshots/{row['id']}/lines/{lines[index]['line_id']}"
    payload = {"creation_key": str(uuid4()), "target_asset_id": identity, **overrides}
    response = client.post(url + "/inputs", json=payload)
    assert response.status_code == 201, response.text
    assert client.post(url + "/inputs", json=payload).json() == response.json()
    return url, identity, response.json()["dependency_manifest_id"]


def output_audio(client, source, asset_id, manifest_id, *, data=None, rights=True, attach=True):
    """Offline producer: actual bytes and original manifest, without approval shortcuts."""
    data = wave_bytes() if data is None else data
    checksum = hashlib.sha256(data).hexdigest()
    project_id = UUID(source[0]["project_id"])
    key = f"audio01/{project_id}/{uuid4()}.wav"
    ObjectStorage(client.app.state.settings).put(key, data, "audio/wav")
    with client.app.state.session_factory.begin() as db:
        version = AssetVersion(
            asset_id=UUID(asset_id),
            version_no=1,
            object_key=key,
            checksum=checksum,
            status="DRAFT",
            created_by="service-owner",
            metadata_json={"dependency_manifest_id": manifest_id},
        )
        db.add(version)
        db.flush()
        if attach:
            attach_output(db, project_id, UUID(manifest_id), version)
        settings = client.app.state.settings
        file = StoredFile(
            project_id=project_id,
            asset_version_id=version.id,
            storage_backend=settings.object_storage_backend.upper(),
            bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
            object_key=key,
            checksum=checksum,
            byte_size=len(data),
            media_type="audio/wav",
            original_name="original-test.wav",
            source_note="Original offline waveform",
            rights_note="Original test fixture",
            rights_confirmed=rights,
            created_by="service-owner",
        )
        db.add(file)
        db.flush()
        return str(version.id)


def test_approved_snapshot_freezes_dialogue_narration_and_paginates(client, audio_source):
    episode, script, row, lines = audio_source
    assert row["line_count"] == 2
    assert [line["line_type"] for line in lines] == ["dialogue", "narration"]
    assert [line["sequence_no"] for line in lines] == [1, 2]
    generic = client.get(
        f"/api/v1/episodes/{episode['id']}/dialogue-lines",
        params={"script_version_id": script["id"]},
    ).json()
    assert lines == [
        {k: v for k, v in line.items() if k != "script_version_id"} for line in generic
    ]
    url = f"/api/v1/audio-snapshots/{row['id']}"
    assert client.get(url).json() == row
    assert client.get(url + "/lines?limit=1&offset=1").json() == [lines[1]]
    assert client.get(url + "/lines?offset=2").json() == []
    assert client.get(url + "/lines?limit=101").status_code == 422
    assert client.get(f"/api/v1/episodes/{episode['id']}/audio-snapshots").json() == [row]
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(script["id"]))
        version.stale = True
    assert client.get(url + "/lines").json() == lines
    assert snapshot(client, episode, script).status_code == 409


def test_pending_script_remains_readable_but_not_producible(client, episode):
    script = save_script(client, episode, approve=False)
    assert (
        client.get(
            f"/api/v1/episodes/{episode['id']}/dialogue-lines",
            params={"script_version_id": script["id"]},
        ).status_code
        == 200
    )
    response = snapshot(client, episode, script)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "AUDIO_SCRIPT_NOT_APPROVED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioSnapshot)) == 0


def test_snapshot_replay_conflict_and_source_change_preserve_history(client, audio_source):
    episode, script, _, lines = audio_source
    key = str(uuid4())
    first = snapshot(client, episode, script, creation_key=key)
    assert snapshot(client, episode, script, creation_key=key).json() == first.json()
    assert snapshot(client, episode, {"id": str(uuid4())}, creation_key=key).status_code == 409
    content = deepcopy(script["content_json"])
    content["scenes"][0]["dialogue"][0]["text"] = "改变的台词。"
    saved = client.post(
        f"/api/v1/episodes/{episode['id']}/scripts",
        json={"expected_version_no": 1, "content": content},
    )
    assert saved.status_code == 201, saved.text
    assert client.get(f"/api/v1/audio-snapshots/{first.json()['id']}/lines").json() == lines


@pytest.mark.parametrize("problem", ["missing", "other_episode", "archived", "stale", "rejected"])
def test_snapshot_scope_and_lifecycle_gate(client, audio_source, problem):
    episode, script, _, _ = audio_source
    if problem == "missing":
        script = {"id": str(uuid4())}
    elif problem == "other_episode":
        response = client.post(
            f"/api/v1/projects/{episode['project_id']}/episodes",
            json={"episode_no": 2, "title": "其他集", "target_duration_ms": 30000},
        )
        episode = response.json()
    else:
        with client.app.state.session_factory.begin() as db:
            v = db.get(AssetVersion, UUID(script["id"]))
            if problem == "stale":
                v.stale = True
            else:
                v.status = problem.upper()
    assert snapshot(client, episode, script).status_code == (
        404 if problem in {"missing", "other_episode"} else 409
    )


def test_original_manifest_audio_binding_real_bytes_history_and_exact_line(client, audio_source):
    url, asset_id, manifest_id = prepare(client, audio_source)
    identity = output_audio(client, audio_source, asset_id, manifest_id)
    payload = {"asset_version_id": identity}
    script, row, lines = audio_source[1:]
    with (
        client.app.state.session_factory() as db,
        pytest.raises(DomainError, match="精确剧本台词"),
    ):
        validate_audio_line(db, UUID(identity), UUID(script["id"]), lines[0]["line_id"])
    response = client.post(url + "/bindings", json=payload)
    assert response.status_code == 201, response.text
    assert response.json()["duration_ms"] == 100
    assert client.post(url + "/bindings", json=payload).json() == response.json()
    assert client.get(url + "/bindings").json() == [response.json()]
    other = url.replace(lines[0]["line_id"], lines[1]["line_id"])
    assert client.post(other + "/bindings", json=payload).status_code == 409
    with client.app.state.session_factory() as db:
        validate_audio_line(db, UUID(identity), UUID(script["id"]), lines[0]["line_id"])
        with pytest.raises(DomainError):
            validate_audio_line(db, UUID(identity), UUID(script["id"]), lines[1]["line_id"])
        with pytest.raises(DomainError):
            validate_audio_line(db, UUID(identity), uuid4(), lines[0]["line_id"])
        assert db.get(AssetVersion, UUID(identity)).status == "DRAFT"
        assert db.get(VisualAsset, UUID(asset_id)).current_version_id is None
        assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == 1


@pytest.mark.parametrize(
    "problem", ["rights", "corrupt", "no_receipt", "wrong_line", "stale", "wrong_project"]
)
def test_binding_rejects_invalid_media_or_provenance_atomically(client, audio_source, problem):
    url, asset_id, manifest_id = prepare(client, audio_source)
    identity = output_audio(
        client,
        audio_source,
        asset_id,
        manifest_id,
        data=b"bad" if problem == "corrupt" else None,
        rights=problem != "rights",
        attach=problem != "no_receipt",
    )
    if problem == "wrong_line":
        url = url.replace(audio_source[3][0]["line_id"], audio_source[3][1]["line_id"])
    if problem == "stale":
        with client.app.state.session_factory.begin() as db:
            db.get(AssetVersion, UUID(identity)).stale = True
    if problem == "wrong_project":
        project = client.post("/api/v1/projects", json={"name": "其他"}).json()
        episode = client.post(
            f"/api/v1/projects/{project['id']}/episodes",
            json={"episode_no": 1, "title": "其他", "target_duration_ms": 30000},
        ).json()
        script = save_script(client, episode)
        row = snapshot(client, episode, script).json()
        line = client.get(f"/api/v1/audio-snapshots/{row['id']}/lines").json()[0]
        url = f"/api/v1/audio-snapshots/{row['id']}/lines/{line['line_id']}"
    result = client.post(url + "/bindings", json={"asset_version_id": identity})
    assert result.status_code in {404, 409, 422}, result.text
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioLineBinding)) == 0


@pytest.mark.parametrize("action", ["UPDATE", "DELETE"])
def test_snapshot_and_binding_history_cannot_be_rewritten(client, audio_source, action):
    url, asset, manifest = prepare(client, audio_source)
    identity = output_audio(client, audio_source, asset, manifest)
    assert client.post(url + "/bindings", json={"asset_version_id": identity}).status_code == 201
    for table in ("audio_snapshots", "audio_line_bindings"):
        statement = (
            f"UPDATE {table} SET created_by='tampered'"
            if action == "UPDATE"
            else f"DELETE FROM {table}"
        )
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text(statement))


def test_audio_migration_refuses_discarding_snapshot_history(client, audio_source, migrated):
    with pytest.raises(RuntimeError, match="audio production history"):
        command.downgrade(migrated, "0061")
    assert client.get(f"/api/v1/audio-snapshots/{audio_source[2]['id']}").status_code == 200


def test_empty_migration_roundtrip(client, migrated):
    command.downgrade(migrated, "0061")
    command.upgrade(migrated, "head")
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AudioSnapshot)) == 0


def test_postgres_duplicate_snapshot_and_binding_are_serialized(client, audio_source):
    if client.app.state.session_factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("PostgreSQL row locking required")
    episode, script, _, _ = audio_source
    key = str(uuid4())
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(lambda _: snapshot(client, episode, script, creation_key=key), range(2))
        )
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
    url, asset, manifest = prepare(client, audio_source)
    identity = output_audio(client, audio_source, asset, manifest)
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(
            pool.map(
                lambda _: client.post(url + "/bindings", json={"asset_version_id": identity}),
                range(2),
            )
        )
    assert [r.status_code for r in responses] == [201, 201]
    assert responses[0].json() == responses[1].json()
