"""Production generation -> durable review -> admission, without a live broker/model."""

from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_rules import FixedImageProvider
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_generation_candidates import candidates, deliver, ingest
from test_generation_jobs import capture_ok, payload

from arclight.application.continuity_review_store import REVIEW_RESOURCE
from arclight.application.runtime import execute_run
from arclight.persistence.models import GenerationAttempt, VersionBinding
from arclight.providers.media_outputs import MediaOutput
from arclight.providers.media_submission import MockMediaSubmissionProvider
from arclight.workers import tasks


class BatchImages(FixedImageProvider):
    def download(self, request, handle, max_bytes):
        assert len(self.data) <= max_bytes
        return MediaOutput(self.data, "image/png")


def bound_body(episode, case, *, count=1, legacy=False):
    binding = {name: case["target"][name] for name in ("storyboard_id", "shot_id")}
    body = payload(
        episode_id=episode["id"],
        inputs=[
            {
                "source_asset_version_id": binding["storyboard_id"],
                "dependency_type": "CONTENT_DEPENDENCY",
                "selectors": ["shot:" + binding["shot_id"]],
            }
        ],
    )
    body["spec"]["batch_size"] = count
    body["spec"]["output_spec"] = {"width": 64, "height": 96, "format": "png"}
    if legacy:
        body["spec"]["context"].update(binding)
    else:
        body["review_target"] = binding
    return body


def make_job(client, episode, case, *, count=1, legacy=False, import_images=True):
    body = bound_body(episode, case, count=count, legacy=legacy)
    job = capture_ok(client, episode["project_id"], body)
    assert deliver(client, job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(job["id"]))
        )
    if import_images:
        assert ingest(client, attempt, BatchImages("match.png")) == count
    return job, attempt


def work_for(client, project_id, candidate):
    response = client.get(
        f"/api/v1/projects/{project_id}/generation-candidates/{candidate['id']}/review"
    )
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("legacy", [False, True])
def test_normal_worker_reviews_each_checked_batch_candidate(
    client, episode, settings, reviewer_case, model_service, monkeypatch, legacy
):
    sent, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, reviewer_case, count=2, legacy=legacy, import_images=False)
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(
        tasks, "get_media_output_provider", lambda *a, **kw: BatchImages("match.png")
    )
    messages = []
    for name in (
        "execute_workflow",
        "execute_generation",
        "recover_generation",
        "execute_render_preview",
        "coordinate_production",
        "import_generation_candidates",
    ):
        monkeypatch.setattr(
            getattr(tasks, name),
            "apply_async",
            lambda *, args, _name=name, **kw: messages.append((_name, args)),
        )
    for _ in range(3):
        tasks.dispatch_outbox()
        pending, messages[:] = list(messages), []
        for name, args in pending:
            if name in {
                "execute_workflow",
                "import_generation_candidates",
                "coordinate_production",
            }:
                getattr(tasks, name)(*args)
    rows = candidates(client, episode["project_id"], job)
    assert len(rows) == 2
    for candidate in rows:
        assert candidate["media_check"]["status"] == "PASSED"
        work = work_for(client, episode["project_id"], candidate)
        assert work["status"] == "WAITING_REVIEW", work
        assert work["pending_review"]["destination"] == "READY_FOR_REVIEW", work
        with client.app.state.session_factory() as db:
            records = list(
                db.scalars(
                    select(VersionBinding).where(
                        VersionBinding.resource_type == REVIEW_RESOURCE,
                        VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                            "candidate_id"
                        ].as_string()
                        == candidate["id"],
                    )
                )
            )
            assert len(records) == 1
            assert records[0].snapshot_json["review_contract"]["review"]["conclusion"] == "PASS"
        target = {"project_id": episode["project_id"], "target": work["work"]["target"]}
        asset, version = target_rows(client, target)
        assert asset["current_version_id"] is None
        assert version["status"] == "DRAFT"
    assert len(deliveries) == 2 and sent and not errors, errors


