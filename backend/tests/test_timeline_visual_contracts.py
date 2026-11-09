from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from test_timeline_contracts import snapshot

from arclight.domain.timeline_visuals import ClipTransition, ClipVisual, CropRect
from arclight.domain.timelines import TimelineSnapshot
from arclight.domain.versions import digest


def editable_snapshot(durations=(1000, 1000, 1000)):
    value = snapshot()
    value.update(schema_version=2, duration_ms=sum(durations))
    template = value["tracks"][0]["clips"][0]
    cursor = 0
    clips = []
    for ordinal, duration in enumerate(durations):
        clip = deepcopy(template)
        clip.update(
            id=str(uuid4()),
            shot_id=str(uuid4()),
            ordinal=ordinal,
            start_ms=cursor,
            duration_ms=duration,
            visual={},
        )
        clips.append(clip)
        cursor += duration
    value["tracks"][0]["clips"] = clips
    for track in value["tracks"][1:]:
        track["clips"] = []
    return value


def test_legacy_serialization_and_checksum_keep_the_exact_original_field_set():
    raw = snapshot()
    expected = deepcopy(raw)
    expected["schema_version"] = 1
    for track in expected["tracks"]:
        for clip in track["clips"]:
            for name in (
                "shot_id",
                "script_version_id",
                "line_id",
                "audio_version_id",
                "review_id",
                "review_storyboard_id",
                "review_shot_id",
            ):
                clip.setdefault(name, None)
            clip["source_start_ms"] = 0
    value = TimelineSnapshot.model_validate(raw)
    assert value.model_dump(mode="json") == expected
    assert digest(value.model_dump(mode="json")) == digest(expected)
    assert all(
        window.incoming_ms == window.outgoing_ms == 0 for window in value.video_fades().values()
    )


@pytest.mark.parametrize(
    "value",
    [
        {"x": -1},
        {"x": 10000},
        {"y": -1},
        {"y": 10000},
        {"width": 0},
        {"height": 10001},
        {"x": 9000, "width": 1001},
        {"y": 9000, "height": 1001},
        {"x": True},
        {"width": 8000.0},
        {"height": "8000"},
        {"x": float("nan")},
        {"filter": "movie=https://untrusted.example/input"},
    ],
)
def test_crop_rejects_empty_outside_coerced_and_untrusted_values(value):
    with pytest.raises(ValidationError):
        CropRect.model_validate(value)


def test_crop_accepts_exact_source_edges_and_nonempty_rectangles():
    assert CropRect(x=2000, y=1000, width=8000, height=9000).x == 2000
    assert CropRect(x=9999, y=9999, width=1, height=1).width == 1


@pytest.mark.parametrize(
    ("start", "end"),
    [
        ({}, {"x": 1000, "y": 1000, "width": 8000, "height": 8000}),
        ({"x": 1000, "y": 1000, "width": 8000, "height": 8000}, {}),
        (
            {"x": 0, "y": 1000, "width": 8000, "height": 8000},
            {"x": 2000, "y": 1000, "width": 8000, "height": 8000},
        ),
        (
            {"x": 1000, "y": 2000, "width": 8000, "height": 8000},
            {"x": 1000, "y": 0, "width": 8000, "height": 8000},
        ),
    ],
)
def test_bounded_push_pull_and_horizontal_vertical_pan_roundtrip(start, end):
    visual = ClipVisual.model_validate(
        {"crop": start, "motion": {"kind": "LINEAR", "end_crop": end}}
    )
    assert ClipVisual.model_validate_json(visual.model_dump_json()) == visual


