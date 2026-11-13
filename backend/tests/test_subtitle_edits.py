"""Subtitle editing appends private versions and reuses review, timeline and permission gates."""

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import render_case, review_case, timeline_case
from test_audio_version_lifecycle import read_ok, selection_payload
from test_concurrency import require_postgres
from test_model_services import account
from test_subtitle_lifecycle import approve_subtitle, asset_url, select_subtitle
from test_subtitle_production import (
    corrupt_object,
    counts,
    save_timeline,
    subtitle_case,
    subtitle_source,
    version_url,
)

from arclight.application.subtitles import edit_subtitles
from arclight.core.errors import DomainError
from arclight.domain.subtitles import SubtitleEdit
from arclight.persistence.models import AssetVersion, ProjectMember, VisualAsset
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "review_case", "timeline_case", "subtitle_source", "subtitle_case"]


def edit_url(track):
    return f"/api/v1/subtitle-tracks/{track['id']}/edits"


def edit_payload(client, case, changes, **overrides):
    return {
        "creation_key": str(uuid4()),
        "expected_revision": read_ok(client, asset_url(case))["revision"],
        "changes": changes,
        **overrides,
    }


def edit_subtitle(client, case, changes, *, parent=None, **overrides):
    return client.post(
        edit_url(parent or case["track"]), json=edit_payload(client, case, changes, **overrides)
    )


def require_edit(client, case, changes, *, parent=None):
    response = edit_subtitle(client, case, changes, parent=parent)
    assert response.status_code == 201, response.text
    return response.json()


def track_url(track):
    return f"/api/v1/subtitle-tracks/{track['id']}"


def test_subtitle_text_and_time_edit_appends_draft_without_changing_sources(client, subtitle_case):
    case, original = subtitle_case, deepcopy(subtitle_case["track"])
    old_srt = client.get(track_url(original) + "/srt").content
    fresh = require_edit(
        client,
        case,
        [{"cue_no": 1, "text": "修改后的中文字幕", "start_ms": 150, "end_ms": 300}],
    )
    assert fresh["schema_version"] == "subtitle-track/2"
    assert fresh["edit_source_version_id"] == original["id"]
    assert fresh["asset_id"] == original["asset_id"] and fresh["version_no"] == 2
    assert fresh["id"] != original["id"] and fresh["file_id"] != original["file_id"]
    assert fresh["status"] == "DRAFT" and not fresh["stale"]
    assert fresh["source_cues"] == original["cues"]
    assert fresh["cues"][1] == original["cues"][1]
    first = fresh["cues"][0]
    assert (first["text"], first["start_ms"], first["duration_ms"]) == (
        "修改后的中文字幕",
        150,
        150,
    )
    for field, value in original["cues"][0].items():
        if field not in {"text", "start_ms", "duration_ms"}:
            assert first[field] == value
    assert read_ok(client, asset_url(case))["current_version_id"] is None
    assert client.get(track_url(original)).json() == original
    assert client.get(track_url(original) + "/srt").content == old_srt
    exported = client.get(track_url(fresh) + "/srt")
    assert exported.status_code == 200, exported.text
    assert "00:00:00,150 --> 00:00:00,300\n修改后的中文字幕" in exported.text
    assert hashlib.sha256(exported.content).hexdigest() == fresh["checksum"]


def test_subtitle_reediting_a_draft_inherits_display_layer_and_original_source(
    client, subtitle_case
):
    case = subtitle_case
    first = require_edit(
        client, case, [{"cue_no": 1, "text": "保留第一句修改", "start_ms": 200, "end_ms": 400}]
    )
    second = require_edit(client, case, [{"cue_no": 2, "text": "新增第二句修改"}], parent=first)
    assert second["edit_source_version_id"] == first["id"]
    assert second["version_no"] == 3 and second["status"] == "DRAFT"
    assert second["cues"][0] == first["cues"][0]
    assert second["cues"][1]["text"] == "新增第二句修改"
    assert second["source_cues"] == case["track"]["cues"]
    assert client.get(track_url(first)).json() == first


