"""Exercise the owner-scoped first-frame handoff and real temporal Reviewer worker."""

import base64
import json
from decimal import Decimal
from hashlib import sha256
from uuid import UUID

import httpx
import pytest
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_agent_runtime import final
from test_asset_versions import image_bytes
from test_collaboration import create_episode, create_project
from test_contexts import approved_board
from test_generation_delivery import deliver
from test_generation_jobs import base, capture
from test_generation_reconciliation import query
from test_generation_video_inputs import configure_video
from test_model_services import account
from test_native_media_integration import attempt_for, configure, provider_for
from test_series_performance_admission import performance_case
from test_short_drama_delivery import authorized_run
from test_text_contract_v2 import save_script
from test_video_temporal_workflow import CATEGORIES, split_clip

from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.application.generation_artifacts import import_attempt as import_video
from arclight.application.generation_candidates import import_attempt as import_image
from arclight.application.generation_reconciliation import mark_result_unknown
from arclight.application.media_services import account_selection, selection_for_job
from arclight.application.runtime import execute_run
from arclight.application.short_drama_production import set_progress
from arclight.application.short_drama_visuals import (
    advance_short_drama_visuals,
    preflight_visuals,
    visual_reconciliation,
)
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.versions import SHORT_DRAMA_VISUAL_SERVICE
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    GenerationCandidate,
    GenerationJob,
    StoredFile,
    VersionBinding,
    WorkflowRun,
)


def visual_service_bindings(db, run_id):
    return {
        row.snapshot_json["task"]: row
        for row in db.scalars(
            select(VersionBinding).where(
                VersionBinding.resource_type == SHORT_DRAMA_VISUAL_SERVICE,
                VersionBinding.snapshot_json["run_id"].as_string() == str(run_id),
            )
        )
    }


@pytest.mark.parametrize(
    ("service", "code", "kind"),
    [
        ("none", "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED", "IMAGE"),
        ("image", "SHORT_DRAMA_VISUAL_SERVICE_REQUIRED", "VIDEO"),
        ("text_to_video", "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED", "VIDEO"),
        ("single_image_review", "SHORT_DRAMA_REVIEW_CAPABILITY_REQUIRED", "REVIEW"),
    ],
)
def test_visual_preflight_reports_the_required_route_without_media_jobs(
    client, settings, model_service, service, code, kind
):
    actor = account(client)
    if service != "none":
        configure(client, settings)
    if service == "text_to_video":
        configure_video(client, settings, model="doubao-seedance-1-0-pro-unlisted")
    elif service == "single_image_review":
        configure_video(client, settings)
        settings.llm_agent_capabilities = settings.llm_agent_capabilities.model_copy(
            update={"max_input_images": 1}
        )
    episode = create_episode(client, create_project(client))
    board, _ = approved_board(client, episode, save_script(client, episode))
    board_id = UUID(board["id"])
    run_id, _ = authorized_run(client, episode, board_id, actor=actor, generated_rights=True)
    with client.app.state.session_factory() as db:
        with pytest.raises(DomainError) as raised:
            preflight_visuals(db, settings, db.get(WorkflowRun, run_id), board_id)
        assert raised.value.code == code
        assert raised.value.service_kind == kind
        assert db.scalar(select(GenerationJob.id)) is None


@pytest.mark.parametrize("dynamic", [False, True])
def test_performance_board_respects_its_motion_requirement(
    client, settings, model_service, dynamic
):
    actor = account(client)
    configure(client, settings)
    episode = create_episode(client, create_project(client))
    _, board, _ = performance_case(client, episode, dynamic=dynamic)
    decision = {"decision": "APPROVE"}
    if dynamic:
        decision |= {
            "performance_intent_only": True,
            "comment": "先确认表演意图，稍后处理动态制作。",
        }
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/storyboards/{board['id']}/review", json=decision
    )
    assert response.status_code == 200, response.text
    board_id = UUID(board["id"])
    run_id, _ = authorized_run(client, episode, board_id, actor=actor, generated_rights=True)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        if dynamic:
            with pytest.raises(DomainError) as raised:
                preflight_visuals(db, settings, run, board_id)
            assert raised.value.code == "SHORT_DRAMA_PERFORMANCE_REVIEW_REQUIRED"
            assert not visual_service_bindings(db, run_id)
        else:
            settings.provider = "mock"
            assert preflight_visuals(db, settings, run, board_id) is None
            bindings = visual_service_bindings(db, run_id)
            assert set(bindings) == {"IMAGE", "REVIEW"}
            assert bindings["REVIEW"].snapshot_json["service"] is None
            settings.llm_agent_capabilities = None
            assert preflight_visuals(db, settings, run, board_id) is None
        assert db.scalar(select(GenerationJob.id)) is None


