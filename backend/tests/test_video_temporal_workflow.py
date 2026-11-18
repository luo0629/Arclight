"""Exercise video sampling, Reviewer delivery, shared QA and revision via real entry points."""

import base64
import io
import json
from decimal import Decimal
from uuid import UUID, uuid4

import av
import httpx
from PIL import Image
from test_agent_model import model_service as model_service  # noqa: F401
from test_agent_runtime import final
from test_generation_delivery import deliver
from test_generation_reconciliation import query
from test_generation_video_inputs import video_case as video_case  # noqa: F401
from test_native_media_integration import attempt_for, provider_for
from test_video_candidates import finish_video, imported_rows, video_bytes
from test_video_candidates import formal_video_case as formal_video_case  # noqa: F401
from test_video_review_admission import lifecycle
from test_video_review_admission import reviewed_video as reviewed_video  # noqa: F401

from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.application.generation_artifacts import import_attempt
from arclight.application.generation_reconciliation import mark_result_unknown
from arclight.application.runtime import execute_run
from arclight.persistence.models import AssetVersion, DependencyManifest, WorkflowRun

CATEGORIES = (
    "identity_and_costume",
    "flicker_and_distortion",
    "action_completion",
    "character_interaction",
    "spatial_and_prop_continuity",
    "unexplained_stillness",
)


def split_clip():
    buffer = io.BytesIO()
    with av.open(buffer, "w", format="mp4") as output:
        stream = output.add_stream("mpeg4", rate=24)
        stream.width, stream.height, stream.pix_fmt = 1280, 720, "yuv420p"
        first = av.VideoFrame.from_image(Image.new("RGB", (1280, 720), (250, 20, 24)))
        later = av.VideoFrame.from_image(Image.new("RGB", (1280, 720), (15, 46, 230)))
        for index in range(144):
            frame = first if index < 66 else later
            frame.pts = index
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return buffer.getvalue()


def candidate_endpoint(case, candidate_id):
    return f"/api/v1/projects/{case['project_id']}/generation-candidates/{candidate_id}"


def outcome(client, endpoint):
    response = client.get(endpoint)
    assert response.status_code == 200, response.text
    return response.json()["admission"]


def start(client, case, endpoint):
    response = client.post(endpoint + "/review", json=case["body"]["review_target"])
    assert response.status_code == 202, response.text
    return UUID(response.json()["run_id"])