def test_subtitle_audio_sync_restores_source_voice_range_and_keeps_edited_text(
    client, subtitle_case
):
    case = subtitle_case
    manual = require_edit(
        client, case, [{"cue_no": 1, "text": "保留人工措辞", "start_ms": 200, "end_ms": 400}]
    )
    synced = require_edit(client, case, [{"cue_no": 1, "sync_to_audio": True}], parent=manual)
    source = case["track"]["cues"][0]
    actual = synced["cues"][0]
    assert actual["text"] == "保留人工措辞"
    assert (actual["start_ms"], actual["duration_ms"]) == (
        source["start_ms"],
        source["duration_ms"],
    )
    assert synced["cues"][1] == manual["cues"][1]
    assert synced["source_cues"] == case["track"]["cues"]


def test_subtitle_edits_allow_touching_boundaries_and_exact_timeline_end(client, subtitle_case):
    case = subtitle_case
    end = case["timeline"]["snapshot"]["duration_ms"]
    fresh = require_edit(
        client,
        case,
        [
            {"cue_no": 1, "start_ms": 0, "end_ms": 875},
            {"cue_no": 2, "start_ms": 875, "end_ms": end},
        ],
    )
    assert fresh["cues"][0]["start_ms"] == 0
    assert fresh["cues"][0]["duration_ms"] == 875
    assert fresh["cues"][1]["start_ms"] + fresh["cues"][1]["duration_ms"] == end


@pytest.mark.parametrize("failure", ["order", "overlap", "timeline-end", "unknown-cue"])
def test_subtitle_invalid_display_range_or_identity_rolls_back_atomically(
    client, subtitle_case, failure
):
    case = subtitle_case
    changes = {
        "order": [
            {"cue_no": 1, "start_ms": 900, "end_ms": 950},
            {"cue_no": 2, "start_ms": 100, "end_ms": 150},
        ],
        "overlap": [{"cue_no": 1, "start_ms": 125, "end_ms": 876}],
        "timeline-end": [
            {
                "cue_no": 2,
                "start_ms": 875,
                "end_ms": case["timeline"]["snapshot"]["duration_ms"] + 1,
            }
        ],
        "unknown-cue": [{"cue_no": 3, "text": "不存在的第三句"}],
    }[failure]
    before, original = counts(client), deepcopy(case["track"])
    response = edit_subtitle(client, case, changes)
    assert response.status_code == 422, response.text
    assert counts(client) == before
    assert client.get(track_url(original)).json() == original


def test_subtitle_edit_replay_ignores_consumed_revision_but_rejects_changed_payload(
    client, subtitle_case
):
    case = subtitle_case
    payload = edit_payload(client, case, [{"cue_no": 1, "text": "重放字幕"}])
    response = client.post(edit_url(case["track"]), json=payload)
    assert response.status_code == 201, response.text
    first, before = response.json(), counts(client)
    replay = client.post(edit_url(case["track"]), json={**payload, "expected_revision": 999})
    assert replay.status_code == 201, replay.text
    assert replay.json() == first and counts(client) == before
    conflict = client.post(
        edit_url(case["track"]),
        json={**payload, "changes": [{"cue_no": 1, "text": "不能借用创建键"}]},
    )
    assert conflict.status_code == 409, conflict.text
    assert counts(client) == before


def test_subtitle_edit_creation_key_cannot_be_replayed_by_a_different_actor(client, subtitle_case):
    case = subtitle_case
    payload = edit_payload(client, case, [{"cue_no": 1, "text": "本人提交"}])
    assert client.post(edit_url(case["track"]), json=payload).status_code == 201
    before = counts(client)
    actor = account(client)
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(case["episode"]["project_id"]), user_id=UUID(actor), role="editor"
            )
        )
    response = client.post(edit_url(case["track"]), json=payload)
    assert response.status_code == 409, response.text
    assert counts(client) == before


def test_subtitle_edit_checks_asset_cas_without_consuming_a_version(client, subtitle_case):
    case = subtitle_case
    before = counts(client)
    revision = read_ok(client, asset_url(case))["revision"]
    response = edit_subtitle(
        client, case, [{"cue_no": 1, "text": "冲突的草稿"}], expected_revision=revision + 1
    )
    assert response.status_code == 409, response.text
    assert counts(client) == before
    assert read_ok(client, asset_url(case))["revision"] == revision


