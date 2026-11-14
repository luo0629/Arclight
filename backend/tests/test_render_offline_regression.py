"""Task-level failure and repeat-render regressions with original offline media."""

import json
import logging
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from uuid import UUID, uuid4

import pytest
from render_admission_helpers import ready_manifest
from render_job_helpers import job_case, read, run, submit
from render_manifest_helpers import audio_case, render_case, review_case, stored_file, timeline_case
from render_offline_helpers import (
    DATASET,
    input_state,
    object_bytes,
    offline_case,
    rendered_offline,
    sample_movie,
)
from render_report_helpers import report_bytes, report_read
from sqlalchemy import func, select
from test_media_execution import recorded_children
from test_timeline_saves import save_url, saved_timeline

from arclight.application import render_execution
from arclight.media import execution
from arclight.persistence.models import (
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
)
from arclight.storage.cleanup import snapshot as object_snapshot
from arclight.storage.objects import ObjectStorage

__all__ = [
    "job_case",
    "audio_case",
    "render_case",
    "review_case",
    "timeline_case",
    "offline_case",
    "rendered_offline",
    "recorded_children",
]


def assert_unpublished(client, job, code):
    final = read(client, job)
    assert final["status"] == final["phase"] == "FAILED"
    assert final["error_code"] == code and final["progress"] < 100
    assert final["output_file_id"] is final["artifact_id"] is final["report_id"] is None
    assert final["is_delivery"] is False and final["cleanup_status"] == "CLEANED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderArtifact.id))) == 0
        attempt = db.scalar(select(RenderAttempt).where(RenderAttempt.run_id == UUID(job["id"])))
        assert attempt.status == "FAILED" and attempt.error_code == code
        assert object_snapshot(ObjectStorage(client.app.state.settings), attempt.output_key) is None
    return final


def test_same_offline_manifest_rerenders_without_mutating_inputs_or_old_delivery(
    client, episode, rendered_offline
):
    timeline, manifest, inputs, results = rendered_offline
    first, second = results
    assert first[0]["id"] != second[0]["id"]
    for field in ("artifact_id", "output_file_id", "output_asset_version_id", "report_id"):
        assert first[0][field] != second[0][field]
    assert input_state(client, manifest) == inputs
    assert client.get(f"/api/v1/render-manifests/{manifest['id']}").json() == manifest
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline
    sources = {
        source["asset_version_id"]: object_bytes(client, source["file"]["file_id"])
        for source in manifest["snapshot"]["sources"]
        if source["asset_type"] in {"IMAGE", "SHOT_IMAGE"}
    }
    for final, data, report, original_report in results:
        assert final["progress"] == 100 and final["manifest_sha256"] == manifest["content_sha256"]
        assert object_bytes(client, final["output_file_id"]) == data
        assert report_bytes(client, report) == original_report
        assert report_read(client, episode, final["artifact_id"])["report"] == report["report"]
        details = report["report"]["details"]
        assert details["sources"] == manifest["snapshot"]["sources"]
        assert details["checks"]["subtitle_range_valid"] is True
        assert all(check["passed"] for check in details["checks"]["technical"]["checks"])
        sample_movie(data, timeline["snapshot"], sources)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderArtifact.id))) == 2
        assert db.scalar(select(func.count(RenderInspection.id))) == 2


@pytest.mark.skipif(
    shutil.which("ffprobe") is None and not os.environ.get("CI"),
    reason="Independent FFprobe CLI unavailable locally; required in final CI",
)
def test_independent_ffprobe_checks_both_offline_outputs(rendered_offline):
    executable = shutil.which("ffprobe")
    if executable is None:
        if os.environ.get("CI"):
            pytest.fail("CI must provide FFprobe for the independent media regression")
        pytest.skip("Independent FFprobe CLI unavailable locally; required in final CI")
    timeline, manifest, _, results = rendered_offline
    evidence = Path("test-results/render-offline-v1")
    evidence.mkdir(parents=True, exist_ok=True)
    version = subprocess.run(
        [executable, "-version"], capture_output=True, text=True, check=True, timeout=15
    ).stdout.splitlines()[0]
    observations = []
    for index, (final, data, report, _) in enumerate(results, 1):
        path = evidence / f"render-{index}.mp4"
        path.write_bytes(data)
        command = [
            executable,
            "-v",
            "error",
            "-count_frames",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ]
        process = subprocess.run(command, capture_output=True, text=True, check=True, timeout=15)
        assert not process.stderr.strip()
        value = json.loads(process.stdout)
        streams = {stream["codec_type"]: stream for stream in value["streams"]}
        assert len(value["streams"]) == 2 and set(streams) == {"audio", "video"}
        video, audio = streams["video"], streams["audio"]
        assert (video["codec_name"], video["pix_fmt"], video["width"], video["height"]) == (
            "h264",
            "yuv420p",
            256,
            256,
        )
        assert video["avg_frame_rate"] == "24/1" and int(video["nb_read_frames"]) == 60
        assert float(video["duration"]) == pytest.approx(2.5, abs=1 / 24)
        assert (audio["codec_name"], audio["sample_rate"], audio["channels"]) == ("aac", "48000", 2)
        assert float(audio["duration"]) == pytest.approx(2.5, abs=1024 / 48000)
        assert int(value["format"]["size"]) == len(data)
        observations.append(
            {
                "job": final["id"],
                "probe": value,
                "command": command,
                "stderr": process.stderr,
                "exit_code": process.returncode,
                "report": report["report"],
            }
        )
    (evidence / "ffprobe.json").write_text(
        json.dumps(
            {
                "dataset": DATASET,
                "version": version,
                "timeline": timeline,
                "manifest": manifest,
                "observations": observations,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


@pytest.mark.parametrize("problem", ["negative", "overflow"])
def test_subtitle_range_failures_cannot_append_or_launch_a_timeline(client, offline_case, problem):
    timeline, _ = offline_case
    snapshot = deepcopy(timeline["snapshot"])
    track = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")
    clip = track["clips"][0]
    if problem == "negative":
        clip["start_ms"] = -1
    else:
        clip["start_ms"] = snapshot["duration_ms"] - clip["duration_ms"] + 1
    response = client.patch(
        save_url(timeline),
        json={
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )
    assert response.status_code == 422, response.text
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderExecution.id))) == 0


def test_overlapping_literal_subtitles_cannot_publish_a_render(client, episode, offline_case):
    timeline, font = offline_case
    _, version, checksum = stored_file(
        client, episode["project_id"], b"A", "text/plain", kind="SUBTITLE"
    )
    snapshot = deepcopy(timeline["snapshot"])
    track = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")
    clip = track["clips"][0]
    clip.update(source_asset_version_id=str(version), source_sha256=checksum)
    track["clips"].append({**clip, "id": str(uuid4()), "ordinal": 1, "start_ms": 1})
    updated = saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )
    manifest, _ = ready_manifest(client, updated, font_file_id=font)
    response = client.post(
        f"/api/v1/render-manifests/{manifest['id']}/jobs", json={"creation_key": str(uuid4())}
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "RENDER_SUBTITLE_RANGE_INVALID"
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count(RenderExecution.id))) == 0
        assert db.scalar(select(func.count(RenderArtifact.id))) == 0


