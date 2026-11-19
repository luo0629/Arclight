"""VIDEO-04: correction must survive the exact workflow and the actual encoder."""

import json
import os
import shutil
import subprocess
from copy import deepcopy
from hashlib import sha256
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import assert_blocked, ready_manifest, request_export
from render_job_helpers import read, run, submit
from sqlalchemy import select
from test_asset_selection import choose
from test_asset_versions import state
from test_audio_production import output_audio, prepare, snapshot
from test_collaboration import register
from test_generation_video_inputs import MODEL
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_human_reviews import decide, resume
from test_lip_sync_media import MOUTH, cue_audio, pose_video, video_pixels
from test_render_mixing import decoded_audio, rms
from test_series_performance_admission import performance_case
from test_timelines import created_timeline, timeline_url
from test_video_candidates import finish_video, imported_rows
from test_video_review_admission import lifecycle
from test_video_temporal_workflow import candidate_endpoint, execute_run, start
from test_video_timeline_render import actual_movie

from arclight.application import generation_artifacts, video_lip_sync
from arclight.persistence.models import AssetDependency, AssetVersion, Project, ProjectMember


def ok(response, status):
    assert response.status_code == status, response.text
    return response.json()


def audio_version(client, source, index=0):
    line_url, asset_id, manifest_id = prepare(client, source, index)
    identity = output_audio(client, source, asset_id, manifest_id, data=cue_audio())
    ok(client.post(line_url + "/bindings", json={"asset_version_id": identity}), 201)
    root = f"/api/v1/projects/{source[0]['project_id']}/assets/{asset_id}"
    asset = ok(client.get(root), 200)
    version = ok(client.get(root + "/versions/" + identity), 200)
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        version = ok(state(client, asset, version, status), 200)
    ok(choose(client, ok(client.get(root), 200), version), 200)
    return version


def setup(client, settings, video_case, presentation):
    case = deepcopy(video_case)
    episode = {"id": case["episode_id"], "project_id": case["project_id"]}
    prefix = f"/api/v1/episodes/{episode['id']}"
    ok(client.patch(prefix, json={"target_duration_ms": 6000}), 200)
    with client.app.state.session_factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).output_profile = {
            "width": 256,
            "height": 144,
            "fps": 24,
        }
    script, board, _ = performance_case(
        client,
        episode,
        dynamic=True,
        duration_ms=6000,
        generation_routes=[{"provider": "seedance", "model": MODEL}],
        dialogue_presentation=presentation,
    )
    ok(
        client.post(prefix + f"/storyboards/{board['id']}/review", json={"decision": "APPROVE"}),
        200,
    )
    (shot,) = ok(client.get(prefix + "/shots", params={"storyboard_id": board["id"]}), 200)
    case["body"]["review_target"] = {"storyboard_id": board["id"], "shot_id": shot["id"]}
    case["body"]["inputs"].append(
        {
            "source_asset_version_id": board["id"],
            "dependency_type": "CONTENT_DEPENDENCY",
            "selectors": ["shot:" + shot["id"]],
        },
    )
    raw = pose_video(width=1280, height=720, rate=24, seconds=6)
    _, attempt, provider = finish_video(client, settings, case, raw)
    assert (
        generation_artifacts.import_attempt(
            client.app.state.session_factory, settings, attempt, provider
        )
        == 1
    )
    (candidate,) = imported_rows(client)
    endpoint = candidate_endpoint(case, candidate.id)
    row = ok(client.get(endpoint), 200)
    return {
        **case,
        "episode": episode,
        "script": script,
        "shot": shot,
        "board": board,
        "endpoint": endpoint,
        "raw": raw,
        "row": row,
        "candidate_id": candidate.id,
        "version_id": candidate.asset_version_id,
        "root": f"/api/v1/projects/{candidate.project_id}/assets/{row['version']['asset_id']}",
    }


