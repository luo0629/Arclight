"""Track edits persist immutable intent and change the actual renderer, not just the UI."""

from array import array
from copy import deepcopy
from io import BytesIO
from uuid import UUID, uuid4

import av
import pytest
from pydantic import ValidationError
from render_admission_helpers import edited, line_decisions, ready_manifest, render_path
from render_job_helpers import read, run, submit
from render_manifest_helpers import audio_case, manifest, render_case, review_case, timeline_case

from arclight.application.render_audio import validate_line_audio
from arclight.application.render_plans import build_plan
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import LineAudioDecision, RenderManifestSnapshot
from arclight.domain.timelines import TimelineSnapshot, TrackSnapshot
from arclight.domain.versions import digest
from arclight.media.render_plan import RenderPlan
from arclight.persistence.models import AssetVersion, Shot, StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = ["audio_case", "render_case", "review_case", "timeline_case"]


@pytest.mark.parametrize("kind", ["VOICE", "MUSIC", "SFX", "SUBTITLE"])
def test_switch_is_saved_in_new_version_replays_and_restores_without_changing_parent(
    client, audio_case, kind
):
    parent, _, _ = audio_case
    snapshot = deepcopy(parent["snapshot"])
    track = next(track for track in snapshot["tracks"] if track["kind"] == kind)
    track["enabled"] = False
    body = {
        "creation_key": str(uuid4()),
        "expected_version_no": parent["version_no"],
        "snapshot": snapshot,
    }
    path = f"/api/v1/timelines/{parent['id']}"
    response = client.patch(path, json=body)
    assert response.status_code == 201, response.text
    saved = response.json()
    assert client.patch(path, json=body).json() == saved
    assert client.get(path).json() == parent
    assert client.get(f"/api/v1/timelines/{saved['id']}").json() == saved
    frozen = next(t for t in saved["snapshot"]["tracks"] if t["kind"] == kind)
    assert frozen["enabled"] is False and frozen["clips"]
    assert saved["content_sha256"] == digest(saved["snapshot"])
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, UUID(saved["id"]))
        assert version.metadata_json["timeline_disabled_tracks"] == [kind]
    restored = deepcopy(saved["snapshot"])
    next(t for t in restored["tracks"] if t["kind"] == kind)["enabled"] = True
    restored = edited(client, saved, restored)
    assert all("enabled" not in t for t in restored["snapshot"]["tracks"])
    assert client.get(f"/api/v1/timelines/{saved['id']}").json() == saved


@pytest.mark.parametrize("enabled", [0, 1, "false", None])
def test_switch_rejects_coercion(enabled):
    with pytest.raises(ValidationError):
        TrackSnapshot(id=uuid4(), kind="VOICE", ordinal=1, enabled=enabled)


@pytest.mark.parametrize(
    "disabled", ["VOICE", ["VIDEO"], ["UNKNOWN"], ["VOICE", "VOICE"], [False], [{}]]
)
def test_corrupt_frozen_switch_metadata_is_rejected_before_checksum(
    client, episode, audio_case, disabled, monkeypatch
):
    from arclight.application import timeline_store
    from arclight.persistence.timelines import Timeline

    original = timeline_store.exact_version

    def corrupt_frozen_metadata(db, project_id, version_id):
        # Inject a damaged storage result after exact_version's deliberate refresh.
        # An unflushed identity-map mutation is correctly discarded by that read.
        version, asset = original(db, project_id, version_id)
        version.metadata_json = {**version.metadata_json, "timeline_disabled_tracks": disabled}
        return version, asset

    monkeypatch.setattr(timeline_store, "exact_version", corrupt_frozen_metadata)
    timeline, _, _ = audio_case
    with client.app.state.session_factory() as db, db.no_autoflush:
        row = db.get(Timeline, UUID(timeline["id"]))
        with pytest.raises(DomainError) as error:
            timeline_store.checked_timeline(db, row, UUID(episode["project_id"]))
        assert error.value.code == "TIMELINE_HISTORY_INVALID"


def test_default_switch_has_identical_historical_serialization_and_video_cannot_be_muted():
    track = {"id": str(uuid4()), "kind": "VIDEO", "ordinal": 0, "clips": []}
    assert TrackSnapshot.model_validate(track).model_dump(mode="json") == track
    assert TrackSnapshot.model_validate({**track, "enabled": True}).model_dump(mode="json") == track
    with pytest.raises(ValidationError, match="画面轨"):
        TrackSnapshot.model_validate({**track, "enabled": False})