@pytest.mark.parametrize(
    "data,code",
    [
        (b"\xff", "INVALID_FILE"),
        (b"1\n00:00:00,800 --> 00:00:00,100\nA\n", "RENDER_SUBTITLE_INVALID"),
        (b"1\n00:00:00,000 --> 00:00:03,000\nA\n", "SUBTITLE_CLIP_MISMATCH"),
    ],
)
def test_invalid_subtitle_objects_fail_the_real_job_without_false_completion(
    client, episode, offline_case, data, code
):
    timeline, font = offline_case
    _, version, checksum = stored_file(
        client, episode["project_id"], data, "text/plain", kind="SUBTITLE"
    )
    snapshot = deepcopy(timeline["snapshot"])
    clip = next(track for track in snapshot["tracks"] if track["kind"] == "SUBTITLE")["clips"][0]
    clip.update(source_asset_version_id=str(version), source_sha256=checksum)
    updated = saved_timeline(
        client,
        timeline,
        {
            "creation_key": str(uuid4()),
            "expected_version_no": timeline["version_no"],
            "snapshot": snapshot,
        },
    )
    manifest, _ = ready_manifest(client, updated, font_file_id=font)
    job, _ = submit(client, manifest)
    assert run(client, job) == "FAILED"
    assert_unpublished(client, job, code)
    assert client.get(f"/api/v1/timelines/{timeline['id']}").json() == timeline


@pytest.mark.parametrize(
    "fault,code",
    [
        ("exit", "RENDER_MEDIA_FAILED"),
        ("timeout", "RENDER_MEDIA_TIME_LIMIT"),
        ("diagnostic_limit", "RENDER_MEDIA_OUTPUT_LIMIT"),
        pytest.param(
            "cpu_limit",
            "RENDER_MEDIA_RESOURCE_LIMIT",
            marks=pytest.mark.skipif(sys.platform != "linux", reason="Linux kernel CPU limit"),
        ),
    ],
)
def test_actual_encoder_child_failure_is_reaped_unpublished_and_retryable(
    client, job_case, monkeypatch, recorded_children, caplog, fault, code
):
    manifest, job, _ = job_case
    capture, children, _, _ = recorded_children
    scripts = {
        "exit": "import sys; sys.stdin.buffer.read(); "
        "sys.stderr.write('PRIVATE_RENDER_TOKEN:/private/input'); sys.exit(17)",
        "timeout": "import sys,time; sys.stdin.buffer.read(); time.sleep(30)",
        "diagnostic_limit": "import sys,time; sys.stdin.buffer.read(); "
        "sys.stderr.write('PRIVATE_RENDER_TOKEN'*8192); "
        "sys.stderr.flush(); time.sleep(30)",
        "cpu_limit": "import sys; sys.stdin.buffer.read(); "
        "from arclight.media import limits; limits.CPU_SECONDS=1; "
        "limits.restrict_process(); "
        "from arclight.media.isolation import seal_process; seal_process()\n"
        "while True: pass",
    }
    real_execute = render_execution.execute_media
    caplog.set_level(logging.INFO, logger=execution.__name__)

    def faulted_encoder(*args, **kwargs):
        capture(scripts[fault])
        try:
            return real_execute(*args, **{**kwargs, "timeout": 0.1 if fault == "timeout" else 10})
        finally:
            capture()

    with monkeypatch.context() as patch:
        patch.setattr(render_execution, "execute_media", faulted_encoder)
        assert run(client, job) == "FAILED"
    failed = assert_unpublished(client, job, code)
    failed_children = list(children)
    assert failed_children and all(child.poll() is not None for child in failed_children)
    assert failed_children[-1].returncode != 0
    assert "PRIVATE_RENDER_TOKEN" not in caplog.text
    assert "/private/input" not in caplog.text
    assert "cleanup=complete" in caplog.text
    history_url = f"/api/v1/render-jobs/{job['id']}/attempts"
    old_history = client.get(history_url).json()
    retry, _ = submit(client, manifest, retry_of=job["id"])
    assert run(client, retry) == "SUCCEEDED", read(client, retry)
    assert read(client, job) == failed and client.get(history_url).json() == old_history
    assert read(client, retry)["output_file_id"]