def lip_request(row, line, audio, **values):
    return {
        "creation_key": str(uuid4()),
        "expected_revision": row["asset_revision"],
        "source_rights_note": "Original offline animation, authorized to adapt.",
        "cues": [
            {
                "line_id": line["line_id"],
                "audio_version_id": audio["id"],
                "start_ms": 500,
                "closed_frame_ms": 0,
                "open_frame_ms": 42,
                "mouth": MOUTH,
            }
        ],
        **values,
    }


def review_and_select(client, settings, case, row):
    endpoint = candidate_endpoint(case, row["id"])
    run_id = start(client, case, endpoint)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    review = ok(client.get(endpoint), 200)["admission"]
    assert review["conclusion"] == "HUMAN_REVIEW", review
    current = case | {"version_id": row["asset_version_id"]}
    assert_blocked(lifecycle(client, current, "READY_FOR_REVIEW"), "HUMAN_DECISION_REQUIRED")
    queue = (
        f"/api/v1/projects/{case['project_id']}/human-reviews?version_id={row['asset_version_id']}"
    )
    (request,) = ok(client.get(queue), 200)
    assert request["snapshot"]["object_id"] == review["report_id"]
    saved = ok(decide(client, request), 200)
    ok(resume(client, request, saved), 200)
    for status in ("READY_FOR_REVIEW", "APPROVED"):
        version = ok(lifecycle(client, current, status), 200)
    ok(choose(client, ok(client.get(case["root"]), 200), version), 200)
    assert ok(client.get(f"/api/v1/runs/{run_id}"), 200)["status"] == "SUCCEEDED"
    return review


