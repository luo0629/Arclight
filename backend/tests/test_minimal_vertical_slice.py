"""TEST-16: real production slice; only model/image transport is deterministic.

The original schematic reference library includes a historical COSTUME asset
without a directory creation API. This is an input fixture,
never candidate admissions. Every new shot image goes through production checks,
Reviewer, human approval and selection before the real renderer consumes it.
"""

import hashlib
import json
from copy import deepcopy
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import av
from continuity_reviewer_support import install_reviewer_model
from fastapi.testclient import TestClient
from render_job_helpers import read as read_render
from render_report_helpers import accept_warnings, completed_report, report_bytes, report_read
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_asset_versions import state
from test_continuity_escalations import pending
from test_continuity_feedback import install_feedback_model
from test_continuity_rules import ANNOTATIONS, imported_reference, reference_image
from test_continuity_selection import approve, target_rows
from test_continuity_workflow import BatchImages, make_job, work_for
from test_continuity_workflow_recovery import run_work
from test_director_creation import install as install_director
from test_generation_budget import priced, set_policy
from test_generation_candidates import candidates, ingest
from test_generation_delivery import deliver
from test_human_reviews import decide, resume
from test_screenwriter_creation import approve as approve_text
from test_screenwriter_creation import install as install_writer
from test_screenwriter_creation import run_once, start
from test_timelines import created_timeline

from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.application.render_inputs import file_storage
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import GenerationAttempt, StoredFile, WorkflowRun
from arclight.persistence.render_manifests import RenderArtifact
from arclight.providers.media_submission import MockMediaSubmissionProvider


def reference_library(client, settings, episode):
    project_id = episode["project_id"]
    refs = {
        kind: reference_image(client, project_id, kind, kind, filename)
        for kind, filename in (
            ("CHARACTER", "character.png"),
            ("SCENE", "scene-reference.png"),
            ("PROP", "prop-reference.png"),
        )
    }
    refs["COSTUME"] = imported_reference(
        client, settings, project_id, "COSTUME", "costume-reference.png"
    )
    return refs


def approved_five_shots(client, settings, episode, model_service, monkeypatch, refs):
    def writer(phase, result, seed):
        if phase == "scene":
            result["scene"].update(action=ANNOTATIONS["baseline"], dialogue=[])
            result["character_assets"] = [{"character": "主角", "asset": refs["CHARACTER"]}]

    def director(phase, result, seed):
        if phase == "scene":
            assert len(result["shots"]) == 5
            for shot in result["shots"]:
                shot.update(
                    characters=["主角"],
                    character_details=[
                        {
                            "character": "主角",
                            "asset": refs["CHARACTER"],
                            "costume": refs["COSTUME"],
                            "action": "hold the closed red case",
                        }
                    ],
                    scene_asset=refs["SCENE"],
                    props=[refs["PROP"]],
                    visual_description=ANNOTATIONS["baseline"],
                    composition="Full shot; protagonist left, looking right; show the red case.",
                )

    _, writer_errors = install_writer(model_service, monkeypatch, settings, count=1, mutate=writer)
    identity = start(
        client,
        episode,
        professional_director=True,
        story="蓝色房间里，穿金色外套的黑发主角守护关闭的红箱。五个静态镜头，无对白。",
    )
    first = run_once(client, settings, identity)
    assert not writer_errors, writer_errors
    assert (
        first["status"] == "WAITING_REVIEW" and first["pending_review"]["resource_type"] == "script"
    )
    script_id = first["pending_review"]["version_id"]
    approve_text(client, first)
    _, director_errors = install_director(model_service, monkeypatch, settings, mutate=director)
    second = run_once(client, settings, identity)
    assert not director_errors, director_errors
    assert (
        second["status"] == "WAITING_REVIEW"
        and second["pending_review"]["resource_type"] == "storyboard"
    )
    board_id = second["pending_review"]["version_id"]
    approve_text(client, second)
    assert run_once(client, settings, identity)["status"] == "SUCCEEDED"
    response = client.get(
        f"/api/v1/episodes/{episode['id']}/shots", params={"storyboard_id": board_id}
    )
    assert response.status_code == 200, response.text
    shots = response.json()
    assert len(shots) == 5 and all(shot["status"] == "APPROVED" for shot in shots)
    return str(identity), script_id, board_id, shots


