"""Bounded canonical timeline contracts shared with future audio and render writers."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.domain.timelines import TRACK_KINDS, TimelineCreate, TimelineSnapshot


def snapshot():
    script, line, audio = str(uuid4()), str(uuid4()), str(uuid4())
    tracks = []
    for ordinal, kind in enumerate(TRACK_KINDS):
        clip = {
            "id": str(uuid4()),
            "ordinal": 0,
            "source_asset_version_id": audio if kind == "VOICE" else str(uuid4()),
            "source_sha256": "a" * 64,
            "start_ms": 0,
            "duration_ms": 3000,
        }
        if kind == "VIDEO":
            clip["shot_id"] = str(uuid4())
        if kind in {"VOICE", "SUBTITLE"}:
            clip.update(script_version_id=script, line_id=line)
        if kind == "SUBTITLE":
            clip["audio_version_id"] = audio
        tracks.append({"id": str(uuid4()), "kind": kind, "ordinal": ordinal, "clips": [clip]})
    return {
        "episode_id": str(uuid4()),
        "storyboard_id": str(uuid4()),
        "default_duration_ms": 3000,
        "duration_ms": 3000,
        "output_profile": {"width": 1080, "height": 1920, "fps": 30},
        "tracks": tracks,
    }


def test_all_five_tracks_preserve_exact_media_script_line_and_audio_versions():
    value = snapshot()
    parsed = TimelineSnapshot.model_validate(value)
    reread = TimelineSnapshot.model_validate_json(parsed.model_dump_json())
    assert reread == parsed
    subtitle = reread.tracks[4].clips[0]
    voice = reread.tracks[1].clips[0]
    assert subtitle.script_version_id == voice.script_version_id
    assert subtitle.line_id == voice.line_id
    assert subtitle.audio_version_id == voice.source_asset_version_id
    assert subtitle.source_asset_version_id != subtitle.audio_version_id
    with pytest.raises(ValidationError):
        parsed.duration_ms = 4000


@pytest.mark.parametrize(
    "problem",
    [
        "missing_track",
        "duplicate_kind",
        "duplicate_track",
        "duplicate_ordinal",
        "duplicate_clip",
        "duplicate_clip_ordinal",
        "clip_limit",
        "checksum",
        "unpaired_script",
        "unpaired_line",
        "voice_without_line",
        "subtitle_without_line",
        "video_without_shot",
        "audio_on_video",
        "negative_time",
        "overflow",
        "gap",
        "extra_command",
        "boolean_duration",
    ],
)
def test_invalid_timing_identity_reference_and_untrusted_command_contracts_fail(problem):
    value = snapshot()
    video = value["tracks"][0]["clips"][0]
    voice = value["tracks"][1]["clips"][0]
    if problem == "missing_track":
        value["tracks"].pop()
    elif problem == "duplicate_kind":
        value["tracks"][1]["kind"] = "VIDEO"
    elif problem == "duplicate_track":
        value["tracks"][1]["id"] = value["tracks"][0]["id"]
    elif problem == "duplicate_ordinal":
        value["tracks"][1]["ordinal"] = 0
    elif problem == "duplicate_clip":
        voice["id"] = video["id"]
    elif problem == "duplicate_clip_ordinal":
        value["tracks"][1]["clips"].append({**deepcopy(voice), "id": str(uuid4())})
    elif problem == "clip_limit":
        voice["ordinal"] = 256
    elif problem == "checksum":
        video["source_sha256"] = "latest"
    elif problem == "unpaired_script":
        voice["script_version_id"] = None
    elif problem == "unpaired_line":
        voice["line_id"] = None
    elif problem in {"voice_without_line", "subtitle_without_line"}:
        clip = voice if problem == "voice_without_line" else value["tracks"][4]["clips"][0]
        clip.update(script_version_id=None, line_id=None)
    elif problem == "video_without_shot":
        video["shot_id"] = None
    elif problem == "audio_on_video":
        video["audio_version_id"] = str(uuid4())
    elif problem == "negative_time":
        video["start_ms"] = -1
    elif problem == "overflow":
        video["duration_ms"] = 4000
    elif problem == "gap":
        video.update(start_ms=1, duration_ms=2999)
    elif problem == "extra_command":
        value["ffmpeg_command"] = "arbitrary shell command"
    else:
        video["duration_ms"] = True
    with pytest.raises(ValidationError):
        TimelineSnapshot.model_validate(value)


@pytest.mark.parametrize("duration", [0, 499, 30001, True, "3000"])
def test_create_rejects_unbounded_or_coerced_duration(duration):
    with pytest.raises(ValidationError):
        TimelineCreate.model_validate(
            {
                "creation_key": str(uuid4()),
                "storyboard_id": str(uuid4()),
                "default_duration_ms": duration,
                "selected_shots": [{"shot_id": str(uuid4()), "asset_version_id": str(uuid4())}],
            }
        )


def test_create_bounds_inputs_and_never_resolves_a_current_pointer_implicitly():
    value = {
        "creation_key": str(uuid4()),
        "storyboard_id": str(uuid4()),
        "selected_shots": [
            {"shot_id": str(uuid4()), "asset_version_id": str(uuid4())} for _ in range(63)
        ],
    }
    assert len(TimelineCreate.model_validate(value).selected_shots) == 63
    value["selected_shots"].append({"shot_id": str(uuid4()), "asset_version_id": str(uuid4())})
    with pytest.raises(ValidationError):
        TimelineCreate.model_validate(value)
    value["selected_shots"] = [{"shot_id": str(uuid4())}]
    with pytest.raises(ValidationError):
        TimelineCreate.model_validate(value)
