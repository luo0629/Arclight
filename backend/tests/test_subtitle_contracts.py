"""Subtitle timing, source identity and SRT serialization contracts."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError

from arclight.application.subtitles import document_inputs
from arclight.domain.subtitles import (
    LegacySubtitleAssetMetadata,
    SubtitleAssetMetadata,
    SubtitleCue,
    SubtitleDocument,
    SubtitleGenerate,
    srt_bytes,
    srt_timestamp,
)


def cue(**overrides):
    return SubtitleCue(
        **{
            "cue_no": 1,
            "text": "你好，世界。",
            "speaker": "小明",
            "line_type": "dialogue",
            "start_ms": 1023,
            "duration_ms": 987,
            "source_start_ms": 20,
            "source_clip_id": uuid4(),
            "script_version_id": uuid4(),
            "script_sha256": "a" * 64,
            "line_id": "line-one",
            "audio_asset_version_id": uuid4(),
            "audio_sha256": "b" * 64,
            "audio_snapshot_id": uuid4(),
            "audio_binding_id": uuid4(),
            "audio_file_id": uuid4(),
            **overrides,
        }
    )


def document(cues=None):
    return SubtitleDocument(
        episode_id=uuid4(),
        timeline_id=uuid4(),
        timeline_sha256="c" * 64,
        cues=cues or [cue()],
    )


def test_srt_uses_timeline_milliseconds_and_exact_utf8_dialogue():
    value = document()
    assert srt_bytes(value) == "1\n00:00:01,023 --> 00:00:02,010\n你好，世界。\n\n".encode()
    assert srt_timestamp(3_600_000) == "01:00:00,000"
    assert value.cues[0].source_start_ms == 20


@pytest.mark.parametrize("text", ["", " ", "一\n\n二", "一\r\n二", "一\x00二"])
def test_srt_rejects_empty_sections_and_control_characters(text):
    with pytest.raises(ValidationError):
        cue(text=text)


def test_document_rejects_changed_identity_order_and_coerced_timing():
    original = document()
    for field, value in (("start_ms", "1023"), ("duration_ms", 0), ("cue_no", 2)):
        changed = original.model_dump(mode="json")
        changed["cues"][0][field] = value
        with pytest.raises(ValidationError):
            SubtitleDocument.model_validate(changed)
    repeated = deepcopy(original.model_dump(mode="json"))
    repeated["cues"].append({**repeated["cues"][0], "cue_no": 2})
    with pytest.raises(ValidationError, match="来源片段"):
        SubtitleDocument.model_validate(repeated)


def test_generation_requires_explicit_target_revision_and_forbids_client_paths():
    request = {"creation_key": uuid4(), "timeline_id": uuid4()}
    SubtitleGenerate(**request)
    for extra in (
        {"expected_revision": 0},
        {"target_asset_id": uuid4()},
        {"object_key": "private/key.srt"},
        {"command": "ffmpeg"},
    ):
        with pytest.raises(ValidationError):
            SubtitleGenerate(**request, **extra)


def test_production_marker_is_explicit_and_legacy_metadata_remains_distinct():
    episode_id = uuid4()
    assert LegacySubtitleAssetMetadata().episode_id is None
    assert LegacySubtitleAssetMetadata(episode_id=episode_id).episode_id == episode_id
    with pytest.raises(ValidationError):
        SubtitleAssetMetadata(episode_id=episode_id)
    with pytest.raises(ValidationError):
        LegacySubtitleAssetMetadata(episode_id=episode_id, subtitle_production=1)


def test_dependency_inputs_deduplicate_audio_and_scope_each_exact_script_line():
    first = cue()
    second = cue(
        cue_no=2,
        start_ms=5000,
        script_version_id=first.script_version_id,
        audio_asset_version_id=first.audio_asset_version_id,
        line_id="line-two",
    )
    value = document([first, second])
    inputs = document_inputs(value)
    assert len(inputs) == 3
    assert inputs[0].source_asset_version_id == value.timeline_id
    assert inputs[0].dependency_type == "SEQUENCE_DEPENDENCY"
    assert inputs[1].source_asset_version_id == first.audio_asset_version_id
    assert inputs[2].selectors == ["dialogue:line-one", "dialogue:line-two"]
