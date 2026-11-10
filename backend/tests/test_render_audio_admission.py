"""Complete dialogue/narration decisions and source ranges use exact real audio."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from render_admission_helpers import (
    assert_blocked,
    edited,
    line_decisions,
    ready_manifest,
    render_path,
)
from render_manifest_helpers import (
    audio_case,
    manifest,
    manifest_url,
    render_case,
    review_case,
    stored_file,
    timeline_case,
)
from test_media_validation import wave_bytes

from arclight.application.render_audio import validate_line_audio
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import LineAudioDecision, RenderManifestCreate
from arclight.domain.timelines import TimelineSnapshot
from arclight.domain.versions import digest
from arclight.persistence.models import Shot

__all__ = ["audio_case", "render_case", "review_case", "timeline_case"]


def test_decisions_are_frozen_canonical_idempotent_and_preserve_schema1_hash(client, render_case):
    legacy, body = manifest(client, render_case)
    assert legacy["snapshot"]["schema_version"] == 1
    assert "line_audio_decisions" not in legacy["snapshot"]
    assert legacy["content_sha256"] == digest(legacy["snapshot"])
    assert client.post(manifest_url(render_case), json=body).json() == legacy
    decisions = line_decisions(client, render_case)
    assert len(decisions) == 2  # Includes a narrator outside the first dialogue line.
    body = {"creation_key": str(uuid4()), "line_audio_decisions": decisions}
    first = client.post(manifest_url(render_case), json=body)
    assert first.status_code == 201, first.text
    value = first.json()
    assert value["snapshot"]["schema_version"] == 2
    assert value["snapshot"]["line_audio_decisions"] == sorted(
        decisions, key=lambda item: (item["script_version_id"], item["line_id"])
    )
    assert (
        client.post(
            manifest_url(render_case), json={**body, "line_audio_decisions": decisions[::-1]}
        ).json()
        == value
    )
    changed = deepcopy(body)
    changed["line_audio_decisions"][0]["reason"] = "改为另一个创作决定"
    assert_blocked(client.post(manifest_url(render_case), json=changed), "IDEMPOTENCY_CONFLICT")


@pytest.mark.parametrize("problem", ["line", "script", "missing_subtitle"])
def test_decisions_cannot_invent_lines_versions_or_subtitles(client, render_case, problem):
    decisions = line_decisions(client, render_case)
    if problem == "line":
        decisions[0]["line_id"] = "unknown-line"
    elif problem == "script":
        decisions[0]["script_version_id"] = str(uuid4())
    else:
        decisions[0]["mode"] = "SUBTITLE_ONLY"
    response = client.post(
        manifest_url(render_case),
        json={"creation_key": str(uuid4()), "line_audio_decisions": decisions},
    )
    assert_blocked(
        response,
        "RENDER_SUBTITLE_REQUIRED"
        if problem == "missing_subtitle"
        else "RENDER_LINE_DECISION_INVALID",
    )


@pytest.mark.parametrize("reason", ["", "  \t\n", "x" * 501])
def test_empty_or_unbounded_creative_reasons_are_rejected(reason):
    with pytest.raises(ValidationError):
        LineAudioDecision(script_version_id=uuid4(), line_id="L1", mode="SILENT", reason=reason)


def test_duplicate_decisions_are_rejected_and_reason_is_normalized():
    item = LineAudioDecision(
        script_version_id=uuid4(), line_id="L1", mode="SILENT", reason="  创作留白  "
    )
    assert item.reason == "创作留白"
    with pytest.raises(ValidationError):
        RenderManifestCreate(creation_key=uuid4(), line_audio_decisions=(item, item))


def test_narration_cannot_be_omitted_even_when_dialogue_has_real_voice(client, audio_case):
    timeline, font, _ = audio_case
    assert len(line_decisions(client, timeline)) == 1
    value, _ = manifest(client, timeline, font_file_id=str(font))
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_AUDIO_INCOMPLETE")
    ready, _ = ready_manifest(client, timeline, font_file_id=str(font))
    response = client.post(render_path(ready, "admission"))
    assert response.status_code == 200, response.text


def test_silence_cannot_conflict_with_an_actual_voice(client, audio_case):
    timeline, font, _ = audio_case
    voice = timeline["snapshot"]["tracks"][1]["clips"][0]
    decisions = line_decisions(client, timeline) + [
        {
            "script_version_id": voice["script_version_id"],
            "line_id": voice["line_id"],
            "mode": "SILENT",
            "reason": "不应覆盖真实配音",
        }
    ]
    assert_blocked(
        client.post(
            manifest_url(timeline),
            json={
                "creation_key": str(uuid4()),
                "font_file_id": str(font),
                "line_audio_decisions": decisions,
            },
        ),
        "RENDER_LINE_DECISION_CONFLICT",
    )


def test_subtitle_only_is_explicit_and_requires_no_fictional_audio(client, audio_case):
    timeline, font, _ = audio_case
    snapshot = deepcopy(timeline["snapshot"])
    snapshot["tracks"][1]["clips"] = []
    subtitle = snapshot["tracks"][4]["clips"][0]
    subtitle["audio_version_id"] = None
    timeline = edited(client, timeline, snapshot)
    decisions = line_decisions(client, timeline)
    next(item for item in decisions if item["line_id"] == subtitle["line_id"])["mode"] = (
        "SUBTITLE_ONLY"
    )
    value, _ = manifest(client, timeline, font_file_id=str(font), line_audio_decisions=decisions)
    response = client.post(render_path(value, "admission"))
    assert response.status_code == 200, response.text


@pytest.mark.parametrize("problem", ["unrelated_voice", "unvoiced_line"])
def test_subtitle_audio_must_be_the_actual_voice_for_that_exact_line(
    client, episode, audio_case, problem
):
    timeline, font, _ = audio_case
    snapshot = deepcopy(timeline["snapshot"])
    subtitle = snapshot["tracks"][4]["clips"][0]
    if problem == "unrelated_voice":
        _, identity, _ = stored_file(
            client, episode["project_id"], wave_bytes(), "audio/wav", kind="AUDIO"
        )
        subtitle["audio_version_id"] = str(identity)
    else:
        subtitle["line_id"] = line_decisions(client, timeline)[0]["line_id"]
    timeline = edited(client, timeline, snapshot)
    value, _ = ready_manifest(client, timeline, font_file_id=str(font))
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_SUBTITLE_AUDIO_CONFLICT")


@pytest.mark.parametrize("kind", ["VOICE", "SFX", "MUSIC"])
@pytest.mark.parametrize("range_field", ["source_start_ms", "duration_ms"])
def test_audio_range_uses_decoded_duration_instead_of_declared_metadata(
    client, episode, audio_case, kind, range_field
):
    timeline, font, _ = audio_case
    snapshot = deepcopy(timeline["snapshot"])
    clip = next(track for track in snapshot["tracks"] if track["kind"] == kind)["clips"][0]
    clip[range_field] = 1 if range_field == "source_start_ms" else 1001
    _, identity, checksum = stored_file(
        client,
        episode["project_id"],
        wave_bytes(),
        "audio/wav",
        kind="AUDIO",
        version_metadata={"duration_ms": 60_000, "media": {"duration_ms": 60_000}},
    )
    clip.update(source_asset_version_id=str(identity), source_sha256=checksum)
    if kind == "VOICE":
        snapshot["tracks"][4]["clips"][0]["audio_version_id"] = str(identity)
    timeline = edited(client, timeline, snapshot)
    value, _ = ready_manifest(client, timeline, font_file_id=str(font))
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_SOURCE_RANGE_INVALID")


@pytest.mark.parametrize("kind", ["VIDEO", "SUBTITLE"])
def test_static_images_and_subtitles_reject_nonzero_media_inpoints(client, audio_case, kind):
    timeline, font, _ = audio_case
    snapshot = deepcopy(timeline["snapshot"])
    next(track for track in snapshot["tracks"] if track["kind"] == kind)["clips"][0][
        "source_start_ms"
    ] = 1
    timeline = edited(client, timeline, snapshot)
    value, _ = ready_manifest(client, timeline, font_file_id=str(font))
    assert_blocked(client.post(render_path(value, "admission")), "RENDER_SOURCE_RANGE_INVALID")


def test_legacy_schema1_with_all_real_voices_can_still_be_admitted(client, episode, render_case):
    _, identity, checksum = stored_file(
        client, episode["project_id"], wave_bytes(), "audio/wav", kind="AUDIO"
    )
    snapshot = deepcopy(render_case["snapshot"])
    snapshot["tracks"][1]["clips"] = [
        {
            "id": str(uuid4()),
            "ordinal": i,
            "start_ms": i * 1000,
            "duration_ms": 1000,
            "script_version_id": item["script_version_id"],
            "line_id": item["line_id"],
            "source_asset_version_id": str(identity),
            "source_sha256": checksum,
        }
        for i, item in enumerate(line_decisions(client, render_case))
    ]
    timeline = edited(client, render_case, snapshot)
    value, _ = manifest(client, timeline)
    assert value["snapshot"]["schema_version"] == 1
    response = client.post(render_path(value, "admission"))
    assert response.status_code == 200, response.text


def test_configured_required_sound_effects_need_a_clip_for_the_same_shot(client, audio_case):
    timeline, _, _ = audio_case
    snapshot = TimelineSnapshot.model_validate(timeline["snapshot"])
    decisions = tuple(
        LineAudioDecision.model_validate(item) for item in line_decisions(client, timeline)
    )
    shot_id = snapshot.tracks[0].clips[0].shot_id
    # Exercise the rule with in-memory facts; persisted shot content is immutable.
    with client.app.state.session_factory() as db, db.no_autoflush:
        shot = db.get(Shot, shot_id)
        shot.content_json = {**shot.content_json, "sound_effects": ["脚步声", "雨声"]}
        with pytest.raises(DomainError) as error:
            validate_line_audio(db, snapshot, decisions, complete=True)
        assert error.value.code == "RENDER_SFX_INCOMPLETE"
        snapshot.tracks[2].clips[0] = (
            snapshot.tracks[2].clips[0].model_copy(update={"shot_id": shot_id})
        )
        validate_line_audio(db, snapshot, decisions, complete=True)
