"""Existing subtitle production and asynchronous delivery consume the same frozen inputs."""

from copy import deepcopy
from io import BytesIO
from uuid import UUID

import av
import pytest
from render_admission_helpers import ready_manifest, request_export
from render_job_helpers import read, run, submit
from render_manifest_helpers import render_case, review_case, stored_file, timeline_case
from render_report_helpers import report_read
from test_human_reviews import decide, resume
from test_render_captions import caption_font
from test_subtitle_edits import require_edit
from test_subtitle_lifecycle import approve_subtitle
from test_subtitle_production import save_timeline, subtitle_case, subtitle_source

from arclight.application.render_plans import build_plan, legacy_cues
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import RenderManifestSnapshot
from arclight.media.render_plan import RenderPlan
from arclight.persistence.models import StoredFile
from arclight.storage.objects import ObjectStorage

__all__ = ["render_case", "review_case", "timeline_case", "subtitle_source", "subtitle_case"]


@pytest.mark.parametrize("edited", [False, True])
def test_actual_production_v1_v2_text_timing_and_formal_report_preserve_source_history(
    client, subtitle_case, edited
):
    case = subtitle_case
    original = deepcopy(case["track"])
    original_srt = client.get(f"/api/v1/subtitle-tracks/{original['id']}/srt").content
    if edited:
        track = require_edit(
            client, case, [{"cue_no": 1, "text": "编辑显示内容", "start_ms": 250, "end_ms": 500}]
        )
    else:
        track = case["track"]
    approve_subtitle(client, {**case, "track": track})
    snapshot = deepcopy(case["timeline"]["snapshot"])
    next(item for item in snapshot["tracks"] if item["kind"] == "SUBTITLE")["clips"] = track[
        "clips"
    ]
    timeline = save_timeline(client, case["timeline"], snapshot)
    expected_texts = [cue["text"] for cue in track["cues"]]
    font_id, _, _ = stored_file(
        client, case["episode"]["project_id"], caption_font("".join(expected_texts)), "font/ttf"
    )
    manifest, _ = ready_manifest(
        client,
        timeline,
        profile="FORMAL",
        font_file_id=str(font_id),
        subtitle_style={"font_size": 20, "margin_bottom": 16},
    )
    parsed = RenderManifestSnapshot.model_validate(manifest["snapshot"])
    with client.app.state.session_factory() as db:
        plan = RenderPlan.model_validate_json(build_plan(db, client.app.state.settings, parsed))
    assert [cue.text for cue in plan.subtitles.cues] == expected_texts
    assert [(cue.start_ms, cue.duration_ms) for cue in plan.subtitles.cues] == [
        (cue["start_ms"], cue["duration_ms"]) for cue in track["cues"]
    ]
    assert len(plan.audio_clips) == 2 and len(plan.audio_sources) == 2
    assert all("-->" not in cue.text for cue in plan.subtitles.cues)
    requested = request_export(client, manifest)
    decision = decide(client, requested)
    assert decision.status_code == 200, decision.text
    assert resume(client, requested, decision.json()).status_code == 200
    job, _ = submit(client, manifest)
    assert run(client, job) == "SUCCEEDED", read(client, job)
    final = read(client, job)
    assert final["progress"] == 100 and final["report_id"]
    report = report_read(client, case["episode"], final["artifact_id"])
    details = report["report"]["details"]
    assert details["checks"]["subtitle_range_valid"] is True
    assert details["sources"] == manifest["snapshot"]["sources"]
    assert details["font"] == manifest["snapshot"]["font"]
    assert details["software"]["renderer_version"] == "arclight-pyav/3"
    with client.app.state.session_factory() as db:
        output = db.get(StoredFile, UUID(final["output_file_id"]))
        movie = ObjectStorage(client.app.state.settings).read(
            output.object_key, None, output.checksum
        )
    # The fixture uses colored images. A caption's white pixels distinguish the drawn text.
    observed = []
    with av.open(BytesIO(movie)) as container:
        for frame in container.decode(video=0):
            image = frame.to_image()
            pixels = [
                min(image.getpixel((x, y))) > 200
                for y in range(image.height)
                for x in range(image.width)
            ]
            observed.append(sum(pixels))
    expected = [
        any(
            cue.start_ms <= index * 1000 / plan.output.fps < cue.start_ms + cue.duration_ms
            for cue in plan.subtitles.cues
        )
        for index in range(plan.frames)
    ]
    assert [count > 5 for count in observed] == expected
    assert (
        client.get(f"/api/v1/subtitle-tracks/{original['id']}").json()["cues"] == original["cues"]
    )
    assert client.get(f"/api/v1/subtitle-tracks/{original['id']}/srt").content == original_srt
    assert client.get(f"/api/v1/timelines/{case['timeline']['id']}").json() == case["timeline"]
    assert run(client, job) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        assert (
            ObjectStorage(client.app.state.settings).read(output.object_key, None, output.checksum)
            == movie
        )


def test_legacy_literal_caption_and_standard_srt_keep_text_without_timestamp_headers():
    assert legacy_cues(" 中文\n配音 ".encode()) == " 中文\n配音 "
    cues = legacy_cues("1\r\n00:00:00,125 --> 00:00:00,225\r\n中文\r\n配音\r\n\r\n".encode())
    assert [(cue.text, cue.start_ms, cue.duration_ms) for cue in cues] == [("中文\n配音", 125, 100)]


@pytest.mark.parametrize(
    "data",
    [
        b"\xff",
        b"1\n00:00:00,300 --> 00:00:00,200\nA",
        b"1\n00:00:99,000 --> 00:00:99,100\nA",
        b"1\nhttps://private --> file:///etc/passwd\nA",
        b"2\n00:00:00,000 --> 00:00:00,100\nA",
    ],
)
def test_ambiguous_legacy_srt_or_non_utf8_is_rejected(data):
    with pytest.raises(DomainError) as error:
        legacy_cues(data)
    assert error.value.code == "RENDER_SUBTITLE_INVALID"
