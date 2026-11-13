"""Subtitle production through saved timelines, real WAV receipts and review APIs."""

import hashlib
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_audio_production import output_audio, prepare, snapshot
from test_audio_version_lifecycle import approve_audio
from test_model_services import account

from arclight.core.errors import DomainError
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    ProjectMember,
    StoredFile,
    Storyboard,
    VisualAsset,
)
from arclight.persistence.subtitles import SubtitleProduction
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "review_case", "timeline_case", "subtitle_source", "subtitle_case"]


def generation_url(case):
    return f"/api/v1/episodes/{case['episode']['id']}/subtitle-tracks"


def generate(client, case, **overrides):
    return client.post(generation_url(case), json={**case["payload"], **overrides})


def version_url(case, track=None):
    track = case["track"] if track is None else track
    return (
        f"/api/v1/projects/{case['episode']['project_id']}/assets/"
        f"{track['asset_id']}/versions/{track['id']}"
    )


def save_timeline(client, timeline, snapshot):
    response = client.patch(
        f"/api/v1/timelines/{timeline['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest.fixture
def subtitle_source(client, episode, render_case):
    with client.app.state.session_factory() as db:
        board = db.get(Storyboard, UUID(render_case["snapshot"]["storyboard_id"]))
        script = {"id": str(board.script_version_id)}
    response = snapshot(client, episode, script)
    assert response.status_code == 201, response.text
    audio_snapshot = response.json()
    response = client.get(f"/api/v1/audio-snapshots/{audio_snapshot['id']}/lines")
    assert response.status_code == 200, response.text
    lines = response.json()
    assert len(lines) >= 2
    source = episode, script, audio_snapshot, lines
    audio = []
    clips = []
    for index, start in enumerate((125, 875)):
        line_url, asset_id, manifest_id = prepare(client, source, index)
        identity = output_audio(client, source, asset_id, manifest_id)
        response = client.post(line_url + "/bindings", json={"asset_version_id": identity})
        assert response.status_code == 201, response.text
        binding = response.json()
        url = f"/api/v1/projects/{episode['project_id']}/assets/{asset_id}/versions/{identity}"
        approved = approve_audio(client, url)
        audio.append({**approved, "url": url, "binding": binding})
        clips.append(
            {
                "id": str(uuid4()),
                "ordinal": index,
                "source_asset_version_id": identity,
                "source_sha256": approved["checksum"],
                "start_ms": start,
                "duration_ms": 100,
                "source_start_ms": 0,
                "script_version_id": script["id"],
                "line_id": lines[index]["line_id"],
            }
        )
    snapshot_json = deepcopy(render_case["snapshot"])
    next(track for track in snapshot_json["tracks"] if track["kind"] == "VOICE")["clips"] = clips
    timeline = save_timeline(client, render_case, snapshot_json)
    return {
        "episode": episode,
        "script": script,
        "snapshot": audio_snapshot,
        "lines": lines,
        "audio": audio,
        "timeline": timeline,
        "original_timeline": render_case,
        "payload": {"creation_key": str(uuid4()), "timeline_id": timeline["id"]},
    }


@pytest.fixture
def subtitle_case(client, subtitle_source):
    response = generate(client, subtitle_source)
    assert response.status_code == 201, response.text
    return {**subtitle_source, "track": response.json()}


def counts(client):
    with client.app.state.session_factory() as db:
        return tuple(
            db.scalar(select(func.count()).select_from(model))
            for model in (
                VisualAsset,
                AssetVersion,
                DependencyManifest,
                StoredFile,
                SubtitleProduction,
            )
        )


def test_actual_timeline_cues_exact_sources_and_utf8_srt(client, subtitle_case):
    case, track = subtitle_case, subtitle_case["track"]
    assert track["timeline_id"] == case["timeline"]["id"]
    assert track["status"] == "DRAFT" and track["stale"] is False
    assert track["version_no"] == 1
    assert [(cue["start_ms"], cue["duration_ms"]) for cue in track["cues"]] == [
        (125, 100),
        (875, 100),
    ]
    for index, cue in enumerate(track["cues"]):
        assert cue["cue_no"] == index + 1
        assert cue["text"] == case["lines"][index]["text"]
        assert cue["speaker"] == case["lines"][index]["speaker"]
        assert cue["line_type"] == case["lines"][index]["line_type"]
        assert cue["line_id"] == case["lines"][index]["line_id"]
        assert cue["script_version_id"] == case["script"]["id"]
        assert cue["audio_asset_version_id"] == case["audio"][index]["id"]
        assert cue["audio_sha256"] == case["audio"][index]["checksum"]
        assert cue["audio_snapshot_id"] == case["snapshot"]["id"]
        assert cue["source_start_ms"] == 0
        assert cue["audio_binding_id"] and cue["audio_file_id"] and cue["script_sha256"]
        clip = track["clips"][index]
        assert clip["source_asset_version_id"] == track["id"]
        assert clip["source_sha256"] == track["checksum"]
        assert clip["source_start_ms"] == 0
        assert clip["audio_version_id"] == cue["audio_asset_version_id"]
    response = client.get(f"/api/v1/subtitle-tracks/{track['id']}/srt")
    assert response.status_code == 200, response.text
    expected = "".join(
        f"{index + 1}\n00:00:00,{start:03d} --> 00:00:00,{start + 100:03d}\n"
        f"{case['lines'][index]['text']}\n\n"
        for index, start in enumerate((125, 875))
    ).encode("utf-8")
    assert response.content == expected
    assert hashlib.sha256(response.content).hexdigest() == track["checksum"]
    assert "attachment" in response.headers["content-disposition"]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(track["id"]))
        asset = db.get(VisualAsset, version.asset_id)
        file = db.get(StoredFile, UUID(track["file_id"]))
        production = db.scalar(
            select(SubtitleProduction).where(SubtitleProduction.asset_version_id == version.id)
        )
        assert asset.current_version_id is None
        assert file.asset_version_id == version.id
        assert file.rights_confirmed and file.byte_size == len(expected)
        assert file.checksum == track["checksum"] and production.file_id == file.id
        assert (
            ObjectStorage(client.app.state.settings).read(file.object_key, None, file.checksum)
            == expected
        )
        assert "object_key" not in track and "bucket" not in track
    original = client.get(f"/api/v1/timelines/{case['timeline']['id']}")
    assert original.status_code == 200
    assert original.json() == case["timeline"]


