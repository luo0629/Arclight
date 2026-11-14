"""Public submissions reject content the bounded renderer cannot faithfully encode."""

import base64
import io
from copy import deepcopy
from random import Random
from uuid import UUID, uuid4

import pytest
from PIL import Image
from render_admission_helpers import assert_blocked, edited, ready_manifest, render_path
from render_manifest_helpers import audio_case, render_case, review_case, stored_file, timeline_case
from sqlalchemy import func, select
from test_timeline_saves import duration_edit

from arclight.application import render_manifests
from arclight.application.render_plans import build_plan
from arclight.domain.render_manifests import RenderManifestSnapshot
from arclight.media.execution import command_for
from arclight.media.render_plan import MAX_FRAMES, MAX_PIXEL_FRAMES, RenderPlan
from arclight.media.versions import renderer_software
from arclight.persistence.models import Project, RenderExecution, RenderJob

__all__ = ["audio_case", "render_case", "review_case", "timeline_case"]


def rejected(client, manifest, code):
    response = client.post(render_path(manifest, "jobs"), json={"creation_key": str(uuid4())})
    assert_blocked(response, code)
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(RenderJob)) == 0
        assert db.scalar(select(func.count()).select_from(RenderExecution)) == 0


@pytest.mark.parametrize("kind", ["VOICE", "MUSIC", "SFX", "SUBTITLE"])
def test_each_nonempty_track_is_consumed_by_actual_public_render(client, audio_case, kind):
    from render_job_helpers import read, run, submit

    original, font_id, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    for track in snapshot["tracks"]:
        if track["kind"] not in {"VIDEO", kind}:
            track["clips"] = []
        elif kind == "SUBTITLE" and track["kind"] == kind:
            for item in track["clips"]:
                item["audio_version_id"] = None
    timeline = edited(client, original, snapshot)
    manifest, _ = ready_manifest(client, timeline, font_file_id=str(font_id))
    with client.app.state.session_factory() as db:
        plan = RenderPlan.model_validate_json(
            build_plan(
                db,
                client.app.state.settings,
                RenderManifestSnapshot.model_validate(manifest["snapshot"]),
            )
        )
    assert len(plan.audio_clips) == (0 if kind == "SUBTITLE" else 1)
    assert bool(plan.subtitles) == (kind == "SUBTITLE")
    if plan.subtitles:
        assert plan.subtitles.cues[0].text == "A"
    job, _ = submit(client, manifest)
    assert run(client, job) == "SUCCEEDED"
    final = read(client, job)
    assert final["progress"] == 100 and final["output_file_id"]
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest


@pytest.mark.parametrize("starts", [(0, 400), (900, 100)])
def test_public_submission_rejects_global_subtitle_overlap_or_reversed_order(
    client, audio_case, starts
):
    original, font_id, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    track = next(item for item in snapshot["tracks"] if item["kind"] == "SUBTITLE")
    template = track["clips"][0]
    track["clips"] = [
        {
            **template,
            "id": str(uuid4()),
            "ordinal": index,
            "start_ms": start,
            "duration_ms": 500 if starts[0] == 0 else 100,
        }
        for index, start in enumerate(starts)
    ]
    timeline = edited(client, original, snapshot)
    manifest, _ = ready_manifest(client, timeline, font_file_id=str(font_id))
    rejected(client, manifest, "RENDER_SUBTITLE_RANGE_INVALID")


def test_public_submission_rejects_audio_decoder_work_before_creating_execution(client, audio_case):
    original, font_id, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    track = next(item for item in snapshot["tracks"] if item["kind"] == "MUSIC")
    template = track["clips"][0]
    track["clips"] = [{**template, "id": str(uuid4()), "ordinal": index} for index in range(17)]
    timeline = edited(client, original, snapshot)
    manifest, _ = ready_manifest(client, timeline, font_file_id=str(font_id))
    rejected(client, manifest, "RENDER_AUDIO_WORK_LIMIT")