def test_explicit_history_start_is_idempotent_and_reuses_shared_owner_gate(
    client, episode, settings, reviewer_case, model_service
):
    _, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    target = reviewer_case["target"]
    root = (
        f"/api/v1/projects/{episode['project_id']}"
        f"/generation-candidates/{target['candidate_id']}/review"
    )
    body = {name: target[name] for name in ("storyboard_id", "shot_id")}
    first = client.post(root, json=body)
    assert first.status_code == 202, first.text
    run_id = UUID(first.json()["run_id"])
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    again = client.post(root, json=body)
    assert again.status_code == 202 and again.json()["run_id"] == str(run_id), again.text
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    assert len(deliveries) == 1 and not errors
    assert (
        client.post(root, json=body | {"shot_id": reviewer_case["shots"][1]["id"]}).status_code
        == 409
    )
    assert client.post(root, json=body | {"agent_limits": {"max_steps": 1}}).status_code == 409
    current = client.get(root).json()
    case = {"project_id": episode["project_id"], "target": current["work"]["target"]}
    asset, version = target_rows(client, case)
    selection = (
        f"/api/v1/projects/{episode['project_id']}/assets/{asset['id']}"
        f"/versions/{version['id']}/select"
    )
    request = {
        "expected_revision": asset["revision"],
        "expected_lock_version": asset["lock_version"],
    }
    assert client.post(selection, json=request).status_code == 409
    approve(client, case)
    selected = client.post(selection, json=request)
    assert selected.status_code == 200, selected.text


@pytest.mark.parametrize("change", ["missing_dependency", "wrong_shot", "partial", "conflicting"])
def test_generation_rejects_inexact_review_binding(client, episode, reviewer_case, change):
    body = bound_body(episode, reviewer_case)
    if change == "missing_dependency":
        body["inputs"] = []
    elif change == "wrong_shot":
        body["review_target"]["shot_id"] = str(uuid4())
    elif change == "partial":
        body.pop("review_target")
        body["spec"]["context"]["shot_id"] = reviewer_case["target"]["shot_id"]
    else:
        body["spec"]["context"].update(body["review_target"] | {"shot_id": str(uuid4())})
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs",
        headers={"Idempotency-Key": str(uuid4())},
        json=body,
    )
    assert response.status_code in {409, 422}, response.text


def test_frozen_generation_target_cannot_be_changed_before_first_review(
    client, episode, reviewer_case
):
    job, _ = make_job(client, episode, reviewer_case)
    candidate = candidates(client, episode["project_id"], job)[0]
    endpoint = (
        f"/api/v1/projects/{episode['project_id']}/generation-candidates/{candidate['id']}/review"
    )
    response = client.post(
        endpoint,
        json={
            "storyboard_id": reviewer_case["target"]["storyboard_id"],
            "shot_id": reviewer_case["shots"][1]["id"],
        },
    )
    assert response.status_code == 409, response.text
    assert response.json()["error"]["code"] == "REVIEW_TASK_CONFLICT"
    assert work_for(client, episode["project_id"], candidate)["work"] is None


def test_pending_production_review_cannot_borrow_an_independent_old_pass(
    client, episode, settings, reviewer_case, model_service
):
    from test_asset_versions import state
    from test_continuity_reviewer import run_review
    from test_continuity_routing import route

    _, errors, _ = install_reviewer_model(model_service, dynamic=False)
    assert run_review(client, settings, reviewer_case).status == "COMPLETED"
    route(client, settings, reviewer_case)
    target = reviewer_case["target"]
    endpoint = (
        f"/api/v1/projects/{episode['project_id']}"
        f"/generation-candidates/{target['candidate_id']}/review"
    )
    response = client.post(
        endpoint, json={name: target[name] for name in ("storyboard_id", "shot_id")}
    )
    assert response.status_code == 202, response.text
    asset, version = target_rows(client, reviewer_case)
    refused = state(client, asset, version, "READY_FOR_REVIEW")
    assert refused.status_code == 409, refused.text
    assert refused.json()["error"]["code"] == "CONTINUITY_REVIEW_REQUIRED"
    execute_run(client.app.state.session_factory, settings, UUID(response.json()["run_id"]), 0)
    approve(client, reviewer_case)
    assert not errors, errors