def test_short_story_review_revision_human_selection_default_timeline_and_real_mp4(
    client,
    settings,
    model_service,
    monkeypatch,
):
    project = client.post(
        "/api/v1/projects",
        json={
            "name": "TEST-16 original offline slice",
            "output_profile": {"width": 256, "height": 384, "fps": 24},
        },
    )
    assert project.status_code == 201, project.text
    project_id = project.json()["id"]
    created = client.post(
        f"/api/v1/projects/{project_id}/episodes",
        json={
            "episode_no": 1,
            "title": "守护红箱",
            "target_duration_ms": 15000,
        },
    )
    assert created.status_code == 201, created.text
    episode = created.json()
    priced(monkeypatch, "1", "test16-offline-quote:1")
    set_policy(client, project_id, "10", task_limit="1")
    settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(
        update={"max_input_images": 8}
    )
    refs = reference_library(client, settings, episode)
    text_run, script_id, board_id, shots = approved_five_shots(
        client, settings, episode, model_service, monkeypatch, refs
    )
    factory = client.app.state.session_factory
    selected, records, jobs, pauses = [], [], [], []
    revision_evidence = {}
    for index, shot in enumerate(shots):
        case = {"target": {"storyboard_id": board_id, "shot_id": shot["id"]}}
        job, attempt = make_job(client, episode, case, import_images=False)
        jobs.append(job["id"])
        assert ingest(client, attempt, BatchImages("state.png" if index == 4 else "match.png")) == 1
        candidate = candidates(client, project_id, job)[0]
        assert candidate["media_check"]["status"] == "PASSED"
        assert queue_pending_reviews(factory, settings) == 1
        work = work_for(client, project_id, candidate)
        case = {"project_id": project_id, "target": work["work"]["target"]}
        asset, version = target_rows(client, case)
        assert state(client, asset, version, "READY_FOR_REVIEW").status_code == 409
        assert choose(client, asset, version).status_code == 409
        options = {"difference": ("STATE", "MEDIUM")} if index == 4 else {}
        if index == 1:
            options = {
                "uncertainty": {
                    "reason": "LOW_VISIBILITY",
                    "detail": "Offline low-confidence costume comparison.",
                }
            }
        _, errors, deliveries = install_reviewer_model(model_service, dynamic=False, **options)
        run_id = run_work(client, settings, work)
        assert not errors, errors
        with factory() as db:
            diagnostic = db.get(WorkflowRun, run_id)
            assert len(deliveries) == 1, (
                diagnostic.status,
                diagnostic.error_code,
                diagnostic.pending_review,
            )
        progress = work_for(client, project_id, candidate)
        records.append(deepcopy(deliveries[0]))
        if index == 4:
            original_checksum = version["checksum"]
            assert progress["pending_review"]["destination"] == "REVISION_REQUIRED", progress
            with factory() as db:
                control = db.get(AgentRunLimit, run_id)
                limits = (deepcopy(control.limits_json), control.deadline_at, control.root_run_id)
            command = {
                "review_id": progress["pending_review"]["review_id"],
                "issue_ids": [deliveries[0]["issues"][0]["issue_id"]],
                "action": "REGENERATE",
                "amendments": [],
            }
            endpoint = f"/api/v1/projects/{project_id}/continuity-revisions"
            headers = {"Idempotency-Key": "test16-one-local-revision"}
            set_policy(client, project_id, "10", task_limit="0.50")
            blocked = client.post(endpoint, json=command, headers=headers)
            assert (
                blocked.status_code == 409
                and blocked.json()["error"]["code"] == "BUDGET_TASK_EXCEEDED"
            ), blocked.text
            set_policy(client, project_id, "10", task_limit="1")
            revision = client.post(endpoint, json=command, headers=headers)
            assert revision.status_code == 201, revision.text
            revision = revision.json()
            assert revision["round"] == 1
            replay = client.post(endpoint, json=command, headers=headers)
            assert replay.status_code == 200 and replay.json() == revision
            revised_job = client.get(
                f"/api/v1/projects/{project_id}/generation-jobs/{revision['job_id']}"
            ).json()
            jobs.append(revised_job["id"])
            assert deliver(client, revised_job, MockMediaSubmissionProvider()) == "SUCCEEDED"
            with factory() as db:
                attempt = db.scalar(
                    select(GenerationAttempt.id).where(
                        GenerationAttempt.job_id == UUID(revised_job["id"])
                    )
                )
            assert ingest(client, attempt, BatchImages("match.png")) == 1
            candidate = candidates(client, project_id, revised_job)[0]
            assert candidate["media_check"]["status"] == "PASSED"
            assert queue_pending_reviews(factory, settings) == 1
            work = work_for(client, project_id, candidate)
            assert work["work"]["run_id"] == str(run_id)
            _, _, _, errors, deliveries = install_feedback_model(
                model_service, ["READY_FOR_REVIEW"]
            )
            run_work(client, settings, work)
            assert not errors, errors
            assert deliveries[0]["conclusion"] == "PASS"
            records.extend(deepcopy(deliveries))
            progress = work_for(client, project_id, candidate)
            with factory() as db:
                control = db.get(AgentRunLimit, run_id)
                assert (control.limits_json, control.deadline_at, control.root_run_id) == limits
            revision_evidence = {
                "budget_denial": blocked.json(),
                "revision": revision,
                "replay": replay.json(),
                "limits": limits[0],
                "deadline_at": str(limits[1]),
                "root_run_id": str(limits[2]),
                "original_checksum": original_checksum,
            }
            case["target"] = work["work"]["target"]
        if index < 4:
            assert progress["pending_review"]["destination"] == "HUMAN_REVIEW", progress
            assert records[-1]["confidence"]["level"] == "LOW"
            case["run_id"] = run_id
            row = pending(client, case)
            asset, version = target_rows(client, case)
            assert state(client, asset, version, "READY_FOR_REVIEW").status_code == 409
            # A fresh in-process API instance reads the committed pause; no server
            # or local database service is started for this restart boundary.
            with TestClient(create_app(settings)) as restarted:
                restarted.headers["Authorization"] = "Bearer test-token"
                recovered = restarted.get(f"/api/v1/human-reviews/{row['id']}")
                assert recovered.status_code == 200 and recovered.json() == row
                decision = decide(restarted, row)
                assert decision.status_code == 200, decision.text
                resumed = resume(restarted, row, decision.json())
                assert resumed.status_code == 200, resumed.text
                assert resume(restarted, row, decision.json()).json() == resumed.json()
                pauses.append(
                    {"request": row, "decision": decision.json(), "resume": resumed.json()}
                )
        else:
            assert progress["pending_review"]["destination"] == "READY_FOR_REVIEW", progress
        asset, version = target_rows(client, case)
        if index == 4:
            assert version["checksum"] != original_checksum
            revision_evidence["revised_checksum"] = version["checksum"]
        assert version["status"] == "DRAFT" and asset["current_version_id"] is None
        assert choose(client, asset, version).status_code == 409
        version = approve(client, case)
        assert work_for(client, project_id, candidate)["status"] == "WAITING_REVIEW"
        assert choose(client, current(client, asset), version).status_code == 200
        assert work_for(client, project_id, candidate)["status"] == "SUCCEEDED"
        selected.append({"shot_id": shot["id"], "asset_version_id": version["id"]})
    assert queue_pending_reviews(factory, settings) == 0
    unchanged = client.get(
        f"/api/v1/episodes/{episode['id']}/shots", params={"storyboard_id": board_id}
    )
    assert unchanged.status_code == 200 and unchanged.json() == shots
    assert selected[3]["asset_version_id"] in {
        reference["resource_id"] for reference in records[-1]["references"]
    }
    timeline = created_timeline(
        client,
        episode,
        {"creation_key": str(uuid4()), "storyboard_id": board_id, "selected_shots": selected},
    )
    clips = timeline["snapshot"]["tracks"][0]["clips"]
    assert len(clips) == 5 and [clip["duration_ms"] for clip in clips] == [3000] * 5
    assert timeline["snapshot"]["duration_ms"] == 15000
    manifest, rendered, report = completed_report(client, episode, timeline)
    assert rendered["status"] == "SUCCEEDED"
    assert report["acceptance"]["phase"] == "DELIVERY_QA"
    warnings = report["report"]["details"]["warnings"]
    assert len(warnings) == 4 and {warning["code"] for warning in warnings} == {"MEDIA_EXCEPTION"}
    accept_warnings(client, episode, rendered["artifact_id"])
    report = report_read(client, episode, rendered["artifact_id"])
    rendered = read_render(client, rendered)
    assert report["acceptance"]["phase"] == "COMPLETED_WITH_WARNINGS"
    assert rendered["is_delivery"] is True
    result_dir = Path("test-results/minimal-vertical-slice")
    result_dir.mkdir(parents=True, exist_ok=True)
    with factory() as db:
        artifact = db.get(RenderArtifact, UUID(rendered["artifact_id"]))
        output = db.get(StoredFile, artifact.file_id)
        movie = file_storage(settings, output).read(
            output.object_key, output.byte_size, output.checksum
        )
        assert hashlib.sha256(movie).hexdigest() == artifact.checksum
    (result_dir / "slice.mp4").write_bytes(movie)
    with av.open(BytesIO(movie)) as decoded:
        video = decoded.streams.video[0]
        assert (video.width, video.height, video.average_rate, video.codec_context.name) == (
            256,
            384,
            24,
            "h264",
        )
        assert len(decoded.streams.audio) == 1
        audio = decoded.streams.audio[0]
        assert (audio.codec_context.name, audio.sample_rate, audio.channels) == ("aac", 48000, 2)
        frames = list(decoded.decode(video))
        assert len(frames) == 360 and int(decoded.duration / 1000) == 15000
        for number, frame_index in enumerate((36, 108, 180, 252, 324), 1):
            frames[frame_index].to_image().save(result_dir / f"shot-{number}.png")
    histories = {}
    for name, endpoint in {
        "runs": f"/api/v1/episodes/{episode['id']}/runs?limit=100",
        "generation": f"/api/v1/projects/{project_id}/generation-jobs?limit=100",
        "render": f"/api/v1/render-manifests/{manifest['id']}/jobs",
    }.items():
        response = client.get(endpoint)
        assert response.status_code == 200, response.text
        histories[name] = response.json()
    assert {row["id"] for row in histories["generation"]} == set(jobs)
    assert len(jobs) == 6
    assert {row["id"] for row in histories["runs"]} == {
        text_run,
        *[
            row["work"]["run_id"]
            for row in [
                work_for(client, project_id, {"id": record["target"]["candidate_id"]})
                for record in records
            ]
        ],
    }
    assert [row["id"] for row in histories["render"]] == [rendered["id"]]
    assert len(histories["runs"]) == 6
    assert all(row["status"] == "SUCCEEDED" for row in histories["runs"])
    for row in histories["runs"]:
        events = client.get(f"/api/v1/runs/{row['id']}/events")
        assert events.status_code == 200 and events.json()
    (result_dir / "machine-report.json").write_bytes(report_bytes(client, report))
    evidence = {
        "scope": (
            "Schematic images; deterministic model transport and simulated Owner decisions; "
            "real checks, persistence and rendering. COSTUME is an imported reference fixture. "
            "Four explicit media exceptions; no real provider or artistic-quality claim."
        ),
        "text_run": text_run,
        "script_id": script_id,
        "storyboard_id": board_id,
        "references": refs,
        "generation_job_ids": jobs,
        "reviews": records,
        "local_revision": revision_evidence,
        "human_pauses": pauses,
        "approved_shots": shots,
        "selected": selected,
        "timeline": timeline,
        "manifest": manifest,
        "render_job": rendered,
        "report": report,
        "task_histories": histories,
    }
    (result_dir / "slice.json").write_text(
        json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8"
    )