def test_replay_and_history_are_read_only_and_different_request_conflicts(client, subtitle_case):
    case, track = subtitle_case, subtitle_case["track"]
    before = counts(client)
    response = generate(client, case)
    assert response.status_code == 201 and response.json() == track
    assert client.get(f"/api/v1/subtitle-tracks/{track['id']}").json() == track
    response = client.get(generation_url(case))
    assert response.status_code == 200 and response.json() == [track]
    assert client.get(f"/api/v1/subtitle-tracks/{track['id']}/srt").status_code == 200
    assert counts(client) == before
    # A different existing, valid source cannot reuse the logical creation key.
    response = generate(client, case, timeline_id=case["original_timeline"]["id"])
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "IDEMPOTENCY_CONFLICT"
    assert counts(client) == before


def test_new_version_requires_cas_and_preserves_old_file(client, subtitle_case):
    case, original = subtitle_case, subtitle_case["track"]
    old_srt = client.get(f"/api/v1/subtitle-tracks/{original['id']}/srt").content
    asset_url = f"/api/v1/projects/{case['episode']['project_id']}/assets/{original['asset_id']}"
    asset = client.get(asset_url).json()
    before = counts(client)
    response = generate(
        client,
        case,
        creation_key=str(uuid4()),
        target_asset_id=original["asset_id"],
        expected_revision=asset["revision"] + 1,
    )
    assert response.status_code == 409
    assert counts(client) == before
    response = generate(
        client,
        case,
        creation_key=str(uuid4()),
        target_asset_id=original["asset_id"],
        expected_revision=asset["revision"],
    )
    assert response.status_code == 201, response.text
    fresh = response.json()
    assert fresh["asset_id"] == original["asset_id"] and fresh["version_no"] == 2
    assert fresh["id"] != original["id"] and fresh["file_id"] != original["file_id"]
    assert fresh["checksum"] == original["checksum"]
    assert client.get(f"/api/v1/subtitle-tracks/{original['id']}/srt").content == old_srt


def test_missing_voice_rejects_without_partial_production(client, subtitle_source):
    case = subtitle_source
    before = counts(client)
    response = generate(client, case, timeline_id=case["original_timeline"]["id"])
    assert response.status_code == 422, response.text
    assert counts(client) == before


@pytest.mark.parametrize("bad_state", ["DRAFT", "STALE", "ARCHIVED"])
def test_source_audio_lifecycle_rechecked_without_partial_output(
    client, subtitle_source, bad_state
):
    case = subtitle_source
    with client.app.state.session_factory.begin() as db:
        version = db.get(AssetVersion, UUID(case["audio"][0]["id"]))
        version.status = bad_state
        version.stale = bad_state == "STALE"
    before = counts(client)
    response = generate(client, case)
    assert response.status_code == 409, response.text
    assert counts(client) == before


def corrupt_object(client, key, data, media_type):
    """Simulate external storage damage without bypassing production immutability."""
    storage = ObjectStorage(client.app.state.settings)
    if storage.client is None:
        storage.path(key).write_bytes(data)
    else:
        storage.client.put_object(
            Bucket=storage.settings.s3_bucket, Key=key, Body=data, ContentType=media_type
        )


