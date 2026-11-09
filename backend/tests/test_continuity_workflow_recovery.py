"""Durable intake, journal recovery, questions and original-run revision handoff."""

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_asset_selection import choose, current
from test_asset_versions import state
from test_continuity_escalations import pending
from test_continuity_feedback import install_feedback_model
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_rules import rules_storage as rules_storage
from test_continuity_selection import approve, target_rows
from test_continuity_workflow import BatchImages, make_job, work_for
from test_file_transfers import transfer_setup as transfer_setup
from test_generation_candidates import OutputFake, candidates, deliver, ingest
from test_human_reviews import decide, resume
from test_upload_completion import complete, ticket

from arclight.application import continuity_execution
from arclight.application.continuity_workflow import queue_pending_reviews
from arclight.application.runtime import execute_run
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.continuity_workflow import REVIEW_WORK_RESOURCE
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    Project,
    VersionBinding,
    WorkflowRun,
)
from arclight.providers.media_outputs import MediaOutput
from arclight.providers.media_submission import MockMediaSubmissionProvider
from arclight.workers import tasks


def run_work(client, settings, work):
    identity = UUID(work["work"]["run_id"])
    with client.app.state.session_factory() as db:
        revision = db.get(WorkflowRun, identity).revision
    execute_run(client.app.state.session_factory, settings, identity, revision)
    return identity


def test_restart_after_committed_review_repairs_route_without_another_model_call(
    client, episode, settings, reviewer_case, model_service, monkeypatch
):
    sent, errors, _ = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, reviewer_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    assert queue_pending_reviews(factory, settings) == 0
    candidate = candidates(client, episode["project_id"], job)[0]
    work = work_for(client, episode["project_id"], candidate)
    original = continuity_execution.route_reviewer_result

    def crash(*args, **kwargs):
        raise SystemExit("Simulated process loss after the model journal committed")

    monkeypatch.setattr(continuity_execution, "route_reviewer_result", crash)
    with pytest.raises(SystemExit):
        run_work(client, settings, work)
    count = len(sent)
    with factory() as db:
        run_id = UUID(work["work"]["run_id"])
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id))
        assert loop.status == "COMPLETED"
        control = db.get(AgentRunLimit, run_id)
        budget = (deepcopy(control.limits_json), control.deadline_at, control.root_run_id)
    monkeypatch.setattr(continuity_execution, "route_reviewer_result", original)
    messages = []
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
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
    tasks.dispatch_outbox()
    repairs = [
        args for name, args in messages if name == "execute_workflow" and args[0] == str(run_id)
    ]
    assert repairs
    for args in repairs * 2:
        tasks.execute_workflow(*args)
    assert len(sent) == count and not errors
    current = work_for(client, episode["project_id"], candidate)
    assert current["status"] == "WAITING_REVIEW", current
    assert current["pending_review"]["destination"] == "READY_FOR_REVIEW"
    with factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(VersionBinding).where(
                            VersionBinding.resource_type == ADMISSION_RESOURCE,
                            VersionBinding.snapshot_json["review_route"]["run_id"].as_string()
                            == str(run_id),
                        )
                    )
                )
            )
            == 1
        )
        control = db.get(AgentRunLimit, run_id)
        assert (control.limits_json, control.deadline_at, control.root_run_id) == budget


def test_one_corrupt_output_does_not_review_bad_image_or_starve_good_sibling(
    client, episode, settings, reviewer_case, model_service
):
    _, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    job, attempt = make_job(client, episode, reviewer_case, count=2, import_images=False)
    output = OutputFake(
        [
            MediaOutput(b"corrupted", "image/png"),
            MediaOutput(BatchImages("match.png").data, "image/png"),
        ]
    )
    assert ingest(client, attempt, output) == 2
    assert queue_pending_reviews(client.app.state.session_factory, settings) == 1
    rows = candidates(client, episode["project_id"], job)
    for candidate in rows:
        work = work_for(client, episode["project_id"], candidate)
        if candidate["asset_version_id"] is None:
            assert work["work"] is None
            assert candidate["media_check"]["status"] == "FAILED"
        else:
            run_work(client, settings, work)
            assert work_for(client, episode["project_id"], candidate)["status"] == "WAITING_REVIEW"
    assert len(deliveries) == 1 and not errors


