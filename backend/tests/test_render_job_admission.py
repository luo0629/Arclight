"""Public submissions reject content the bounded renderer cannot faithfully encode."""

from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import assert_blocked, edited, ready_manifest, render_path
from render_manifest_helpers import audio_case, render_case, review_case, timeline_case
from sqlalchemy import func, select
from test_timeline_saves import duration_edit

from arclight.application import render_manifests
from arclight.media.render_plan import MAX_FRAMES, MAX_PIXEL_FRAMES
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
def test_each_nonempty_unimplemented_track_is_rejected_without_silent_export(
    client, audio_case, kind
):
    original, font_id, _ = audio_case
    snapshot = deepcopy(original["snapshot"])
    for track in snapshot["tracks"]:
        if track["kind"] not in {"VIDEO", kind}:
            track["clips"] = []
        elif kind == "SUBTITLE" and track["kind"] == kind:
            # A subtitle-only line has no consumed voice version.
            for clip in track["clips"]:
                clip["audio_version_id"] = None
    timeline = edited(client, original, snapshot)
    manifest, _ = ready_manifest(client, timeline, font_file_id=str(font_id))
    rejected(client, manifest, "RENDER_TRACK_UNSUPPORTED")


def test_historical_software_manifest_remains_readable_but_cannot_execute(
    client, render_case, monkeypatch
):
    old = renderer_software().model_copy(update={"renderer_version": "arclight-pyav/1"})
    with monkeypatch.context() as patch:
        patch.setattr(render_manifests, "renderer_software", lambda: old)
        manifest, _ = ready_manifest(client, render_case)
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
