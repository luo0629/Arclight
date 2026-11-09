"""Current permissions, trusted source versions and immutable revision recovery."""

from concurrent.futures import ThreadPoolExecutor
from uuid import UUID, uuid4

import pytest
from continuity_reviewer_support import install_reviewer_model
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from test_agent_model import model_service as model_service
from test_collaboration import register
from test_concurrency import require_postgres
from test_continuity_reviewer import reviewer_case as reviewer_case
from test_continuity_reviewer import run_review
from test_continuity_revision_boundaries import checked_request, retry
from test_continuity_revisions import (
    execution_counts,
    request_revision,
    revision_job,
)
from test_continuity_revisions import (
    revision_case as revision_case,
)
from test_continuity_routing import new_review_run, route
from test_continuity_rules import rules_storage as rules_storage
from test_generation_delivery import Provider, deliver

from arclight.application.continuity_routing import read_routed_review
from arclight.application.generation_attempts import start_attempt
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import ReviewRerun
from arclight.domain.continuity_revisions import REVISION_RESOURCE
from arclight.domain.versions import digest
from arclight.persistence.models import (
    GenerationAttempt,
    ProjectMember,
    User,
    VersionBinding,
    WorkflowRun,
)
from arclight.providers.media_submission import SubmissionReceipt


@pytest.mark.parametrize("difference", [None, ("PROP", "HIGH")])
def test_pass_and_human_routes_cannot_authorize_a_generation(
    client, settings, model_service, reviewer_case, difference
):
    install_reviewer_model(model_service, dynamic=False, difference=difference)
    outcome = run_review(client, settings, reviewer_case)
    assert outcome.status == "COMPLETED", outcome.model_dump_json()
    route(client, settings, reviewer_case)
    case = reviewer_case | {
        "command": {
            "review_id": str(outcome.result.result_id),
            "issue_ids": [str(uuid4())],
            "action": "REGENERATE",
        }
    }
    before = execution_counts(client)
    response = request_revision(client, case)
    assert (
        response.status_code == 409 and response.json()["error"]["code"] == "REVISION_NOT_ALLOWED"
    ), response.text
    assert execution_counts(client) == before


@pytest.mark.parametrize("change", ["disabled", "membership"])
@pytest.mark.parametrize("phase", ["EXECUTE", "RETRY"])
def test_revoked_revision_initiator_cannot_be_replaced_by_another_authorized_actor(
    client, revision_case, change, phase
):
    account = register(client, uuid4().hex + "@example.com")
    actor = account["user"]["id"]
    with client.app.state.session_factory.begin() as db:
        db.add(
            ProjectMember(
                project_id=UUID(revision_case["project_id"]), user_id=UUID(actor), role="editor"
            )
        )
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    if phase == "RETRY":
        assert (
            deliver(
                client,
                job,
                Provider(
                    receipt=SubmissionReceipt(status="FAILED", error_code="PROVIDER_FAILED", cost=0)
                ),
            )
            == "FAILED"
        )
        job = revision_job(client, revision_case, revision)
    with client.app.state.session_factory.begin() as db:
        if change == "disabled":
            db.get(User, UUID(actor)).disabled = True
        else:
            db.execute(ProjectMember.__table__.delete().where(ProjectMember.user_id == UUID(actor)))
    client.headers["Authorization"] = "Bearer test-token"
    before = execution_counts(client)
    if phase == "EXECUTE":
        with pytest.raises(DomainError) as error, client.app.state.session_factory.begin() as db:
            start_attempt(db, UUID(job["id"]), job["revision"])
        assert error.value.status_code in {403, 404}
    else:
        response = retry(client, job)
        assert response.status_code in {403, 404}, response.text
    assert execution_counts(client) == before


def test_new_authoritative_review_blocks_queued_old_revision(
    client, episode, settings, model_service, revision_case
):
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    current = new_review_run(client, episode, settings, revision_case)
    install_reviewer_model(model_service, dynamic=False)
    with client.app.state.session_factory() as db:
        original, _ = read_routed_review(db, revision_case["review"].result_id)
    rerun = ReviewRerun(
        previous_review_id=revision_case["review"].result_id,
        previous_sha256=digest(original.model_dump(mode="json")),
        reason="Recheck the same exact candidate before spending",
    )
    outcome = run_review(client, settings, current, rerun=rerun)
    assert outcome.status == "COMPLETED" and outcome.result.conclusion == "PASS", (
        outcome.model_dump_json()
    )
    route(client, settings, current)
    before = execution_counts(client)
    with pytest.raises(DomainError) as error, client.app.state.session_factory.begin() as db:
        start_attempt(db, UUID(job["id"]), job["revision"])
    assert error.value.code == "REVIEW_RESULT_STALE"
    assert execution_counts(client) == before


def test_saved_revision_history_cannot_be_changed_or_deleted(client, revision_case):
    revision = checked_request(client, revision_case)
    for remove in (False, True):
        with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
            binding = db.scalar(
                select(VersionBinding).where(
                    VersionBinding.resource_type == REVISION_RESOURCE,
                    VersionBinding.resource_id == UUID(revision["revision_id"]),
                )
            )
            if remove:
                db.delete(binding)
            else:
                binding.snapshot_json = {"forged": True}
            db.flush()
    response = client.get(
        f"/api/v1/projects/{revision_case['project_id']}/continuity-revisions/{revision['revision_id']}"
    )
    assert response.status_code == 200 and response.json() == revision


def test_fresh_application_replays_original_receipt_without_new_work(
    client, settings, revision_case
):
    from fastapi.testclient import TestClient

    from arclight.main import create_app

    response = request_revision(client, revision_case, key="restart")
    assert response.status_code == 201, response.text
    before = execution_counts(client)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers["Authorization"] = "Bearer test-token"
        replay = request_revision(restarted, revision_case, key="restart")
        assert replay.status_code == 200 and replay.json() == response.json(), replay.text
    assert execution_counts(client) == before


def test_postgres_worker_does_not_take_workflow_lock_after_job_and_project(
    client, settings, revision_case
):
    require_postgres(settings)
    revision = checked_request(client, revision_case)
    job = revision_job(client, revision_case, revision)
    factory = client.app.state.session_factory

    def begin_attempt():
        with factory.begin() as db:
            return start_attempt(db, UUID(job["id"]), job["revision"]).id

    with ThreadPoolExecutor(max_workers=1) as pool, factory.begin() as lock:
        lock.get(WorkflowRun, revision_case["run_id"], with_for_update=True)
        attempt_id = pool.submit(begin_attempt).result(timeout=15)
    with factory() as db:
        assert db.get(GenerationAttempt, attempt_id).job_id == UUID(job["id"])