def test_actual_question_answer_uses_same_loop_limits_and_durable_dispatch(
    client, episode, settings, reviewer_case, model_service
):
    sent, errors, deliveries = install_reviewer_model(model_service, dynamic=False, human=True)
    job, _ = make_job(client, episode, reviewer_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    run_id = run_work(client, settings, work_for(client, episode["project_id"], candidate))
    current = work_for(client, episode["project_id"], candidate)
    assert current["pending_review"]["question_id"], current
    loop_id = current["pending_review"]["loop_id"]
    with factory() as db:
        loop = db.get(AgentLoop, UUID(loop_id))
        control = db.get(AgentRunLimit, run_id)
        budget = (deepcopy(control.limits_json), control.deadline_at, control.root_run_id)
        body = {
            "question_id": current["pending_review"]["question_id"],
            "input_sha256": loop.input_sha256,
            "decision": "ANSWER",
            "answer": "补充",
        }
    endpoint = f"/api/v1/runs/{run_id}/continuity/{loop_id}/answers"
    answer = client.post(endpoint, json=body)
    assert answer.status_code == 202, answer.text
    assert client.post(endpoint, json=body).json() == answer.json()
    run_work(client, settings, current)
    assert len(deliveries) == 1 and sent and not errors, errors
    assert (
        work_for(client, episode["project_id"], candidate)["pending_review"]["destination"]
        == "READY_FOR_REVIEW"
    )
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        assert (control.limits_json, control.deadline_at, control.root_run_id) == budget
        assert len(list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))) == 1


def test_revision_new_image_reenters_original_reviewer_and_feedback_with_same_global_limits(
    client, episode, settings, reviewer_case, model_service
):
    install_reviewer_model(model_service, dynamic=False, difference=("STATE", "MEDIUM"))
    job, _ = make_job(client, episode, reviewer_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    source = candidates(client, episode["project_id"], job)[0]
    run_id = run_work(client, settings, work_for(client, episode["project_id"], source))
    current = work_for(client, episode["project_id"], source)
    assert current["pending_review"]["destination"] == "REVISION_REQUIRED", current
    review_id = current["pending_review"]["review_id"]
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        budget = (deepcopy(control.limits_json), control.deadline_at, control.root_run_id)
        reviewed = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_id == UUID(review_id),
                VersionBinding.resource_type == "REVIEW_RESULT",
            )
        )
        issue = reviewed.snapshot_json["review_contract"]["review"]["issues"][0]["issue_id"]
    request = client.post(
        f"/api/v1/projects/{episode['project_id']}/continuity-revisions",
        headers={"Idempotency-Key": "production-revision"},
        json={
            "review_id": review_id,
            "issue_ids": [issue],
            "action": "REGENERATE",
            "amendments": [],
        },
    )
    assert request.status_code == 201, request.text
    revision = request.json()
    revised_job = client.get(
        f"/api/v1/projects/{episode['project_id']}/generation-jobs/{revision['job_id']}"
    ).json()
    assert deliver(client, revised_job, MockMediaSubmissionProvider()) == "SUCCEEDED"
    with factory() as db:
        attempt = db.scalar(
            select(GenerationAttempt.id).where(GenerationAttempt.job_id == UUID(revised_job["id"]))
        )
    assert ingest(client, attempt, BatchImages("match.png")) == 1
    sent, _, reviewed_calls, errors, deliveries = install_feedback_model(
        model_service, ["READY_FOR_REVIEW"]
    )
    assert queue_pending_reviews(factory, settings) == 1
    candidate = candidates(client, episode["project_id"], revised_job)[0]
    work = work_for(client, episode["project_id"], candidate)
    assert work["work"]["run_id"] == str(run_id)
    assert work["work"]["revision_id"] == revision["revision_id"]
    run_work(client, settings, work)
    current = work_for(client, episode["project_id"], candidate)
    assert current["status"] == "WAITING_REVIEW", current
    assert current["pending_review"]["destination"] == "READY_FOR_REVIEW", current
    assert len(sent) == len(deliveries) == 1 and reviewed_calls and not errors, errors
    assert queue_pending_reviews(factory, settings) == 0
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        assert (control.limits_json, control.deadline_at, control.root_run_id) == budget
        assert (
            len(
                list(
                    db.scalars(
                        select(VersionBinding).where(
                            VersionBinding.resource_type == REVIEW_WORK_RESOURCE,
                            VersionBinding.snapshot_json["continuity_work"]["run_id"].as_string()
                            == str(run_id),
                        )
                    )
                )
            )
            == 2
        )