@pytest.mark.parametrize("state", ["LOCKED", "ARCHIVED", "STALE"])
def test_subtitle_edit_refuses_locked_archived_or_stale_target(client, subtitle_case, state):
    case = subtitle_case
    if state == "LOCKED":
        approve_subtitle(client, case)
        selected = select_subtitle(client, case)
        response = client.post(version_url(case) + "/lock", json=selection_payload(selected))
        assert response.status_code == 200, response.text
    else:
        with client.app.state.session_factory.begin() as db:
            asset = db.get(VisualAsset, UUID(case["track"]["asset_id"]))
            asset.status = state
    before = counts(client)
    response = edit_subtitle(client, case, [{"cue_no": 1, "text": "禁止自动替换"}])
    assert response.status_code == 409, response.text
    assert counts(client) == before
    assert client.get(track_url(case["track"])).status_code == 200


@pytest.mark.parametrize("state", ["DRAFT", "STALE"])
def test_subtitle_edit_rechecks_current_audio_sources_while_old_history_stays_readable(
    client, subtitle_case, state
):
    case = subtitle_case
    fresh = require_edit(client, case, [{"cue_no": 1, "text": "来源仍有效时保存"}])
    with client.app.state.session_factory.begin() as db:
        audio = db.get(AssetVersion, UUID(case["audio"][0]["id"]))
        audio.status = state
        audio.stale = state == "STALE"
    before = counts(client)
    response = edit_subtitle(client, case, [{"cue_no": 2, "text": "失效后不能再写"}], parent=fresh)
    assert response.status_code == 409, response.text
    assert counts(client) == before
    for track in (case["track"], fresh):
        assert client.get(track_url(track)).status_code == 200
        assert client.get(track_url(track) + "/srt").status_code == 200


@pytest.mark.parametrize("role,status", [(None, 404), ("viewer", 403), ("editor", 201)])
def test_subtitle_edit_reuses_project_write_permissions(client, subtitle_case, role, status):
    case = subtitle_case
    payload = edit_payload(client, case, [{"cue_no": 1, "text": "成员字幕修改"}])
    member = account(client)
    if role:
        with client.app.state.session_factory.begin() as db:
            db.add(
                ProjectMember(
                    project_id=UUID(case["episode"]["project_id"]), user_id=UUID(member), role=role
                )
            )
    before = counts(client)
    response = client.post(edit_url(case["track"]), json=payload)
    assert response.status_code == status, response.text
    if status != 201:
        assert counts(client) == before


def test_subtitle_edited_version_uses_original_review_selection_lock_and_timeline_path(
    client, subtitle_case
):
    case = subtitle_case
    fresh = require_edit(
        client, case, [{"cue_no": 1, "text": "审核后的字幕", "start_ms": 150, "end_ms": 300}]
    )
    edited_case = {**case, "track": fresh}
    snapshot = deepcopy(case["timeline"]["snapshot"])
    next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")["clips"] = fresh[
        "clips"
    ]
    before = counts(client)
    denied = client.patch(
        f"/api/v1/timelines/{case['timeline']['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": case["timeline"]["version_no"],
            "snapshot": snapshot,
        },
    )
    assert denied.status_code == 409, denied.text
    assert counts(client) == before
    approve_subtitle(client, edited_case)
    selected = select_subtitle(client, edited_case)
    assert selected["current_version_id"] == fresh["id"]
    locked = client.post(version_url(edited_case) + "/lock", json=selection_payload(selected))
    assert locked.status_code == 200, locked.text
    forged = deepcopy(snapshot)
    next(track for track in forged["tracks"] if track["kind"] == "SUBTITLE")["clips"][0][
        "start_ms"
    ] += 1
    bad = client.patch(
        f"/api/v1/timelines/{case['timeline']['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": case["timeline"]["version_no"],
            "snapshot": forged,
        },
    )
    assert bad.status_code == 422, bad.text
    assert bad.json()["error"]["code"] == "SUBTITLE_CLIP_MISMATCH"
    saved = save_timeline(client, case["timeline"], snapshot)
    actual = next(track for track in saved["snapshot"]["tracks"] if track["kind"] == "SUBTITLE")
    assert actual["clips"][0]["source_asset_version_id"] == fresh["id"]
    assert actual["clips"][0]["start_ms"] == 150
    assert client.get(track_url(case["track"])).json() == case["track"]


