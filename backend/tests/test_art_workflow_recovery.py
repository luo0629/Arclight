"""Reopened apps/processes, transactional rollback and concurrent original-run recovery."""

import json
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import UUID

import pytest
from art_direction_support import install_direction_model, reference_set
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_art_review_workflow import decide, effects
from test_art_workflow import drive, progress, proposal, start_art

from arclight.application.agent_workflows import WorkflowExecutionDeferred
from arclight.application.runtime import execute_run
from arclight.main import create_app
from arclight.persistence.agent_loops import AgentLoop, AgentLoopQuestion
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import OutboxEvent, VisualAsset, WorkflowRun


@pytest.mark.parametrize("verdict", ["APPROVE", "REJECT"])
def test_reopened_application_and_separate_worker_process_resume_same_review_thread(
    client, episode, settings, model_service, verdict
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service, human=True)
    run_id, _, _ = start_art(client, episode, settings)
    assert drive(client, settings, run_id)["status"] == "RUNNING"
    original = progress(client, run_id)
    operation = original["operations"][0]
    # New app owns a new engine/session factory and opens the same persisted saver.
    with TestClient(create_app(settings), headers=client.headers) as restarted:
        assert progress(restarted, run_id) == original
        body = {
            "question_id": operation["question_id"],
            "input_sha256": operation["input_sha256"],
            "decision": "ANSWER",
            "answer": operation["human_request"]["options"][0],
        }
        endpoint = f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers"
        assert restarted.post(endpoint, json=body).status_code == 202
        waiting = drive(restarted, settings, run_id)
        assert waiting["status"] == "WAITING_REVIEW", waiting
        delivery = progress(restarted, run_id)["delivery"]
        row = proposal(restarted, episode, delivery)
        decided = decide(restarted, row, verdict)
        assert decided.status_code == 200, decided.text
        queued = restarted.get(f"/api/v1/runs/{run_id}").json()
        count = len(sent)
        with restarted.app.state.session_factory() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            frozen_input = run.input_json.copy()
        arguments = json.dumps(
            {
                "environment": "test",
                "database_url": settings.database_url,
                "checkpoint_url": settings.checkpoint_url,
                "asset_storage_path": str(settings.asset_storage_path),
            }
        )
        code = """
import json, sys
from uuid import UUID
from arclight.core.config import Settings
from arclight.persistence.session import build_engine, session_factory
from arclight.application.runtime import execute_run
s = Settings(_env_file=None, **json.loads(sys.argv[1]))
engine = build_engine(s)
try:
    execute_run(session_factory(engine, s), s, UUID(sys.argv[2]), int(sys.argv[3]))
finally:
    engine.dispose()
"""
        result = subprocess.run(
            [sys.executable, "-c", code, arguments, run_id, str(queued["revision"])],
            capture_output=True,
            text=True,
            timeout=45,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        final = restarted.get(f"/api/v1/runs/{run_id}").json()
        assert final["status"] == ("SUCCEEDED" if verdict == "APPROVE" else "REJECTED"), final
        assert drive(restarted, settings, run_id, queued["revision"])["status"] == final["status"]
        assert len(sent) == count and not errors
        with restarted.app.state.session_factory() as db:
            run = db.get(WorkflowRun, UUID(run_id))
            assert (
                run.input_json == frozen_input and run.agent_state_json["status"] == final["status"]
            )
            loop = db.get(AgentLoop, UUID(operation["loop_id"]))
            assert loop.thread_id == f"agent-loop:{loop.id}"
            assert loop.status == "COMPLETED"
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(AgentLoopQuestion)
                    .where(AgentLoopQuestion.loop_id == loop.id)
                )
                == 1
            )


def test_approval_failure_rolls_back_decision_formal_asset_and_resume_outbox_together(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service)
    run_id, _, _ = start_art(client, episode, settings)
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    row = proposal(client, episode, progress(client, run_id)["delivery"])
    before = effects(client)
    waiting = client.get(f"/api/v1/runs/{run_id}").json()
    from arclight.application import art_reviews

    original = art_reviews.apply_option

    def fail_after_write(*args):
        original(*args)
        raise RuntimeError("isolated failure after formal version write")

    monkeypatch.setattr(art_reviews, "apply_option", fail_after_write)
    with pytest.raises(RuntimeError, match="isolated failure"):
        decide(client, row)
    assert effects(client) == before
    assert client.get(f"/api/v1/runs/{run_id}").json() == waiting
    monkeypatch.setattr(art_reviews, "apply_option", original)
    assert decide(client, row).status_code == 200
    assert drive(client, settings, run_id)["status"] == "SUCCEEDED"
    assert len(sent) == 4 and not errors