def test_execution_revalidates_project_after_intake_without_model_io(
    client, episode, settings, reviewer_case, model_service
):
    sent, _, _ = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, reviewer_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    work = work_for(client, episode["project_id"], candidate)
    with factory.begin() as db:
        db.get(Project, UUID(episode["project_id"])).archived = True
    run_id = run_work(client, settings, work)
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "FAILED" and run.error_code == "PROJECT_ARCHIVED"
    assert not sent


def test_postgres_parallel_intake_has_one_task_per_image(client, episode, settings, reviewer_case):
    factory = client.app.state.session_factory
    if factory.kw["bind"].dialect.name != "postgresql":
        pytest.skip("Requires isolated PostgreSQL row locks")
    make_job(client, episode, reviewer_case, count=2)
    with ThreadPoolExecutor(max_workers=2) as pool:
        values = list(pool.map(lambda _: queue_pending_reviews(factory, settings), range(2)))
    assert sum(values) == 2
    with factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(VersionBinding).where(
                            VersionBinding.resource_type == REVIEW_WORK_RESOURCE
                        )
                    )
                )
            )
            == 2
        )


def test_blocked_intake_is_durable_and_does_not_starve_next_page(
    client, episode, settings, reviewer_case
):
    factory = client.app.state.session_factory
    first, _ = make_job(client, episode, reviewer_case)
    second, _ = make_job(client, episode, reviewer_case)
    bad = candidates(client, episode["project_id"], first)[0]
    good = candidates(client, episode["project_id"], second)[0]
    with factory.begin() as db:
        db.get(AssetVersion, UUID(bad["asset_version_id"])).stale = True
    assert queue_pending_reviews(factory, settings, limit=1) == 0
    blocked = work_for(client, episode["project_id"], bad)
    assert blocked["status"] == "BLOCKED" and blocked["error_code"], blocked
    assert queue_pending_reviews(factory, settings, limit=1) == 1
    assert work_for(client, episode["project_id"], good)["work"] is not None


@pytest.fixture
def uploaded_review_case(
    transfer_setup, client, episode, settings, model_service, rules_storage, request, monkeypatch
):
    return reviewer_case.__wrapped__(
        client, episode, settings, model_service, rules_storage, request, monkeypatch
    )