def test_muting_voice_requires_explicit_decisions_and_preserves_frozen_subtitle_audio(
    client, audio_case
):
    original, font, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    next(t for t in snapshot["tracks"] if t["kind"] == "VOICE")["enabled"] = False
    timeline = edited(client, original, snapshot)
    missing, _ = manifest(client, timeline, font_file_id=str(font))
    response = client.post(render_path(missing, "admission"))
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RENDER_AUDIO_INCOMPLETE"
    decisions = line_decisions(client, timeline)
    subtitle = next(t for t in snapshot["tracks"] if t["kind"] == "SUBTITLE")["clips"][0]
    next(d for d in decisions if d["line_id"] == subtitle["line_id"])["mode"] = "SUBTITLE_ONLY"
    value, _ = manifest(client, timeline, font_file_id=str(font), line_audio_decisions=decisions)
    response = client.post(render_path(value, "admission"))
    assert response.status_code == 200, response.text
    assert (
        value["snapshot"]["timeline"]["tracks"][4]["clips"][0]["audio_version_id"]
        == subtitle["audio_version_id"]
    )
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original


def test_muted_subtitle_cannot_satisfy_subtitle_only_decision(client, audio_case):
    original, _, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    for track in snapshot["tracks"]:
        if track["kind"] in {"VOICE", "SUBTITLE"}:
            track["enabled"] = False
    timeline = edited(client, original, snapshot)
    decisions = line_decisions(client, timeline)
    decisions[0]["mode"] = "SUBTITLE_ONLY"
    response = client.post(
        f"/api/v1/timelines/{timeline['id']}/render-manifests",
        json={"creation_key": str(uuid4()), "line_audio_decisions": decisions},
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "RENDER_SUBTITLE_REQUIRED"


def test_muting_required_sfx_is_not_a_bypass(client, audio_case):
    timeline, _, _ = audio_case
    snapshot = TimelineSnapshot.model_validate(timeline["snapshot"])
    sfx = next(t for t in snapshot.tracks if t.kind == "SFX")
    shot_id = snapshot.tracks[0].clips[0].shot_id
    sfx.clips[0] = sfx.clips[0].model_copy(update={"shot_id": shot_id})
    decisions = tuple(LineAudioDecision.model_validate(d) for d in line_decisions(client, timeline))
    with client.app.state.session_factory() as db, db.no_autoflush:
        shot = db.get(Shot, shot_id)
        shot.content_json = {**shot.content_json, "sound_effects": ["脚步声"]}
        validate_line_audio(db, snapshot, decisions, complete=True)
        muted = snapshot.model_copy(
            update={
                "tracks": [
                    t.model_copy(update={"enabled": False}) if t.kind == "SFX" else t
                    for t in snapshot.tracks
                ]
            }
        )
        with pytest.raises(DomainError) as error:
            validate_line_audio(db, muted, decisions, complete=True)
        assert error.value.code == "RENDER_SFX_INCOMPLETE"


@pytest.mark.parametrize("kind", ["VOICE", "MUSIC", "SFX", "SUBTITLE"])
def test_plan_only_executes_enabled_tracks_while_retaining_all_exact_sources(
    client, audio_case, kind
):
    original, font, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    next(t for t in snapshot["tracks"] if t["kind"] == kind)["enabled"] = False
    timeline = edited(client, original, snapshot)
    options = {} if kind == "SUBTITLE" else {"font_file_id": str(font)}
    value, _ = ready_manifest(client, timeline, **options)
    parsed = RenderManifestSnapshot.model_validate(value["snapshot"])
    with client.app.state.session_factory() as db:
        plan = RenderPlan.model_validate_json(build_plan(db, client.app.state.settings, parsed))
    assert len(plan.audio_clips) == (3 if kind == "SUBTITLE" else 2)
    assert (plan.subtitles is None) == (kind == "SUBTITLE")
    ids = {s.asset_version_id for s in parsed.sources}
    assert all(c.source_asset_version_id in ids for t in parsed.timeline.tracks for c in t.clips)


def test_actual_mp4_with_all_sound_and_subtitles_disabled_is_silent_and_decodable(
    client, audio_case
):
    original, _, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    for track in snapshot["tracks"]:
        if track["kind"] != "VIDEO":
            track["enabled"] = False
    timeline = edited(client, original, snapshot)
    value, _ = ready_manifest(client, timeline)
    assert value["snapshot"]["font"] is None
    job, _ = submit(client, value)
    assert run(client, job) == "SUCCEEDED", read(client, job)
    final = read(client, job)
    with client.app.state.session_factory() as db:
        output = db.get(StoredFile, UUID(final["output_file_id"]))
        movie = ObjectStorage(client.app.state.settings).read(
            output.object_key, None, output.checksum
        )
    with av.open(BytesIO(movie)) as container:
        frames = list(container.decode(audio=0))
        assert frames and all(frame.format.name == "fltp" for frame in frames)
        peak = max(
            abs(sample)
            for frame in frames
            for plane in frame.planes
            for sample in array("f", bytes(plane))[: frame.samples]
        )
    assert peak < 0.0001
    with av.open(BytesIO(movie)) as container:
        frames = list(container.decode(video=0))
    assert len(frames) == 60
    assert client.get(f"/api/v1/timelines/{original['id']}").json() == original