def test_real_temporal_pixels_locate_midclip_defects_then_fresh_revision_runs_same_reviewer(
    client,
    settings,
    formal_video_case,
    model_service,
):
    case = formal_video_case
    settings.model_gateway_policy = settings.model_gateway_policy.model_copy(
        update={"text_maximum_cost": Decimal("1"), "pricing_version": "temporal-fixture/1"}
    )
    seen = []

    def review_response(request):
        payload = json.loads(request.content)
        content = next(row["content"] for row in payload["messages"] if row["role"] == "user")
        seed = json.loads(content[0]["text"])["data"]
        images = [value["image_url"]["url"] for value in content if value["type"] == "image_url"]
        assert len(images) == 2
        sheet = Image.open(io.BytesIO(base64.b64decode(images[0].split(",", 1)[1])))
        actual = [part["time_ms"] for part in seed["samples"]]
        assert len(actual) == 8 and actual[0] == 0 and actual[-1] == 5958
        assert seed["shot_context"]["selected_shot_id"] == case["body"]["review_target"]["shot_id"]
        assert seed["target"]["storyboard_id"] == case["body"]["review_target"]["storyboard_id"]
        drifting = not seen
        if drifting:
            assert sheet.getpixel((32, 32))[0] > 180
            assert sheet.getpixel((32, sheet.height - 32))[2] > 150
        seen.append(seed)
        result = {
            "summary": "The first frame was acceptable, but the second half has defects."
            if drifting
            else "The reviewed samples are consistent.",
            "observations": [
                {"time_ms": time, "observation": f"Visual comparison at {time} ms."}
                for time in actual
            ],
            **{
                field: {
                    "time_ms": actual[4],
                    "description": "Mid-clip discontinuity."
                    if drifting
                    else "Consistent against the approved shot.",
                    "status": "ISSUE" if drifting else "CLEAR",
                }
                for field in CATEGORIES
            },
        }
        return httpx.Response(200, json=final(result=result))

    model_service(review_response)
    _, attempt, provider = finish_video(client, settings, case, split_clip())
    factory = client.app.state.session_factory
    assert import_attempt(factory, settings, attempt, provider) == 1
    original = imported_rows(client)[0]
    endpoint = candidate_endpoint(case, original.id)
    assert outcome(client, endpoint)["blocked_code"] == "VIDEO_REVIEW_REQUIRED"
    assert queue_pending_reviews(factory, settings) == 1
    run_id = start(client, case, endpoint)
    assert start(client, case, endpoint) == run_id
    execute_run(factory, settings, run_id, 0)
    execute_run(factory, settings, run_id, 0)
    first = outcome(client, endpoint)
    assert first["conclusion"] == "REVISION_REQUIRED", first
    assert first["review_status"] == "WAITING_REVIEW"
    assert len(first["issues"]) == 6 and all(issue["time_ms"] >= 3000 for issue in first["issues"])
    assert first["sampling_strategy"] == "BOUNDED_UNIFORM" and first["limitations"]
    assert len({sample["frame_sha256"] for sample in first["samples"]}) > 1
    assert len(seen) == 1

    revisions = f"/api/v1/projects/{case['project_id']}/continuity-revisions"
    body = {
        "review_id": first["report_id"],
        "issue_ids": [issue["issue_id"] for issue in first["issues"]],
        "action": "REVISE_PROMPT",
        "amendments": [
            {
                "block": "shot_description",
                "instruction": "Keep the same wardrobe and complete the action.",
            }
        ],
    }
    key = {"Idempotency-Key": str(uuid4())}
    bad = client.post(revisions, headers=key, json=body | {"issue_ids": [str(uuid4())]})
    assert bad.status_code == 422, bad.text
    response = client.post(revisions, headers=key, json=body)
    assert response.status_code == 201, response.text
    receipt = response.json()
    assert receipt["root_candidate_id"] == str(original.id) and receipt["round"] == 1
    replay = client.post(revisions, headers=key, json=body)
    assert replay.status_code == 200 and replay.json() == receipt
    other = client.post(revisions, headers={"Idempotency-Key": str(uuid4())}, json=body)
    assert other.status_code == 409, other.text

    job_url = f"/api/v1/projects/{case['project_id']}/generation-jobs/{receipt['job_id']}"
    job = client.get(job_url).json()
    assert job["task_type"] == "VIDEO"
    with factory() as db:
        source = db.get(DependencyManifest, original.input_manifest_id).snapshot_json
        revised = db.get(DependencyManifest, UUID(job["input_manifest_id"])).snapshot_json
        assert revised["inputs"] == source["inputs"]
        assert revised["context"]["video_images"] == source["context"]["video_images"]
        assert revised["context"]["generation"]["batch_size"] == 1
    replacement = video_bytes()

    def generation_response(request):
        if request.method == "POST":
            return httpx.Response(200, json={"id": "video02-revised"})
        if request.url.host == "cdn.example":
            return httpx.Response(200, content=replacement)
        return httpx.Response(
            200, json={"status": "succeeded", "content": {"video_url": "https://cdn.example/video"}}
        )

    provider = provider_for(client, settings, job, generation_response)
    assert deliver(client, job, provider) == "SUBMITTED"
    attempt = attempt_for(client, job)
    with factory.begin() as db:
        mark_result_unknown(db, UUID(job["id"]), attempt, request_id="video02-revised")
    latest = client.get(job_url).json()
    resolution = query(client, latest, provider)
    assert resolution.status_code == 200, resolution.text
    assert import_attempt(factory, settings, attempt, provider) == 1
    updated = next(row for row in imported_rows(client) if row.id != original.id)
    child = candidate_endpoint(case, updated.id)
    assert outcome(client, child)["blocked_code"] == "VIDEO_REVIEW_REQUIRED"
    assert queue_pending_reviews(factory, settings) == 1
    with factory() as db:
        revision = db.get(WorkflowRun, run_id).revision
    assert start(client, case, child) == run_id
    execute_run(factory, settings, run_id, revision)
    after = outcome(client, child)
    assert after["status"] == "ADMITTED" and after["conclusion"] == "PASS", after
    assert after["report_id"] != first["report_id"] and len(seen) == 2
    assert outcome(client, endpoint)["blocked_code"] == "VIDEO_REVISION_REQUIRED"
    with factory() as db:
        assert db.get(AssetVersion, updated.asset_version_id).status == "DRAFT"


def test_no_vision_model_routes_to_shared_human_queue_with_exact_owner_decision(
    client, settings, reviewed_video
):
    case = reviewed_video
    endpoint = candidate_endpoint(case, case["candidate_id"])
    run_id = start(client, case, endpoint)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    review = outcome(client, endpoint)
    assert (
        review["conclusion"] == "HUMAN_REVIEW"
        and review["blocked_code"] == "HUMAN_DECISION_REQUIRED"
    ), review
    assert review["samples"][0]["time_ms"] == 0 and review["samples"][-1]["time_ms"] == 5958
    assert lifecycle(client, case, "READY_FOR_REVIEW").status_code == 409
    queue = f"/api/v1/projects/{case['project_id']}/human-reviews?version_id={case['version_id']}"
    (row,) = client.get(queue).json()
    assert (
        row["id"] == review["human_review_request_id"]
        and row["snapshot"]["object_id"] == review["report_id"]
    )
    assert row["snapshot"]["version_id"] == str(case["version_id"])
    human = f"/api/v1/human-reviews/{row['id']}"
    decision = {
        "version_id": str(case["version_id"]),
        "request_sha256": row["request_sha256"],
        "decision": "APPROVE",
        "comment": "Viewed the whole clip and accepted the sampled coverage limitation.",
    }
    wrong = client.post(human + "/decisions", json=decision | {"request_sha256": "0" * 64})
    assert wrong.status_code == 409, wrong.text
    saved = client.post(human + "/decisions", json=decision)
    assert saved.status_code == 200, saved.text
    resumed = client.post(
        human + "/resume",
        json={"request_sha256": row["request_sha256"], "decision_id": saved.json()["id"]},
    )
    assert resumed.status_code == 200, resumed.text
    assert outcome(client, endpoint)["status"] == "ADMITTED"
    assert client.get(endpoint).json()["version"]["status"] == "DRAFT"
    assert lifecycle(client, case, "READY_FOR_REVIEW").status_code == 200