def test_uploaded_replacement_gets_independent_check_and_actual_review(
    client, episode, settings, uploaded_review_case, model_service, transfer_setup
):
    _, root = transfer_setup
    _, errors, deliveries = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, uploaded_review_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    original = candidates(client, episode["project_id"], job)[0]
    original_work = work_for(client, episode["project_id"], original)
    run_work(client, settings, original_work)
    upload = ticket(client, root)
    file = complete(client, root, upload).json()
    asset = {"id": original["version"]["asset_id"], "project_id": episode["project_id"]}
    body = {
        "file_id": file["id"],
        "creation_key": str(uuid4()),
        "expected_revision": current(client, asset)["revision"],
    }
    endpoint = root + f"/generation-candidates/{original['id']}/replacements"
    response = client.post(endpoint, json=body)
    assert response.status_code == 201, response.text
    replacement = response.json()
    assert client.post(endpoint, json=body).json() == replacement
    assert replacement["media_check"]["status"] == "PASSED"
    assert replacement["asset_version_id"] != original["asset_version_id"]
    assert queue_pending_reviews(factory, settings) == 1
    work = work_for(client, episode["project_id"], replacement)
    assert work["work"]["run_id"] != original_work["work"]["run_id"]
    assert work["work"]["target"]["shot_id"] == original_work["work"]["target"]["shot_id"]
    run_work(client, settings, work)
    latest = work_for(client, episode["project_id"], replacement)
    assert latest["status"] == "WAITING_REVIEW", latest
    assert len(deliveries) == 2 and not errors, errors
    assert current(client, asset)["current_version_id"] is None


def test_actual_limit_stop_persists_human_wait_without_review_or_selection(
    client, episode, settings, reviewer_case, model_service
):
    sent, _, _ = install_reviewer_model(model_service, dynamic=False)
    job, _ = make_job(client, episode, reviewer_case)
    candidate = candidates(client, episode["project_id"], job)[0]
    binding = {name: reviewer_case["target"][name] for name in ("storyboard_id", "shot_id")}
    endpoint = (
        f"/api/v1/projects/{episode['project_id']}/generation-candidates/{candidate['id']}/review"
    )
    response = client.post(endpoint, json=binding | {"agent_limits": {"max_model_calls": 0}})
    assert response.status_code == 202, response.text
    run_work(client, settings, work_for(client, episode["project_id"], candidate))
    progress = work_for(client, episode["project_id"], candidate)
    assert progress["status"] == "WAITING_REVIEW", progress
    assert progress["pending_review"]["stop_code"] == "AGENT_MODEL_CALL_LIMIT", progress
    assert not sent


def test_initial_high_risk_review_uses_owner_exception_without_automatic_selection(
    client, episode, settings, reviewer_case, model_service
):
    _, errors, deliveries = install_reviewer_model(
        model_service, dynamic=False, difference=("STATE", "HIGH")
    )
    job, _ = make_job(client, episode, reviewer_case)
    factory = client.app.state.session_factory
    assert queue_pending_reviews(factory, settings) == 1
    candidate = candidates(client, episode["project_id"], job)[0]
    work = work_for(client, episode["project_id"], candidate)
    identity = run_work(client, settings, work)
    progress = work_for(client, episode["project_id"], candidate)
    assert progress["status"] == "WAITING_REVIEW", progress
    assert progress["pending_review"]["destination"] == "HUMAN_REVIEW", progress
    assert len(deliveries) == 1 and deliveries[0]["conclusion"] == "HUMAN_REVIEW"
    assert not errors, errors
    case = {
        "project_id": episode["project_id"],
        "run_id": identity,
        "target": work["work"]["target"],
    }
    row = pending(client, case)
    asset, version = target_rows(client, case)
    assert version["status"] == "DRAFT" and asset["current_version_id"] is None
    blocked = state(client, asset, version, "READY_FOR_REVIEW")
    assert blocked.status_code == 409, blocked.text
    saved = decide(client, row, "APPROVE")
    assert saved.status_code == 200, saved.text
    handoff = resume(client, row, saved.json())
    assert handoff.status_code == 200, handoff.text
    asset, version = target_rows(client, case)
    assert version["status"] == "DRAFT" and asset["current_version_id"] is None
    assert choose(client, asset, version).status_code == 409
    version = approve(client, case)
    selected = choose(client, current(client, asset), version)
    assert selected.status_code == 200, selected.text
