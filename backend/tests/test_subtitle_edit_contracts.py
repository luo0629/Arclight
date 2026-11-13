"""Human subtitle edits preserve source identities and require explicit timing decisions."""

from copy import deepcopy
from uuid import uuid4

import pytest
from pydantic import ValidationError
from test_subtitle_contracts import cue, document

from arclight.domain.subtitles import (
    EditedSubtitleDocument,
    SubtitleCueEdit,
    SubtitleEdit,
    srt_bytes,
    visible_cues,
)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"cue_no": 1, "text": "更清晰的字幕。"}, id="text"),
        pytest.param({"cue_no": 1, "start_ms": 0, "end_ms": 100}, id="manual-range"),
        pytest.param({"cue_no": 1, "sync_to_audio": True}, id="audio-sync"),
    ],
)
def test_subtitle_edit_accepts_explicit_text_range_or_audio_sync(change):
    parsed = SubtitleCueEdit.model_validate(change)
    assert parsed.model_dump(exclude_unset=True) == change


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"cue_no": 1}, id="no-change"),
        pytest.param({"cue_no": 0, "text": "字幕"}, id="invalid-cue-number"),
        pytest.param({"cue_no": 1, "text": None}, id="explicit-null-text"),
        pytest.param({"cue_no": 1, "start_ms": None, "end_ms": 100}, id="null-range"),
        pytest.param({"cue_no": 1, "start_ms": 10}, id="missing-end"),
        pytest.param({"cue_no": 1, "end_ms": 100}, id="missing-start"),
        pytest.param({"cue_no": 1, "start_ms": 100, "end_ms": 100}, id="empty-range"),
        pytest.param({"cue_no": 1, "start_ms": 100, "end_ms": 99}, id="backwards-range"),
        pytest.param({"cue_no": 1, "start_ms": True, "end_ms": 100}, id="boolean-time"),
        pytest.param({"cue_no": 1, "start_ms": "0", "end_ms": 100}, id="coerced-time"),
        pytest.param(
            {"cue_no": 1, "start_ms": 0, "end_ms": 100, "sync_to_audio": True},
            id="sync-and-manual-range",
        ),
        pytest.param({"cue_no": 1, "text": "  \n\t"}, id="blank-text"),
        pytest.param({"cue_no": 1, "text": "第一段\n\n第二段"}, id="srt-cue-separator"),
        pytest.param({"cue_no": 1, "text": "字幕\x00"}, id="control-character"),
    ],
)
def test_subtitle_edit_rejects_ambiguous_or_invalid_cue_changes(change):
    with pytest.raises(ValidationError):
        SubtitleCueEdit.model_validate(change)


@pytest.mark.parametrize(
    "override",
    [
        pytest.param({"changes": []}, id="empty-changes"),
        pytest.param(
            {"changes": [{"cue_no": 1, "text": "甲"}, {"cue_no": 1, "text": "乙"}]},
            id="duplicate-cue",
        ),
        pytest.param({"changes": [{"cue_no": 1, "text": "甲"}] * 257}, id="unbounded-changes"),
        pytest.param({"expected_revision": -1}, id="negative-revision"),
        pytest.param({"expected_revision": True}, id="boolean-revision"),
    ],
)
def test_subtitle_edit_request_requires_bounded_unique_cues_and_strict_cas(override):
    payload = {
        "creation_key": str(uuid4()),
        "expected_revision": 0,
        "changes": [{"cue_no": 1, "text": "字幕"}],
        **override,
    }
    with pytest.raises(ValidationError):
        SubtitleEdit.model_validate(payload)


def test_subtitle_edit_never_accepts_client_source_identity_or_target_replacement():
    for field in ("line_id", "audio_asset_version_id", "source_clip_id", "speaker"):
        with pytest.raises(ValidationError):
            SubtitleCueEdit.model_validate({"cue_no": 1, "text": "字幕", field: str(uuid4())})
    with pytest.raises(ValidationError):
        SubtitleEdit.model_validate(
            {
                "creation_key": str(uuid4()),
                "expected_revision": 0,
                "changes": [{"cue_no": 1, "text": "字幕"}],
                "target_asset_id": str(uuid4()),
            }
        )


def test_subtitle_display_document_preserves_source_cues_and_exports_effective_srt():
    source = document([cue(start_ms=125, duration_ms=100), cue(cue_no=2, start_ms=875)])
    original = source.model_dump(mode="json")
    edited = EditedSubtitleDocument.model_validate(
        {
            **original,
            "schema_version": "subtitle-track/2",
            "display_cues": [
                {"cue_no": 1, "text": "人工字幕\n第二行", "start_ms": 150, "end_ms": 300},
                {"cue_no": 2, "text": "下一句", "start_ms": 300, "end_ms": 500},
            ],
            "edit_source_version_id": str(uuid4()),
            "edit_source_checksum": "d" * 64,
            "edit_source_document_sha256": "e" * 64,
        }
    )
    before = deepcopy(edited.model_dump(mode="json"))
    first = visible_cues(edited)[0]
    assert first.text == "人工字幕\n第二行" and first.start_ms == 150 and first.duration_ms == 150
    for field in type(source.cues[0]).model_fields:
        if field not in {"text", "start_ms", "duration_ms"}:
            assert getattr(first, field) == getattr(source.cues[0], field)
    assert edited.model_dump(mode="json") == before
    assert [item.model_dump(mode="json") for item in edited.cues] == original["cues"]
    assert (
        srt_bytes(edited)
        == (
            "1\n00:00:00,150 --> 00:00:00,300\n人工字幕\n第二行\n\n"
            "2\n00:00:00,300 --> 00:00:00,500\n下一句\n\n"
        ).encode()
    )