def test_historical_software_manifest_remains_readable_but_cannot_execute(
    client, render_case, monkeypatch
):
    old = renderer_software().model_copy(update={"renderer_version": "arclight-pyav/1"})
    with monkeypatch.context() as patch:
        patch.setattr(render_manifests, "renderer_software", lambda: old)
        manifest, _ = ready_manifest(client, render_case)
    rejected(client, manifest, "RENDER_SOFTWARE_CHANGED")


def test_caption_library_versions_are_frozen_and_changed_libraries_require_new_approval(
    client, render_case, monkeypatch
):
    current = renderer_software()
    assert current.caption_libraries["pillow"]
    assert current.caption_libraries["freetype2"] != "unavailable"
    old = current.model_copy(
        update={"caption_libraries": {**current.caption_libraries, "pillow": "historical-version"}}
    )
    with monkeypatch.context() as patch:
        patch.setattr(render_manifests, "renderer_software", lambda: old)
        manifest, _ = ready_manifest(client, render_case)
    assert manifest["snapshot"]["software"]["caption_libraries"]["pillow"] == "historical-version"
    rejected(client, manifest, "RENDER_SOFTWARE_CHANGED")


@pytest.mark.parametrize("clip_duration", [160_000, 30_000])
def test_public_submission_rejects_frame_or_pixel_workload_before_creating_execution(
    client, episode, render_case, clip_duration
):
    snapshot = duration_edit({"snapshot": render_case["snapshot"]}, clip_duration)["snapshot"]
    snapshot["default_duration_ms"] = 500
    if clip_duration == 30_000:
        # Within the frame cap, but above the pixel-frame cap at 4K.
        snapshot["output_profile"].update(width=3840, height=2160)
        with client.app.state.session_factory.begin() as db:
            db.get(Project, UUID(episode["project_id"])).output_profile = snapshot["output_profile"]
    timeline = edited(client, render_case, snapshot)
    manifest, _ = ready_manifest(
        client, timeline, profile="FORMAL" if clip_duration == 30_000 else "PREVIEW"
    )
    output = manifest["snapshot"]["output"]
    frames = (snapshot["duration_ms"] * output["fps"] + 999) // 1000
    if clip_duration == 30_000:
        assert (output["width"], output["height"]) == (3840, 2160)
        assert frames <= MAX_FRAMES
        assert frames * output["width"] * output["height"] > MAX_PIXEL_FRAMES
    else:
        assert frames > MAX_FRAMES
    rejected(client, manifest, "RENDER_WORK_LIMIT")


def test_render_plan_preserves_large_images_without_a_total_byte_ceiling(
    client, episode, render_case
):
    value, _ = ready_manifest(client, render_case)
    image = Image.frombytes("RGB", (2000, 2000), Random(19).randbytes(2000 * 2000 * 3))
    encoded = io.BytesIO()
    image.save(encoded, format="PNG")
    data = encoded.getvalue()
    assert len(data) > 1024**2
    file_id, _, checksum = stored_file(client, episode["project_id"], data, "image/png")
    snapshot = deepcopy(value["snapshot"])
    for source in snapshot["sources"]:
        if source["file"] is not None:
            source["sha256"] = checksum
            source["file"].update(file_id=str(file_id), sha256=checksum, byte_size=len(data))
    with client.app.state.session_factory() as db:
        raw_plan = build_plan(
            db, client.app.state.settings, RenderManifestSnapshot.model_validate(snapshot)
        )
    assert len(raw_plan) > 16 * 1024**2
    plan = RenderPlan.model_validate_json(raw_plan)
    assert plan.clips
    assert all(base64.b64decode(clip.image, validate=True) == data for clip in plan.clips)
    # Repeat an already compiled exact reference without another storage read or media encode.
    expanded = RenderPlan(
        output=plan.output,
        duration_ms=2000,
        clips=tuple(
            plan.clips[0].model_copy(
                update={
                    "start_ms": index * 500,
                    "duration_ms": 500,
                    "fade_in_ms": 0,
                    "fade_out_ms": 0,
                }
            )
            for index in range(4)
        ),
    )
    expanded_bytes = expanded.model_dump_json().encode()
    assert len(expanded_bytes) > 48 * 1024**2
    assert command_for("render", "timeline").input_limit is None
    assert len(RenderPlan.model_validate_json(expanded_bytes).clips) == 4
