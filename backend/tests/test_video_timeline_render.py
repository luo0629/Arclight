"""VIDEO-03: exact selection, video trim, sealed encoding and immutable delivery."""

import base64
import math
from array import array
from copy import deepcopy
from fractions import Fraction
from io import BytesIO
from uuid import UUID, uuid4

import av
import pytest
from alembic import command
from PIL import Image, ImageDraw
from render_admission_helpers import (
    assert_blocked,
    edited,
    ready_manifest,
    render_path,
    request_export,
)
from render_job_helpers import read, run, submit
from render_manifest_helpers import manifest_url, stored_file
from render_report_helpers import report_read
from sqlalchemy import text
from test_asset_selection import choose
from test_generation_video_inputs import MODEL
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_human_reviews import decide, resume
from test_render_captions import caption_font
from test_render_encoder import clip, profile
from test_render_mixing import decoded_audio, rms, sound, tone
from test_series_performance_admission import performance_case
from test_timelines import created_timeline, timeline_url
from test_video_candidates import finish_video, imported_rows
from test_video_review_admission import lifecycle, report_payload, write_report

from arclight.application import generation_artifacts
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.versions import digest
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.render_plan import RenderClip, RenderPlan, RenderVideo
from arclight.persistence.models import AssetVersion, Project, StoredFile
from arclight.storage.objects import ObjectStorage


def synthetic_movie(*, width=1280, height=720, seconds=6, moving=True, audio=True, stamps=None):
    output = BytesIO()
    with av.open(output, "w", format="mp4") as container:
        video = container.add_stream("libx264", rate=24)
        video.width, video.height, video.pix_fmt = width, height, "yuv420p"
        video.options = {"preset": "ultrafast", "threads": "1"}
        video.codec_context.time_base = Fraction(1, 24)
        samples_per_frame = 48000 // 24
        if audio:
            original = container.add_stream("aac", rate=48000)
            original.layout = "mono"
            original.codec_context.thread_count = 1
        for index, timestamp in enumerate(stamps if stamps is not None else range(seconds * 24)):
            with Image.new("RGB", (width, height), (24, 37, 54)) as image:
                # The red column moves in source coordinates. Its position is measurable after mux.
                x = (index * width // 32) % (width * 3 // 4) if moving else 0
                ImageDraw.Draw(image).rectangle(
                    (x, 0, x + width // 4 - 1, height), fill=(205, 27, 42)
                )
                frame = av.VideoFrame.from_image(image)
                frame.pts, frame.time_base = timestamp, Fraction(1, 24)
                for packet in video.encode(frame):
                    container.mux(packet)
            if audio and index >= 24:
                first = index * samples_per_frame
                frame_audio = av.AudioFrame(format="fltp", layout="mono", samples=samples_per_frame)
                raw = array(
                    "f",
                    (
                        0.24 * math.sin(2 * math.pi * 600 * sample / 48000)
                        for sample in range(first, first + samples_per_frame)
                    ),
                ).tobytes()
                plane = frame_audio.planes[0]
                plane.update(raw + bytes(plane.buffer_size - len(raw)))
                frame_audio.sample_rate = 48000
                frame_audio.pts, frame_audio.time_base = first, Fraction(1, 48000)
                for packet in original.encode(frame_audio):
                    container.mux(packet)
        for stream in container.streams:
            for packet in stream.encode(None):
                container.mux(packet)
    return output.getvalue()


def red_bounds(movie):
    with av.open(BytesIO(movie)) as container:
        result = []
        for frame in container.decode(video=0):
            with frame.to_image(threads=1) as image:
                y = image.height // 2
                pixels = [x for x in range(image.width) if image.getpixel((x, y))[0] > 140]
                result.append(min(pixels) if pixels else None)
        return result


@pytest.fixture
def dynamic_video(client, settings, video_case):
    case = deepcopy(video_case)
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    prefix = f"/api/v1/episodes/{episode['id']}"
    patched = client.patch(prefix, json={"target_duration_ms": 6000})
    assert patched.status_code == 200, patched.text
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).output_profile = {
            "width": 256,
            "height": 144,
            "fps": 25,
        }
    script, board, _ = performance_case(
        client,
        episode,
        dynamic=True,
        duration_ms=6000,
        generation_routes=[{"provider": "seedance", "model": MODEL}],
    )
    response = client.post(
        prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}
    )
    assert response.status_code == 200, response.text
    shot = client.get(prefix + "/shots", params={"storyboard_id": board["id"]}).json()[0]
    case["body"]["review_target"] = {"storyboard_id": board["id"], "shot_id": shot["id"]}
    case["body"]["inputs"].append(
        {
            "source_asset_version_id": board["id"],
            "dependency_type": "CONTENT_DEPENDENCY",
            "selectors": ["shot:" + shot["id"]],
        }
    )
    job, attempt, provider = finish_video(client, settings, case, synthetic_movie())
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    candidate = imported_rows(client)[0]
    with client.app.state.session_factory() as db:
        version = db.get(AssetVersion, candidate.asset_version_id)
        return {
            **case,
            "episode": episode,
            "script": script,
            "board": board,
            "shot": shot,
            "job": job,
            "candidate_id": candidate.id,
            "version_id": version.id,
            "asset_id": version.asset_id,
            "root": f"/api/v1/projects/{candidate.project_id}/assets/{version.asset_id}",
        }