@pytest.mark.parametrize(
    "value",
    [
        {"motion": {"kind": "STATIC", "end_crop": {}}},
        {"motion": {"kind": "LINEAR"}},
        {"motion": {"kind": "arbitrary-filter"}},
        {"motion": {"kind": "LINEAR", "end_crop": {}}},
        {
            "motion": {
                "kind": "LINEAR",
                "end_crop": {"width": 9000, "height": 8000},
            }
        },
        {
            "motion": {
                "kind": "LINEAR",
                "end_crop": {"x": 1500, "y": 1500, "width": 7000, "height": 7000},
            }
        },
        {
            "crop": {"width": 6000, "height": 6000},
            "motion": {
                "kind": "LINEAR",
                "end_crop": {"x": 4000, "width": 6000, "height": 6000},
            },
        },
        {"motion": {"kind": "LINEAR", "end_crop": {"x": -1}}},
        {"motion": {"kind": "STATIC", "expression": "system(command)"}},
    ],
)
def test_motion_rejects_ambiguous_distorting_excessive_or_untrusted_paths(value):
    with pytest.raises(ValidationError):
        ClipVisual.model_validate(value)


@pytest.mark.parametrize(
    "value",
    [
        {"kind": "CUT", "duration_ms": 100},
        {"kind": "FADE", "duration_ms": 0},
        {"kind": "FADE", "duration_ms": 99},
        {"kind": "FADE", "duration_ms": 2001},
        {"kind": "FADE", "duration_ms": "300"},
        {"kind": "FADE", "duration_ms": 300.0},
        {"kind": "FADE", "duration_ms": True},
        {"kind": "xfade=custom"},
        {"output_path": "../outside.mp4"},
    ],
)
def test_transition_rejects_unsupported_unbounded_and_coerced_parameters(value):
    with pytest.raises(ValidationError):
        ClipTransition.model_validate(value)


def test_fade_windows_split_odd_milliseconds_and_preserve_all_shot_durations():
    raw = editable_snapshot((1000, 700, 1000))
    clips = raw["tracks"][0]["clips"]
    clips[1]["visual"]["transition"] = {"kind": "FADE", "duration_ms": 301}
    clips[2]["visual"]["transition"] = {"kind": "FADE", "duration_ms": 401}
    value = TimelineSnapshot.model_validate(raw)
    windows = value.video_fades()
    assert [
        (windows[UUID(c["id"])].incoming_ms, windows[UUID(c["id"])].outgoing_ms) for c in clips
    ] == [
        (0, 150),
        (151, 200),
        (201, 0),
    ]
    assert value.duration_ms == 2700
    assert TimelineSnapshot.model_validate_json(value.model_dump_json()) == value


@pytest.mark.parametrize(
    "kind",
    [
        "legacy_with_visual",
        "unknown_schema",
        "missing_visual",
        "audio_visual",
        "first_fade",
        "overlapping_fades",
        "ordinal_gap",
        "time_gap",
        "time_overlap",
        "incomplete_video",
    ],
)
def test_editable_sequence_rejects_ambiguous_schema_and_invalid_timing(kind):
    raw = editable_snapshot((1000, 100, 1000))
    clips = raw["tracks"][0]["clips"]
    if kind == "legacy_with_visual":
        raw["schema_version"] = 1
    elif kind == "unknown_schema":
        raw["schema_version"] = 3
    elif kind == "missing_visual":
        del clips[1]["visual"]
    elif kind == "audio_visual":
        other = snapshot()["tracks"][2]["clips"][0]
        other.update(duration_ms=1000, visual={})
        raw["tracks"][2]["clips"] = [other]
    elif kind == "first_fade":
        clips[0]["visual"]["transition"] = {"kind": "FADE", "duration_ms": 100}
    elif kind == "overlapping_fades":
        for clip in clips[1:]:
            clip["visual"]["transition"] = {"kind": "FADE", "duration_ms": 401}
    elif kind == "ordinal_gap":
        clips[-1]["ordinal"] = 4
    elif kind == "time_gap":
        clips[1]["start_ms"] += 1
    elif kind == "time_overlap":
        clips[1]["start_ms"] -= 1
    else:
        clips.pop()
    with pytest.raises(ValidationError):
        TimelineSnapshot.model_validate(raw)
