from copy import deepcopy

import pytest
from pydantic import ValidationError
from test_timeline_contracts import snapshot

from arclight.domain.timeline_audio import ClipAudio
from arclight.domain.timelines import TimelineSnapshot
from arclight.domain.versions import digest


def audio_snapshot(role="BACKGROUND", kind="MUSIC"):
    value = snapshot()
    clip = next(track for track in value["tracks"] if track["kind"] == kind)["clips"][0]
    clip["audio"] = {
        "purpose_role": role,
        "gain_milli": 750,
        "fade_in_ms": 250,
        "fade_out_ms": 500,
        "envelope": [
            {"offset_ms": 0, "gain_milli": 1000},
            {"offset_ms": 1500, "gain_milli": 250},
            {"offset_ms": 3000, "gain_milli": 1000},
        ],
    }
    return value, clip


def test_audio_optional_field_keeps_legacy_schema_bytes_and_checksums():
    original = TimelineSnapshot.model_validate(snapshot()).model_dump(mode="json")
    assert all("audio" not in clip for track in original["tracks"] for clip in track["clips"])
    explicit = deepcopy(original)
    for track in explicit["tracks"]:
        for clip in track["clips"]:
            clip["audio"] = None
    restored = TimelineSnapshot.model_validate(explicit).model_dump(mode="json")
    assert restored == original
    assert digest(restored) == digest(original)


@pytest.mark.parametrize(
    ("role", "kind"),
    [("BACKGROUND", "MUSIC"), ("AMBIENCE", "SFX"), ("TRANSITION", "SFX")],
)
def test_audio_roles_and_integer_gain_envelope_roundtrip(role, kind):
    raw, clip = audio_snapshot(role, kind)
    value = TimelineSnapshot.model_validate(raw)
    actual = next(track for track in value.tracks if track.kind == kind).clips[0]
    assert actual.audio.model_dump(mode="json") == clip["audio"]
    assert value == TimelineSnapshot.model_validate(value.model_dump(mode="json"))


@pytest.mark.parametrize("kind", ["VIDEO", "VOICE", "SFX", "SUBTITLE"])
def test_background_configuration_cannot_be_used_on_other_tracks(kind):
    raw, _ = audio_snapshot(kind=kind)
    with pytest.raises(ValidationError, match="音频用途"):
        TimelineSnapshot.model_validate(raw)


@pytest.mark.parametrize("role", ["AMBIENCE", "TRANSITION"])
def test_sound_effect_configuration_cannot_be_used_on_music_track(role):
    raw, _ = audio_snapshot(role=role)
    with pytest.raises(ValidationError, match="音频用途"):
        TimelineSnapshot.model_validate(raw)


@pytest.mark.parametrize("field", ["gain_milli", "fade_in_ms", "fade_out_ms"])
@pytest.mark.parametrize("value", [-1, True, 1.5, "1000"])
def test_audio_rejects_coerced_negative_or_fractional_gain_and_fades(field, value):
    raw, clip = audio_snapshot()
    clip["audio"][field] = value
    with pytest.raises(ValidationError):
        TimelineSnapshot.model_validate(raw)


@pytest.mark.parametrize(
    "points",
    [
        [{"offset_ms": 0, "gain_milli": 1000}],
        [{"offset_ms": 1, "gain_milli": 1000}, {"offset_ms": 3000, "gain_milli": 0}],
        [{"offset_ms": 0, "gain_milli": 1000}, {"offset_ms": 3001, "gain_milli": 0}],
        [{"offset_ms": 0, "gain_milli": 1000}, {"offset_ms": 2999, "gain_milli": 0}],
        [{"offset_ms": 0, "gain_milli": 1000}, {"offset_ms": 0, "gain_milli": 0}],
        [{"offset_ms": 0, "gain_milli": 1000}, {"offset_ms": True, "gain_milli": 0}],
        [{"offset_ms": 0, "gain_milli": 1000}, {"offset_ms": 3000, "gain_milli": 4001}],
        [{"offset_ms": index, "gain_milli": 1000} for index in range(65)],
    ],
)
def test_audio_envelope_rejects_missing_edges_duplicate_unordered_or_excess_points(points):
    raw, clip = audio_snapshot()
    clip["audio"]["envelope"] = points
    with pytest.raises(ValidationError):
        TimelineSnapshot.model_validate(raw)


def test_audio_fades_accept_touching_windows_and_reject_overlap():
    raw, clip = audio_snapshot()
    clip["audio"].update(fade_in_ms=1000, fade_out_ms=2000)
    TimelineSnapshot.model_validate(raw)
    clip["audio"]["fade_out_ms"] = 2001
    with pytest.raises(ValidationError, match="淡入淡出"):
        TimelineSnapshot.model_validate(raw)


@pytest.mark.parametrize(
    "extra", [{"filter": "volume=10"}, {"command": "run"}, {"purpose_role": "ACTION"}]
)
def test_audio_configuration_rejects_untrusted_commands_and_unknown_roles(extra):
    with pytest.raises(ValidationError):
        ClipAudio.model_validate({"purpose_role": "BACKGROUND", **extra})