def approved_video(client, case):
    value = report_payload(client, case)
    record = write_report(client, value)
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        response = lifecycle(client, case, status)
        assert response.status_code == 200, response.text
    return record, response.json()


def create_body(case, **trim):
    return {
        "creation_key": str(uuid4()),
        "storyboard_id": case["board"]["id"],
        "selected_shots": [
            {
                "shot_id": case["shot"]["id"],
                "asset_version_id": str(case["version_id"]),
                "video": {"source_start_ms": 500, "duration_ms": 2500, "keep_audio": True, **trim},
            }
        ],
    }


def picture(snapshot):
    return next(track["clips"][0] for track in snapshot["tracks"] if track["kind"] == "VIDEO")


def with_voice_music_and_captions(client, case, timeline):
    project = case["project_id"]
    music, music_version, music_sha = stored_file(
        client,
        project,
        base64.b64decode(tone(duration_ms=2500, frequency=1300, amplitude=0.12).data),
        "audio/wav",
        kind="AUDIO",
    )
    voice, voice_version, voice_sha = stored_file(
        client,
        project,
        base64.b64decode(tone(duration_ms=600, frequency=900, amplitude=0.16).data),
        "audio/wav",
        kind="AUDIO",
    )
    _, subtitle, subtitle_sha = stored_file(client, project, b"AB", "text/plain", kind="SUBTITLE")
    font, _, _ = stored_file(client, project, caption_font("AB"), "font/ttf")
    snapshot = deepcopy(timeline["snapshot"])
    common = {"ordinal": 0, "start_ms": 0, "duration_ms": 2500}
    tracks = {track["kind"]: track for track in snapshot["tracks"]}
    tracks["MUSIC"]["clips"] = [
        {
            **common,
            "id": str(uuid4()),
            "source_asset_version_id": str(music_version),
            "source_sha256": music_sha,
        }
    ]
    line_range = {
        "ordinal": 0,
        "start_ms": 1000,
        "duration_ms": 600,
        "script_version_id": case["script"]["id"],
        "line_id": case["script"]["content_json"]["scenes"][0]["dialogue"][0]["line_id"],
    }
    tracks["VOICE"]["clips"] = [
        {
            **line_range,
            "id": str(uuid4()),
            "source_asset_version_id": str(voice_version),
            "source_sha256": voice_sha,
        }
    ]
    tracks["SUBTITLE"]["clips"] = [
        {
            **line_range,
            "id": str(uuid4()),
            "source_asset_version_id": str(subtitle),
            "source_sha256": subtitle_sha,
            "audio_version_id": str(voice_version),
        }
    ]
    return edited(client, timeline, snapshot), str(font), {str(music), str(voice)}


def actual_movie(client, run_state):
    with client.app.state.session_factory() as db:
        file = db.get(StoredFile, UUID(run_state["output_file_id"]))
        return ObjectStorage(client.app.state.settings).read(
            file.object_key, file.byte_size, file.checksum
        )