@pytest.mark.parametrize("switch_services", [False, True])
def test_draft_first_frame_remains_scoped_and_video_uses_shared_temporal_review(
    client,
    settings,
    model_service,
    switch_services,
):
    actor = account(client)
    image_service = configure(client, settings)
    image_policies = settings.service_model_policies
    video_service = configure_video(client, settings)
    settings.service_model_policies += image_policies
    settings.model_gateway_policy = settings.model_gateway_policy.model_copy(
        update={"text_maximum_cost": Decimal("1"), "pricing_version": "temporal-fixture/1"}
    )
    project = create_project(client)
    episode = create_episode(client, project)
    script = save_script(client, episode)
    board, shots = approved_board(client, episode, script)
    board_id = UUID(board["id"])
    run_id, _ = authorized_run(
        client,
        episode,
        board_id,
        actor=actor,
        generated_rights=True,
        output_profile={"width": 1280, "height": 720, "fps": 24},
        agent_limits=AgentRunLimits(maximum_cost=Decimal(20)),
    )
    factory = client.app.state.session_factory
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        original_inputs = dict(run.input_json)
        preflight_visuals(db, settings, run, board_id)
        bindings = visual_service_bindings(db, run_id)
        assert set(bindings) == {"IMAGE", "VIDEO", "REVIEW"}
        routes = {task: row.snapshot_json["service"] for task, row in bindings.items()}
        assert run.input_json == original_inputs
        private_resource_id = bindings["IMAGE"].resource_id
        set_progress(db, run, "SHOTS", storyboard_id=str(board_id))

    history = client.get(
        f"/api/v1/projects/{project['id']}/production-versions/bindings",
        params={
            "resource_type": SHORT_DRAMA_VISUAL_SERVICE,
            "resource_id": str(private_resource_id),
        },
    )
    assert history.status_code == 200, history.text
    assert history.json() == []

    if switch_services:
        configure(client, settings, revision=image_service["revision"], key="second-image-key")
        image_policies = settings.service_model_policies
        configure_video(
            client, settings, revision=video_service["revision"], key="second-video-key"
        )
        settings.service_model_policies += image_policies
        replacement = client.put(
            "/api/v1/me/model-services/text",
            json={
                "supplier": "custom",
                "protocol": "openai_chat",
                "model": "replacement-review-model",
                "api_key": "second-review-key",
                "base_url": "https://example.com/v1",
                "expected_revision": 0,
            },
        )
        assert replacement.status_code == 200, replacement.text

    def advance():
        with factory.begin() as db:
            return advance_short_drama_visuals(
                db,
                settings,
                db.get(WorkflowRun, run_id),
                board_id,
            )

    assert advance() is None
    assert advance() is None
    target = {"storyboard_id": board["id"], "shot_id": shots[0]["id"]}
    with factory() as db:
        rows = list(db.scalars(select(GenerationJob).where(GenerationJob.task_type == "IMAGE")))
        assert len(rows) == len(shots)
        image = next(
            row
            for row in rows
            if db.get(DependencyManifest, row.input_manifest_id).snapshot_json["context"][
                "continuity_review"
            ]
            == target
        )
        assert selection_for_job(db, image).private_snapshot() == routes["IMAGE"]
        image_job = {"id": str(image.id)}
    picture = image_bytes("PNG", (1280, 720))
    image_provider = provider_for(
        client,
        settings,
        image_job,
        lambda _: httpx.Response(
            200,
            json={"data": [{"b64_json": base64.b64encode(picture).decode()}]},
        ),
    )
    assert deliver(client, image_job, image_provider) == "SUCCEEDED"
    assert import_image(factory, settings, attempt_for(client, image_job), image_provider) == 1
    assert queue_pending_reviews(factory, settings) == 0
    assert advance() is None
    with factory() as db:
        frame_id = db.scalar(select(GenerationCandidate.asset_version_id))
        frame = db.get(AssetVersion, frame_id)
        file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == frame_id))
        assert frame.status == "DRAFT" and not file.rights_confirmed
        video = db.scalar(select(GenerationJob).where(GenerationJob.task_type == "VIDEO"))
        assert selection_for_job(db, video).private_snapshot() == routes["VIDEO"]
        video_job = {"id": str(video.id)}
        manifest = db.get(DependencyManifest, video.input_manifest_id).snapshot_json
        assert manifest["context"]["video_images"][0]["asset_version_id"] == str(frame_id)
        assert manifest["context"]["continuity_review"] == target
        assert manifest["context"]["generation"]["output_spec"]["duration_seconds"] == 6

    def projected_shot():
        canvas = client.get(f"/api/v1/episodes/{episode['id']}/canvas")
        assert canvas.status_code == 200, canvas.text
        return next(
            shot
            for node in canvas.json()["nodes"]
            if node["kind"] == "scene"
            for shot in node["shots"]
            if shot["id"] == f"shot:{target['shot_id']}"
        )

    projected = projected_shot()
    assert projected["cover_node_id"] == f"asset-version:{frame_id}"
    assert projected.get("media_node_id") is None and projected["status"] != "READY"

    payload = {
        "episode_id": episode["id"],
        "target_type": "VIDEO",
        "task_type": "VIDEO",
        "review_target": target,
        "spec": manifest["context"]["generation"],
        "inputs": [
            {key: row[key] for key in ("source_asset_version_id", "dependency_type", "selectors")}
            for row in manifest["inputs"]
        ],
    }
    if switch_services:
        with factory() as db:
            current = account_selection(db, actor, "VIDEO")
            assert current.private_snapshot() != routes["VIDEO"]
        payload["spec"] = {
            **payload["spec"],
            "model_config_version": current.configuration["configuration_version"],
        }
    # The draft frame grant cannot be passed to an ordinary job, even against the same shot.
    response = capture(client, project["id"], payload, "not-the-automatic-job")
    assert response.status_code == 409

    video_data = split_clip()
    request_id = f"short-drama-review-{video_job['id']}"

    def generate(request):
        if request.method == "POST":
            assert base64.b64encode(picture) in request.content
            return httpx.Response(200, json={"id": request_id})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=video_data)
        return httpx.Response(
            200,
            json={"status": "succeeded", "content": {"video_url": "https://cdn.example/video"}},
        )

    video_provider = provider_for(client, settings, video_job, generate)
    assert deliver(client, video_job, video_provider) == "SUBMITTED"
    attempt_id = attempt_for(client, video_job)
    with factory.begin() as db:
        mark_result_unknown(db, UUID(video_job["id"]), attempt_id, request_id=request_id)
    assert advance() is None
    with factory() as db:
        issue = visual_reconciliation(db, db.get(WorkflowRun, run_id), board_id)
        assert issue is not None and issue.code == "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN"
        assert (
            issue.service_kind == "VIDEO"
            and issue.model == manifest["context"]["generation"]["model"]
        )
    latest = client.get(base(project["id"]) + "/" + video_job["id"]).json()
    response = query(client, latest, video_provider)
    assert response.status_code == 200 and response.json()["status"] == "SUCCEEDED", response.text
    with factory() as db:
        assert visual_reconciliation(db, db.get(WorkflowRun, run_id), board_id) is None
    assert import_video(factory, settings, attempt_id, video_provider) == 1
    assert queue_pending_reviews(factory, settings) == 0
    with factory() as db:
        candidate_id = db.scalar(
            select(GenerationCandidate.id).where(
                GenerationCandidate.job_id == UUID(video_job["id"])
            )
        )
    manual_review = client.post(
        f"/api/v1/projects/{project['id']}/generation-candidates/{candidate_id}/review",
        json=target,
    )
    assert manual_review.status_code == 409
    assert manual_review.json()["error"]["code"] == "SHORT_DRAMA_REVIEW_SCOPE"
    assert advance() is None

    with factory() as db:
        child = db.scalar(select(AgentRunLimit).where(AgentRunLimit.parent_run_id == run_id))
        review = db.get(WorkflowRun, child.run_id)
        assert review is not None
        assert review.input_json["_text_service"] == routes["REVIEW"]
        review_id, revision = review.id, review.revision

    def assess(request):
        payload = json.loads(request.content)
        assert payload["model"] == routes["REVIEW"]["model"]
        observed = next(row["content"] for row in payload["messages"] if row["role"] == "user")
        evidence = json.loads(observed[0]["text"])["data"]
        assert evidence["target"]["shot_id"] == target["shot_id"]
        assert evidence["reference_sha256"] == [sha256(picture).hexdigest()]
        samples = evidence["samples"]
        return httpx.Response(
            200,
            json=final(
                result={
                    "summary": "Review completed.",
                    "observations": [
                        {"time_ms": item["time_ms"], "observation": "Reviewed frame."}
                        for item in samples
                    ],
                    **{
                        field: {
                            "time_ms": samples[0]["time_ms"],
                            "description": "Evidence matches.",
                            "status": "CLEAR",
                        }
                        for field in CATEGORIES
                    },
                }
            ),
        )

    model_service(assess)
    execute_run(factory, settings, review_id, revision)
    with factory() as db:
        review = db.get(WorkflowRun, review_id)
        assert review.status == "WAITING_REVIEW", review.error_code
    assert advance() is None
    with factory() as db:
        review = db.get(WorkflowRun, review_id)
        assert review.status == "SUCCEEDED", review.pending_review or review.error_code
        version = db.scalar(
            select(AssetVersion)
            .join(GenerationCandidate, GenerationCandidate.asset_version_id == AssetVersion.id)
            .where(GenerationCandidate.job_id == UUID(video_job["id"]))
        )
        assert version.status == "APPROVED" and not version.stale
        assert db.get(AssetVersion, frame_id).status == "DRAFT"
        version_id = version.id
    projected = projected_shot()
    assert projected["status"] == "READY"
    assert projected["media_node_id"] == f"asset-version:{version_id}"
    assert projected["cover_node_id"] == f"asset-version:{frame_id}"