@pytest.mark.parametrize("mode", ["proposal-api", "generic-review-api"])
def test_changed_baseline_refuses_both_review_entries_without_outbox_or_formal_write(
    client, episode, settings, model_service, mode
):
    asset, _ = reference_set(client, episode)
    sent, errors, _ = install_direction_model(model_service, existing=True)
    run_id, _, _ = start_art(client, episode, settings, target_asset_ids=[asset["id"]])
    assert drive(client, settings, run_id)["status"] == "WAITING_REVIEW"
    row = proposal(client, episode, progress(client, run_id)["delivery"])
    with client.app.state.session_factory.begin() as db:
        db.get(VisualAsset, UUID(asset["id"])).revision += 1
    before = effects(client)
    if mode == "proposal-api":
        response = decide(client, row)
    else:
        from arclight.domain.versions import digest

        response = client.post(
            f"/api/v1/human-reviews/{row['review']['id']}/decisions",
            json={
                "version_id": row["version"]["id"],
                "request_sha256": row["review"]["request_sha256"],
                "decision": "APPROVE",
                "impact_digest": digest(row["review_confirmation"]),
            },
        )
    assert response.status_code == 409, response.text
    assert effects(client) == before and len(sent) == 4 and not errors
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"


@pytest.mark.parametrize("phase", ["answer", "review"])
def test_postgres_parallel_workers_and_duplicate_decisions_keep_one_delivery(
    client, episode, settings, model_service, phase
):
    if client.app.state.engine.dialect.name != "postgresql":
        pytest.skip("PostgreSQL worker fencing and concurrent API transaction evidence")
    reference_set(client, episode)
    entered, release = threading.Event(), threading.Event()

    def on_http(payload):
        if (
            phase == "answer"
            and not entered.is_set()
            and any("human_decision" in str(m.get("content")) for m in payload["messages"])
        ):
            entered.set()
            assert release.wait(10)

    sent, errors, _ = install_direction_model(
        model_service, human=phase == "answer", on_http=on_http
    )
    run_id, _, _ = start_art(client, episode, settings)
    drive(client, settings, run_id)
    if phase == "answer":
        operation = progress(client, run_id)["operations"][0]
        response = client.post(
            f"/api/v1/runs/{run_id}/art/{operation['loop_id']}/answers",
            json={
                "question_id": operation["question_id"],
                "input_sha256": operation["input_sha256"],
                "decision": "ANSWER",
                "answer": operation["human_request"]["options"][0],
            },
        )
        assert response.status_code == 202, response.text
    else:
        row = proposal(client, episode, progress(client, run_id)["delivery"])
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: decide(client, row), range(2)))
        assert all(r.status_code == 200 for r in responses), [r.text for r in responses]
        assert responses[0].json() == responses[1].json()
    current = client.get(f"/api/v1/runs/{run_id}").json()

    def worker():
        try:
            execute_run(
                client.app.state.session_factory, settings, UUID(run_id), current["revision"]
            )
            return "finished"
        except WorkflowExecutionDeferred:
            return "deferred"

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(worker)
        if phase == "answer":
            assert entered.wait(10)
        second = pool.submit(worker)
        if phase == "answer":
            try:
                assert second.result(timeout=10) == "deferred"
            finally:
                release.set()
        results = [first.result(timeout=20), second.result(timeout=20)]
    assert "finished" in results and not errors, errors
    final = client.get(f"/api/v1/runs/{run_id}").json()
    assert final["status"] == ("WAITING_REVIEW" if phase == "answer" else "SUCCEEDED"), final
    delivery = progress(client, run_id)["delivery"]
    factory = client.app.state.session_factory
    with factory() as db:
        assert (
            db.scalar(
                select(func.count()).select_from(AgentLoop).where(AgentLoop.run_id == UUID(run_id))
            )
            == 1
        )
        assert (
            db.scalar(
                select(func.count())
                .select_from(ArtProposalVersion)
                .where(ArtProposalVersion.id == UUID(delivery["version_id"]))
            )
            == 1
        )
        reviews = list(
            db.scalars(select(HumanReviewRequest).where(HumanReviewRequest.run_id == UUID(run_id)))
        )
        assert len(reviews) == 1 and str(reviews[0].id) == delivery["review_request_id"]
        assert db.scalar(
            select(func.count())
            .select_from(HumanReviewDecision)
            .where(HumanReviewDecision.request_id == reviews[0].id)
        ) == (phase == "review")
        assert db.scalar(
            select(func.count())
            .select_from(HumanReviewResume)
            .where(HumanReviewResume.request_id == reviews[0].id)
        ) == (phase == "review")
        assert db.scalar(
            select(func.count())
            .select_from(ArtApprovedVersion)
            .where(ArtApprovedVersion.proposal_version_id == UUID(delivery["version_id"]))
        ) == (phase == "review")
        assert (
            db.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(OutboxEvent.run_id == UUID(run_id))
            )
            == 2
        )
