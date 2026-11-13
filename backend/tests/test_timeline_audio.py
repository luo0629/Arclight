from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_manifest_helpers import manifest, stored_file
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_continuity_reviews import review_case
from test_media_validation import wave_bytes
from test_timeline_saves import save_counts, save_url, saved_case, saved_timeline
from test_timelines import timeline_case

from arclight.application.render_plans import check_support
from arclight.application.timelines import save_timeline
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import RenderManifestSnapshot
from arclight.domain.timelines import TimelineSave
from arclight.persistence.models import AssetDependency, AssetVersion
from arclight.persistence.timelines import Timeline, TimelineClipAudio

__all__ = ["review_case", "saved_case", "timeline_case", "audio_timeline_case"]


def configured_audio(role):
    return {
        "purpose_role": role,
        "gain_milli": 750,
        "fade_in_ms": 100,
        "fade_out_ms": 200,
        "envelope": [
            {"offset_ms": 0, "gain_milli": 1000},
            {"offset_ms": 500, "gain_milli": 500},
            {"offset_ms": 1000, "gain_milli": 1000},
        ],
    }


def music_clip(identity, checksum, role, ordinal=0):
    return {
        "id": str(uuid4()),
        "ordinal": ordinal,
        "source_asset_version_id": str(identity),
        "source_sha256": checksum,
        "start_ms": ordinal * 1000,
        "duration_ms": 1000,
        "audio": configured_audio(role),
    }


@pytest.fixture
def audio_timeline_case(client, episode, saved_case):
    original, body, _ = saved_case
    _, identity, checksum = stored_file(
        client, episode["project_id"], wave_bytes(), "audio/wav", kind="AUDIO"
    )
    for track in body["snapshot"]["tracks"]:
        if track["kind"] == "MUSIC":
            track["clips"] = [music_clip(identity, checksum, "BACKGROUND")]
        if track["kind"] == "SFX":
            track["clips"] = [
                music_clip(identity, checksum, "AMBIENCE"),
                music_clip(identity, checksum, "TRANSITION", 1),
            ]
    return original, body, identity


def audio_counts(client):
    with client.app.state.session_factory() as db:
        return (
            *save_counts(client),
            db.scalar(select(func.count()).select_from(TimelineClipAudio)),
        )


def test_audio_tracks_save_replay_and_capture_exact_dependencies_without_changing_parent(
    client, audio_timeline_case
):
    original, body, identity = audio_timeline_case
    value = saved_timeline(client, original, body)
    expected = {
        clip["audio"]["purpose_role"]: clip["audio"]
        for track in body["snapshot"]["tracks"]
        for clip in track["clips"]
        if "audio" in clip
    }
    assert {
        clip["audio"]["purpose_role"]: clip["audio"]
        for track in value["snapshot"]["tracks"]
        for clip in track["clips"]
        if "audio" in clip
    } == expected
    assert value["snapshot"]["schema_version"] == original["snapshot"]["schema_version"] == 1
    before = audio_counts(client)
    replay = deepcopy(body)
    replay["snapshot"]["tracks"].reverse()
    for track in replay["snapshot"]["tracks"]:
        track["clips"].reverse()
    assert saved_timeline(client, original, replay) == value
    assert audio_counts(client) == before
    assert client.get(save_url(original)).json() == original
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(value["id"]))
        assert version.metadata_json["timeline_audio_clip_count"] == 3
        assert db.scalar(select(func.count()).select_from(TimelineClipAudio)) == 3
        assert (
            db.scalar(
                select(AssetDependency.id).where(
                    AssetDependency.source_asset_version_id == identity,
                    AssetDependency.target_asset_version_id == version.id,
                    AssetDependency.dependency_type == "RENDER_DEPENDENCY",
                )
            )
            is not None
        )


def test_audio_gain_edit_appends_an_independent_draft_and_stale_cas_has_no_effects(
    client, audio_timeline_case
):
    original, body, _ = audio_timeline_case
    first = saved_timeline(client, original, body)
    snapshot = deepcopy(first["snapshot"])
    next(track for track in snapshot["tracks"] if track["kind"] == "MUSIC")["clips"][0]["audio"][
        "gain_milli"
    ] = 250
    edit = {
        "creation_key": str(uuid4()),
        "expected_version_no": first["version_no"],
        "snapshot": snapshot,
    }
    second = saved_timeline(client, first, edit)
    assert second["status"] == "DRAFT"
    assert second["based_on_timeline_id"] == first["id"]
    assert client.get(save_url(first)).json() == first
    before = audio_counts(client)
    body["creation_key"] = str(uuid4())
    assert client.patch(save_url(original), json=body).status_code == 409
    assert audio_counts(client) == before


def test_manifest_freezes_audio_automation_and_source_rights_while_encoder_rejects_mix(
    client, audio_timeline_case
):
    original, body, identity = audio_timeline_case
    timeline = saved_timeline(client, original, body)
    value, _ = manifest(client, timeline)
    snapshot = value["snapshot"]
    assert snapshot["timeline"] == timeline["snapshot"]
    source = next(
        source for source in snapshot["sources"] if source["asset_version_id"] == str(identity)
    )
    assert source["file"]["source_note"] and source["file"]["rights_note"]
    assert source["file"]["rights_confirmed"] is True
    with pytest.raises(DomainError) as error:
        check_support(RenderManifestSnapshot.model_validate(snapshot))
    assert error.value.code == "RENDER_TRACK_UNSUPPORTED"
    changed = deepcopy(timeline["snapshot"])
    next(track for track in changed["tracks"] if track["kind"] == "MUSIC")["clips"][0]["audio"][
        "gain_milli"
    ] = 125
    newer = saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": changed,
        },
    )
    assert newer["id"] != timeline["id"]
    assert client.get(f"/api/v1/render-manifests/{value['id']}").json() == value