def test_subtitle_edit_rejects_damaged_source_audio_without_hiding_old_edits(client, subtitle_case):
    case = subtitle_case
    fresh = require_edit(client, case, [{"cue_no": 1, "text": "损坏前保存"}])
    with client.app.state.session_factory() as db:
        audio = db.get(AssetVersion, UUID(case["audio"][0]["id"]))
        key = audio.object_key
    corrupt_object(client, key, b"corrupt-audio", "audio/wav")
    before = counts(client)
    response = edit_subtitle(
        client, case, [{"cue_no": 2, "text": "不能使用损坏来源"}], parent=fresh
    )
    assert response.status_code == 409, response.text
    assert counts(client) == before
    assert client.get(track_url(fresh)).status_code == 200
    assert client.get(track_url(fresh) + "/srt").status_code == 200


def test_subtitle_edit_transaction_failure_removes_file_and_allows_same_key_retry(
    client, subtitle_case, monkeypatch
):
    from arclight.application import subtitles

    case = subtitle_case
    payload = edit_payload(client, case, [{"cue_no": 1, "text": "回滚后重试"}])
    original_attach, original_write = subtitles.attach_output, subtitles.write_object
    written = []

    def record_write(db, settings, project_id, version_id, data, media_type):
        written.append(subtitles.object_key(project_id, version_id, settings))
        return original_write(db, settings, project_id, version_id, data, media_type)

    def fail_attach(*args, **kwargs):
        raise DomainError("TEST_EDIT_TRANSACTION_FAILURE", "隔离字幕编辑事务失败", 409)

    before = counts(client)
    monkeypatch.setattr(subtitles, "write_object", record_write)
    monkeypatch.setattr(subtitles, "attach_output", fail_attach)
    response = client.post(edit_url(case["track"]), json=payload)
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "TEST_EDIT_TRANSACTION_FAILURE"
    assert counts(client) == before
    assert len(written) == 1
    with pytest.raises(DomainError):
        ObjectStorage(client.app.state.settings).stat(written[0])
    monkeypatch.setattr(subtitles, "attach_output", original_attach)
    retried = client.post(edit_url(case["track"]), json=payload)
    assert retried.status_code == 201, retried.text
    assert len(written) == 2 and written[0] != written[1]
    assert client.get(track_url(case["track"])).json() == case["track"]


def test_subtitle_edited_history_and_write_recheck_revoked_membership(client, subtitle_case):
    case = subtitle_case
    fresh = require_edit(client, case, [{"cue_no": 1, "text": "撤权前的字幕"}])
    payload = edit_payload(client, case, [{"cue_no": 2, "text": "撤权不能继续编辑"}])
    member = account(client)
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(case["episode"]["project_id"]), user_id=UUID(member), role="editor"
        )
        db.add(membership)
        db.flush()
        membership_id = membership.id
    assert client.get(track_url(fresh)).status_code == 200
    assert client.get(track_url(fresh) + "/srt").status_code == 200
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, membership_id))
    before = counts(client)
    for track in (case["track"], fresh):
        assert client.get(track_url(track)).status_code == 404
        assert client.get(track_url(track) + "/srt").status_code == 404
    response = client.post(edit_url(fresh), json=payload)
    assert response.status_code == 404, response.text
    assert counts(client) == before


@pytest.mark.parametrize("operation", ["replay", "cas"])
def test_postgres_subtitle_edit_serializes_replay_and_target_revision(
    client, settings, subtitle_case, operation
):
    require_postgres(settings)
    case = subtitle_case
    payload = edit_payload(client, case, [{"cue_no": 1, "text": "并发修改字幕"}])
    before = counts(client)
    barrier = Barrier(2)
    factory = client.app.state.session_factory

    def execute(index):
        command = {**payload}
        if operation == "cas":
            command["creation_key"] = str(uuid4())
        barrier.wait(timeout=15)
        try:
            with factory.begin() as db:
                result = edit_subtitles(
                    db,
                    settings,
                    UUID(case["track"]["id"]),
                    SubtitleEdit.model_validate(command),
                    case["audio"][0]["created_by"],
                )
                return "created", str(result.id)
        except DomainError as error:
            return "conflict", error.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, range(2)))
    if operation == "replay":
        assert all(status == "created" for status, _ in results)
        assert len({identity for status, identity in results if status == "created"}) == 1
    else:
        assert sorted(status for status, _ in results) == ["conflict", "created"]
        assert [identity for status, identity in results if status == "conflict"] == [
            "VERSION_CONFLICT"
        ]
    assert tuple(
        after - initial for initial, after in zip(before, counts(client), strict=True)
    ) == (
        0,
        1,
        1,
        1,
        1,
    )