def test_corrupt_source_audio_rejected_but_old_subtitle_remains_readable(client, subtitle_case):
    case, track = subtitle_case, subtitle_case["track"]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(case["audio"][0]["id"]))
        key = version.object_key
    corrupt_object(client, key, b"corrupted", "audio/wav")
    before = counts(client)
    response = generate(client, case, creation_key=str(uuid4()))
    assert response.status_code == 409, response.text
    assert counts(client) == before
    assert client.get(f"/api/v1/subtitle-tracks/{track['id']}").status_code == 200
    assert client.get(f"/api/v1/subtitle-tracks/{track['id']}/srt").status_code == 200


def test_corrupt_srt_blocks_read_and_export(client, subtitle_case):
    track = subtitle_case["track"]
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(track["file_id"]))
        key = file.object_key
    corrupt_object(client, key, b"changed subtitle", "text/plain")
    for suffix in ("", "/srt"):
        response = client.get(f"/api/v1/subtitle-tracks/{track['id']}{suffix}")
        assert response.status_code == 409, response.text


def test_production_history_and_export_recheck_membership_and_deletion(client, subtitle_case):
    case, track = subtitle_case, subtitle_case["track"]
    paths = [
        generation_url(case),
        f"/api/v1/subtitle-tracks/{track['id']}",
        f"/api/v1/subtitle-tracks/{track['id']}/srt",
    ]
    owner_header = client.headers["Authorization"]
    member_id = account(client)
    for path in paths:
        assert client.get(path).status_code == 404
    with client.app.state.session_factory.begin() as db:
        member = ProjectMember(
            project_id=UUID(case["episode"]["project_id"]), user_id=UUID(member_id), role="viewer"
        )
        db.add(member)
        db.flush()
        membership_id = member.id
    for path in paths:
        assert client.get(path).status_code == 200
    assert generate(client, case, creation_key=str(uuid4())).status_code == 403
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, membership_id))
    for path in paths:
        assert client.get(path).status_code == 404
    client.headers["Authorization"] = owner_header
    project_url = f"/api/v1/projects/{case['episode']['project_id']}"
    project = client.get(project_url).json()
    assert client.patch(project_url, json={"archived": True}).status_code == 200
    for path in paths:
        assert client.get(path).status_code == 200
    assert generate(client, case, creation_key=str(uuid4())).status_code == 409
    response = client.request("DELETE", project_url, json={"confirm_name": project["name"]})
    assert response.status_code == 200, response.text
    for path in paths:
        assert client.get(path).status_code == 404


def test_wrong_episode_or_project_cannot_consume_source_timeline(client, subtitle_source):
    case = subtitle_source
    other = client.post(
        f"/api/v1/projects/{case['episode']['project_id']}/episodes",
        json={"episode_no": 2, "title": "另一集", "target_duration_ms": 30000},
    )
    assert other.status_code == 201, other.text
    before = counts(client)
    response = client.post(
        f"/api/v1/episodes/{other.json()['id']}/subtitle-tracks", json=case["payload"]
    )
    assert response.status_code == 422, response.text
    assert counts(client) == before
    project = client.post("/api/v1/projects", json={"name": "跨项目"})
    assert project.status_code == 201, project.text
    other = client.post(
        f"/api/v1/projects/{project.json()['id']}/episodes",
        json={"episode_no": 1, "title": "其他项目", "target_duration_ms": 30000},
    )
    assert other.status_code == 201, other.text
    response = client.post(
        f"/api/v1/episodes/{other.json()['id']}/subtitle-tracks", json=case["payload"]
    )
    assert response.status_code in {404, 422}, response.text
    assert counts(client) == before


def test_transaction_failure_removes_partial_file_and_retry_uses_same_key(
    client, subtitle_source, monkeypatch
):
    from arclight.application import subtitles

    case = subtitle_source
    before = counts(client)
    original_attach = subtitles.attach_output
    written = []
    original_write = subtitles.write_object

    def record_write(db, settings, project_id, version_id, data, media_type):
        written.append(subtitles.object_key(project_id, version_id, settings))
        return original_write(db, settings, project_id, version_id, data, media_type)

    def fail_attach(*args, **kwargs):
        raise DomainError("TEST_TRANSACTION_FAILURE", "隔离测试模拟事务失败", 409)

    monkeypatch.setattr(subtitles, "write_object", record_write)
    monkeypatch.setattr(subtitles, "attach_output", fail_attach)
    response = generate(client, case)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TEST_TRANSACTION_FAILURE"
    assert counts(client) == before
    assert len(written) == 1
    with pytest.raises(DomainError):
        ObjectStorage(client.app.state.settings).stat(written[0])
    monkeypatch.setattr(subtitles, "attach_output", original_attach)
    response = generate(client, case)
    assert response.status_code == 201, response.text
    assert len(written) == 2 and written[0] != written[1]