def test_audio_configuration_history_rejects_update_delete_replace_and_late_insert(
    client, settings, audio_timeline_case
):
    original, body, _ = audio_timeline_case
    value = saved_timeline(client, original, body)
    for statement in (
        update(TimelineClipAudio).values(settings_json={}),
        delete(TimelineClipAudio),
    ):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(statement)
    video = original["snapshot"]["tracks"][0]["clips"][0]
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        db.add(
            TimelineClipAudio(
                id=UUID(video["id"]),
                timeline_id=UUID(original["id"]),
                settings_json=configured_audio("BACKGROUND"),
            )
        )
        db.flush()
    if settings.database_url.startswith("sqlite"):
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(
                text("INSERT OR REPLACE INTO timeline_clip_audio SELECT * FROM timeline_clip_audio")
            )
    else:
        with (
            pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
            client.app.state.session_factory.begin() as db,
        ):
            db.execute(text("TRUNCATE timeline_clip_audio"))
    assert client.get(save_url(value)).json() == value


@pytest.mark.parametrize("omit_all", [False, True])
def test_missing_audio_configuration_cannot_seal_and_rolls_back_every_effect(
    client, settings, audio_timeline_case, monkeypatch, omit_all
):
    original, body, _ = audio_timeline_case
    before = audio_counts(client)
    original_add = Session.add
    omitted = False

    def omit_audio(db, instance, *args, **kwargs):
        nonlocal omitted
        if isinstance(instance, TimelineClipAudio) and (omit_all or not omitted):
            omitted = True
            return None
        return original_add(db, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", omit_audio)
    with (
        pytest.raises(DBAPIError, match="incomplete timeline audio"),
        client.app.state.session_factory.begin() as db,
    ):
        actor = db.get(Timeline, UUID(original["id"])).created_by
        save_timeline(db, settings, UUID(original["id"]), actor, TimelineSave.model_validate(body))
    assert omitted
    assert audio_counts(client) == before
    assert client.get(save_url(original)).json() == original


def test_configured_legacy_sound_requires_confirmed_exact_file_rights(
    client, episode, audio_timeline_case
):
    original, body, _ = audio_timeline_case
    _, identity, checksum = stored_file(
        client, episode["project_id"], wave_bytes(), "audio/wav", kind="AUDIO", rights=False
    )
    clip = next(track for track in body["snapshot"]["tracks"] if track["kind"] == "MUSIC")["clips"][
        0
    ]
    clip.update(source_asset_version_id=str(identity), source_sha256=checksum)
    before = audio_counts(client)
    response = client.patch(save_url(original), json=body)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RENDER_RIGHTS_REQUIRED"
    assert audio_counts(client) == before


def test_audio_save_and_replay_recheck_membership_and_editor_role(
    client, episode, audio_timeline_case
):
    from test_collaboration import register

    from arclight.persistence.models import ProjectMember

    original, body, _ = audio_timeline_case
    owner = client.headers["Authorization"]
    member = register(client, "timeline-audio-editor@example.com")
    assert client.patch(save_url(original), json=body).status_code == 404
    with client.app.state.session_factory.begin() as db:
        membership = ProjectMember(
            project_id=UUID(episode["project_id"]),
            user_id=UUID(member["user"]["id"]),
            role="viewer",
        )
        db.add(membership)
        db.flush()
        identity = membership.id
    before = audio_counts(client)
    assert client.patch(save_url(original), json=body).status_code == 403
    assert audio_counts(client) == before
    with client.app.state.session_factory.begin() as db:
        db.get(ProjectMember, identity).role = "editor"
    value = saved_timeline(client, original, body)
    with client.app.state.session_factory.begin() as db:
        db.delete(db.get(ProjectMember, identity))
    assert client.patch(save_url(original), json=body).status_code == 404
    client.headers["Authorization"] = owner
    assert client.get(save_url(value)).json() == value


@pytest.mark.parametrize("tamper", ["role", "timeline", "clip"])
def test_audio_side_rows_cannot_change_track_purpose_or_exact_parent(
    client, settings, audio_timeline_case, monkeypatch, tamper
):
    original, body, _ = audio_timeline_case
    before = audio_counts(client)
    original_add = Session.add

    def corrupt_audio(db, instance, *args, **kwargs):
        if isinstance(instance, TimelineClipAudio):
            if tamper == "role":
                instance.settings_json = {**instance.settings_json, "purpose_role": "BACKGROUND"}
            elif tamper == "timeline":
                instance.timeline_id = UUID(original["id"])
            else:
                instance.id = uuid4()
        return original_add(db, instance, *args, **kwargs)

    monkeypatch.setattr(Session, "add", corrupt_audio)
    with (
        pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"),
        client.app.state.session_factory.begin() as db,
    ):
        actor = db.get(Timeline, UUID(original["id"])).created_by
        save_timeline(db, settings, UUID(original["id"]), actor, TimelineSave.model_validate(body))
    assert audio_counts(client) == before
    assert client.get(save_url(original)).json() == original