def test_visible_dialogue_resynchronizes_locally_and_survives_formal_render(
    client, settings, video_case, monkeypatch, tmp_path
):
    case = setup(client, settings, video_case, "visible")
    original, endpoint = case["row"], case["endpoint"]
    correction = endpoint + "/lip-sync"
    assert original["actions"]["can_lip_sync"] and not original["actions"]["can_submit"]
    assert_blocked(lifecycle(client, case, "READY_FOR_REVIEW"), "LIP_SYNC_REQUIRED")
    captured = ok(snapshot(client, case["episode"], case["script"]), 201)
    lines = ok(client.get(f"/api/v1/audio-snapshots/{captured['id']}/lines"), 200)
    source = (case["episode"], case["script"], captured, lines)
    audio, narration = audio_version(client, source), audio_version(client, source, 1)
    options = ok(client.get(correction), 200)
    line, exempt = options["dialogue"]
    assert line["sources"][0]["audio_version_id"] == audio["id"]
    assert (
        exempt["presentation"] == "voice_over"
        and exempt["exemption_reason"]
        and not exempt["sources"]
    )
    assert options["requires_video_rights_confirmation"]
    payload = lip_request(original, line, audio)
    assert_blocked(
        client.post(correction, json=payload | {"source_rights_note": None}),
        "VIDEO_RIGHTS_UNCONFIRMED",
    )
    assert_blocked(
        client.post(correction, json=lip_request(original, line, narration)),
        "LIP_SYNC_AUDIO_INVALID",
    )
    assert_blocked(
        client.post(correction, json=payload | {"expected_revision": 0}), "VERSION_CONFLICT"
    )
    # Membership is checked again after a successful render, before any output is saved.
    owner = client.headers["Authorization"]
    editor = register(client, "lip-sync-editor@example.test")
    editor_token = client.headers["Authorization"]
    assert client.get(correction).status_code == 404
    member_id = UUID(editor["user"]["id"])
    project_id = UUID(case["project_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.add(ProjectMember(project_id=project_id, user_id=member_id, role="editor"))
    render = video_lip_sync.render_correction

    def downgrade_after_encode(data):
        result = render(data)
        with factory.begin() as db:
            member = db.scalar(
                select(ProjectMember).where(
                    ProjectMember.project_id == project_id,
                    ProjectMember.user_id == member_id,
                )
            )
            member.role = "viewer"
        return result

    with monkeypatch.context() as patch:
        patch.setattr(video_lip_sync, "render_correction", downgrade_after_encode)
        assert_blocked(
            client.post(correction, json=payload, headers={"Authorization": editor_token}),
            "FORBIDDEN",
        )
    client.headers["Authorization"] = owner
    assert len(imported_rows(client)) == 1
    assert ok(client.get(endpoint), 200)["asset_revision"] == original["asset_revision"]
    corrected = ok(client.post(correction, json=payload), 201)
    assert ok(client.post(correction, json=payload), 201)["id"] == corrected["id"]
    assert_blocked(
        client.post(correction, json=payload | {"source_rights_note": "Changed terms"}),
        "IDEMPOTENCY_CONFLICT",
    )
    assert corrected["version"]["status"] == "DRAFT"
    assert corrected["admission"]["blocked_code"] == "VIDEO_REVIEW_REQUIRED"
    assert ok(client.get(case["root"]), 200)["current_version_id"] is None
    assert client.get(endpoint + "/content").content == case["raw"]
    receipt = corrected["lip_sync"]
    assert receipt["source_video_version_id"] == original["asset_version_id"]
    assert receipt["source_video_sha256"] == sha256(case["raw"]).hexdigest()
    assert receipt["lines"][0]["audio_version_id"] == audio["id"]
    assert receipt["lines"][0]["audio_sha256"] == audio["checksum"]
    corrected_bytes = client.get(candidate_endpoint(case, corrected["id"]) + "/content").content
    assert (
        sha256(corrected_bytes).hexdigest()
        == receipt["output_sha256"]
        != receipt["source_video_sha256"]
    )

    # A local repair appends another exact candidate without changing either prior result.
    refreshed = ok(client.get(endpoint), 200)
    repair = lip_request(refreshed, line, audio)
    repair["cues"][0]["start_ms"] = 400
    repaired = ok(client.post(correction, json=repair), 201)
    assert repaired["lip_sync"]["lines"][0]["start_ms"] == 400
    assert repaired["lip_sync"]["output_sha256"] != receipt["output_sha256"]
    assert ok(client.get(candidate_endpoint(case, corrected["id"])), 200)["lip_sync"] == receipt

    review = review_and_select(client, settings, case, repaired)
    assert any("开口、停顿" in reason and "豁免" in reason for reason in review["limitations"])
    row = {"shot_id": case["shot"]["id"], "asset_version_id": repaired["asset_version_id"]}
    body = {
        "creation_key": str(uuid4()),
        "storyboard_id": case["board"]["id"],
        "default_duration_ms": 6000,
        "selected_shots": [row],
    }
    short_default = created_timeline(
        client, case["episode"], body | {"creation_key": str(uuid4()), "default_duration_ms": 900}
    )
    assert short_default["snapshot"]["duration_ms"] == 1200  # Keep the complete 400–1200 ms cue.
    timeline = created_timeline(client, case["episode"], body)
    picture, voice, *_ = timeline["snapshot"]["tracks"]
    assert picture["clips"][0]["video"]["keep_audio"] is False
    (cue,) = voice["clips"]
    assert (cue["source_asset_version_id"], cue["start_ms"], cue["duration_ms"]) == (
        audio["id"],
        400,
        800,
    )
    edited = deepcopy(timeline["snapshot"])
    edited["tracks"][1]["clips"][0]["start_ms"] = 401
    change = {
        "creation_key": str(uuid4()),
        "expected_version_no": timeline["version_no"],
        "snapshot": edited,
    }
    assert_blocked(
        client.patch(f"/api/v1/timelines/{timeline['id']}", json=change),
        "LIP_SYNC_TIMELINE_MISMATCH",
    )
    duplicate = row | {"video": {"source_start_ms": 0, "duration_ms": 6000, "keep_audio": True}}
    assert_blocked(
        client.post(
            timeline_url(case["episode"]),
            json=body
            | {
                "creation_key": str(uuid4()),
                "selected_shots": [duplicate],
            },
        ),
        "LIP_SYNC_TIMELINE_MISMATCH",
    )

    formal, _ = ready_manifest(client, timeline, profile="FORMAL")
    request = request_export(client, formal)
    saved = ok(decide(client, request), 200)
    ok(resume(client, request, saved), 200)
    job, _ = submit(client, formal)
    assert run(client, job) == "SUCCEEDED", read(client, job)
    result = read(client, job)
    movie = actual_movie(client, result)
    pixels = video_pixels(movie)
    assert len(pixels) == 144
    assert all(pixels[index].getpixel((128, 91))[0] < 100 for index in (14, 15, 24, 25))
    assert all(pixels[index].getpixel((128, 91))[0] > 175 for index in (18, 19, 29))
    left, _ = decoded_audio(movie)
    assert rms(left[26_400:32_400]) > 0.15 and rms(left[36_000:40_000]) < 0.01
    assert rms(left[45_600:51_600]) > 0.15
    ffprobe = shutil.which("ffprobe")
    if os.environ.get("CI"):
        assert ffprobe is not None, "Formal output requires independently verifying the MP4 in CI"
    if ffprobe is not None:
        path = tmp_path / "formal-lip-sync.mp4"
        path.write_bytes(movie)
        inspected = subprocess.run(
            [ffprobe, "-v", "error", "-show_streams", "-of", "json", str(path)],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        video, audio_stream = json.loads(inspected.stdout)["streams"]
        assert (video["codec_name"], int(video["nb_frames"]), float(video["duration"])) == (
            "h264",
            144,
            6.0,
        )
        assert (audio_stream["codec_name"], int(audio_stream["sample_rate"])) == ("aac", 48000)

    content = deepcopy(case["script"]["content_json"])
    content["scenes"][0]["dialogue"][0]["text"] = "这一句已经更换。"
    scripts = f"/api/v1/episodes/{case['episode_id']}/scripts"
    updated = ok(
        client.post(
            scripts,
            json={
                "expected_version_no": 1,
                "base_version_id": case["script"]["id"],
                "content": content,
            },
        ),
        201,
    )
    ok(client.post(scripts + f"/{updated['id']}/review", json={"decision": "APPROVE"}), 200)
    with client.app.state.session_factory() as db:
        target = UUID(repaired["asset_version_id"])
        assert (
            db.scalar(
                select(AssetDependency).where(
                    AssetDependency.source_asset_version_id == UUID(audio["id"]),
                    AssetDependency.target_asset_version_id == target,
                )
            )
            is not None
        )
        assert db.get(AssetVersion, UUID(audio["id"])).stale
        assert db.get(AssetVersion, UUID(corrected["asset_version_id"])).stale
        assert db.get(AssetVersion, target).stale
        assert db.get(AssetVersion, UUID(timeline["id"])).stale
        assert not db.get(AssetVersion, UUID(narration["id"])).stale
    assert client.get(endpoint + "/content").content == case["raw"]
    assert (
        client.get(candidate_endpoint(case, corrected["id"]) + "/content").content
        == corrected_bytes
    )
    assert (
        ok(client.get(candidate_endpoint(case, repaired["id"])), 200)["admission"]["status"]
        == "BLOCKED"
    )


@pytest.mark.parametrize("presentation", ["off_screen", "obscured"])
def test_exempt_dialogue_is_reviewed_without_generating_a_replacement(
    client, settings, video_case, presentation
):
    case = setup(client, settings, video_case, presentation)
    row, endpoint = case["row"], case["endpoint"]
    assert not row["lip_sync_required"] and not row["actions"]["can_lip_sync"]
    lines = ok(client.get(endpoint + "/lip-sync"), 200)["dialogue"]
    assert [(line["presentation"], line["lipsync"]) for line in lines] == [
        (presentation, "not_applicable"),
        ("voice_over", "not_applicable"),
    ]
    assert all(line["exemption_reason"] for line in lines)
    assert_blocked(
        client.post(endpoint + "/lip-sync", json=lip_request(row, lines[0], {"id": str(uuid4())})),
        "LIP_SYNC_CUES_INVALID",
    )
    review = review_and_select(client, settings, case, row)
    assert any("豁免" in limitation for limitation in review["limitations"])
    assert len(imported_rows(client)) == 1