def test_selected_dynamic_video_renders_trimmed_motion_sound_subtitles_and_formal_history(
    client, dynamic_video
):
    case = dynamic_video
    record, version = approved_video(client, case)
    body = create_body(case)
    endpoint = timeline_url(case["episode"])
    assert_blocked(client.post(endpoint, json=body), "ASSET_NOT_USABLE")
    selection = choose(client, client.get(case["root"]).json(), version)
    assert selection.status_code == 200, selection.text
    assert_blocked(
        client.post(endpoint, json=create_body(case, source_start_ms=5000)),
        "TIMELINE_SOURCE_RANGE_INVALID",
    )
    timeline = created_timeline(client, case["episode"], body)
    frozen = picture(timeline["snapshot"])
    assert frozen["review_id"] == record["report"]["report_id"]
    assert frozen["source_start_ms"] == 500 and frozen["duration_ms"] == 2500
    assert frozen["video"] == {
        "candidate_id": str(case["candidate_id"]),
        "file_id": frozen["video"]["file_id"],
        "review_sha256": record["report_sha256"],
        "human_decision_id": None,
        "source_end_ms": 3000,
        "source_duration_ms": 6000,
        "frame_rate": 24,
        "has_audio": True,
        "keep_audio": True,
        "motion_required": True,
    }
    assert digest(timeline["snapshot"]) == timeline["content_sha256"]
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline
    with_tracks, font, audio_files = with_voice_music_and_captions(client, case, timeline)
    caption_options = {
        "font_file_id": font,
        "subtitle_style": {"font_size": 32, "margin_bottom": 20},
    }
    preview, _ = ready_manifest(client, with_tracks, **caption_options)
    assert preview["snapshot"]["output"]["fps"] == 24
    assert picture(preview["snapshot"]["timeline"])["video"] == frozen["video"]
    job, _ = submit(client, preview)
    assert run(client, job) == "SUCCEEDED", read(client, job)
    kept = actual_movie(client, read(client, job))
    red = red_bounds(kept)
    assert red[0] is not None and 92 <= red[0] <= 100
    assert len(red) == 60 and len(set(red)) >= 20
    left, _ = decoded_audio(kept)
    assert 0.075 < rms(left[4800:18000]) < 0.095
    assert 0.17 < rms(left[30000:45000]) < 0.22
    assert 0.20 < rms(left[52800:67200]) < 0.24
    caption_frames = []
    with av.open(BytesIO(kept)) as container:
        for index, frame in enumerate(container.decode(video=0)):
            with frame.to_image(threads=1) as image:
                if image.crop((60, 90, 200, 130)).convert("L").getextrema()[1] > 210:
                    caption_frames.append(index)
    assert caption_frames == list(range(24, 39))  # [1000, 1600) ms at 24 FPS.

    muted = deepcopy(with_tracks["snapshot"])
    picture(muted)["video"]["keep_audio"] = False
    latest = edited(client, with_tracks, muted)
    formal, _ = ready_manifest(client, latest, profile="FORMAL", **caption_options)
    assert formal["snapshot"]["output"]["fps"] == 25
    assert formal["snapshot"]["software"]["renderer_version"] == "arclight-pyav/4"
    assert formal["content_sha256"] == digest(formal["snapshot"])
    assert_blocked(
        client.post(render_path(formal, "jobs"), json={"creation_key": str(uuid4())}),
        "RENDER_EXPORT_REVIEW_REQUIRED",
    )
    approval = request_export(client, formal)
    answer = decide(client, approval)
    assert answer.status_code == 200, answer.text
    assert resume(client, approval, answer.json()).status_code == 200
    job, _ = submit(client, formal)
    assert run(client, job) == "SUCCEEDED", read(client, job)
    final = read(client, job)
    muted_movie = actual_movie(client, final)
    assert len(red_bounds(muted_movie)) == 63
    muted_left, _ = decoded_audio(muted_movie)
    assert 0.075 < rms(muted_left[30000:45000]) < 0.095
    assert 0.13 < rms(muted_left[52800:67200]) < 0.152
    assert 0.075 < rms(muted_left[81600:96000]) < 0.095
    report = report_read(client, case["episode"], final["artifact_id"])
    details = report["report"]["details"]
    assert details["manifest_sha256"] == formal["content_sha256"]
    assert {item["asset_type"] for item in details["sources"]} >= {"VIDEO", "AUDIO", "SUBTITLE"}
    assert details["checks"]["missing_required_shots"] == 0
    assert audio_files == {
        item["file"]["file_id"] for item in details["sources"] if item["asset_type"] == "AUDIO"
    }
    state = client.get(f"/api/v1/episodes/{case['episode_id']}/production-state").json()
    assert state["shots"][0]["phase"] == "APPROVED"
    assert state["shots"][0]["media_version_id"] == str(case["version_id"])


def test_video_snapshot_rejects_forged_or_superseded_review_and_stale_worker(
    client, dynamic_video, migrated
):
    case = dynamic_video
    record, version = approved_video(client, case)
    choice = choose(client, client.get(case["root"]).json(), version)
    assert choice.status_code == 200, choice.text
    timeline = created_timeline(client, case["episode"], create_body(case))
    with client.app.state.session_factory() as db:
        revision_before = db.scalar(text("SELECT version_num FROM alembic_version"))
    with pytest.raises(RuntimeError, match="Cannot downgrade with video timeline history"):
        command.downgrade(migrated, "0069")
    with client.app.state.session_factory() as db:
        assert db.scalar(text("SELECT version_num FROM alembic_version")) == revision_before
    forged = deepcopy(timeline["snapshot"])
    picture(forged)["video"]["review_sha256"] = "c" * 64
    save = client.patch(
        f"/api/v1/timelines/{timeline['id']}",
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": forged,
        },
    )
    assert_blocked(save, "TIMELINE_REFERENCE_CONFLICT")
    manifest, _ = ready_manifest(client, timeline)
    job, _ = submit(client, manifest)
    fresh_report = {**record["report"], "report_id": str(uuid4())}
    write_report(client, fresh_report, previous=record, admit=False)
    assert_blocked(
        client.post(manifest_url(timeline), json={"creation_key": str(uuid4())}),
        "VIDEO_REVIEW_REQUIRED",
    )
    assert_blocked(client.post(render_path(manifest, "export-review")), "VIDEO_REVIEW_REQUIRED")
    assert run(client, job) == "FAILED"
    assert read(client, job)["error_code"] == "VIDEO_REVIEW_REQUIRED"
    with client.app.state.session_factory.begin() as db:
        db.get(AssetVersion, case["version_id"]).stale = True
    assert_blocked(
        client.post(timeline_url(case["episode"]), json=create_body(case)), "ASSET_NOT_USABLE"
    )
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline


def test_real_variable_timestamps_trim_and_join_a_still_on_output_clock():
    stamps = (0, 2, 12, 14, 21, 23)
    data = synthetic_movie(width=256, height=256, audio=False, stamps=stamps)
    with av.open(BytesIO(data)) as container:
        rate = float(container.streams.video[0].average_rate)
    video = RenderVideo(
        data=base64.b64encode(data).decode(),
        source_start_ms=300,
        source_end_ms=800,
        frame_rate=rate,
        keep_audio=False,
        motion_required=True,
    )
    still = Image.new("RGB", (32, 32), (20, 40, 100))
    plan = RenderPlan(
        output=profile(fps=25),
        duration_ms=805,
        clips=(
            clip(still, duration_ms=305),
            RenderClip(
                video=video,
                start_ms=305,
                duration_ms=500,
                visual=ClipVisual(),
                fade_in_ms=0,
                fade_out_ms=0,
            ),
        ),
    )
    output = execute_media("render", "timeline", plan.model_dump_json().encode())
    bounds = red_bounds(output)
    assert bounds[:8] == [None] * 8
    assert bounds[8:13] == [8] * 5  # Source frame at 83 ms holds through the PTS gap.
    assert bounds[13:15] == [16] * 2  # Source frame at 500 ms, independent of average FPS.
    assert bounds[15:] == [24] * 6  # Next change at 583 ms, with the frame at 875 ms excluded.


def test_required_motion_rejects_repeated_frames_even_with_independent_sound():
    data = synthetic_movie(width=256, height=256, seconds=1, moving=False, audio=False)
    video = RenderVideo(
        data=base64.b64encode(data).decode(),
        source_start_ms=0,
        source_end_ms=500,
        frame_rate=24,
        keep_audio=False,
        motion_required=True,
    )
    plan = RenderPlan(
        output=profile(),
        duration_ms=500,
        clips=(
            RenderClip(
                video=video,
                start_ms=0,
                duration_ms=500,
                visual=ClipVisual(),
                fade_in_ms=0,
                fade_out_ms=0,
            ),
        ),
        audio_sources=(tone(),),
        audio_clips=(sound(duration_ms=500),),
    )
    with pytest.raises(MediaExecutionError, match="DYNAMIC_MEDIA_REQUIRED"):
        execute_media("render", "timeline", plan.model_dump_json().encode())
