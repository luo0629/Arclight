"""Durable working shots, native human questions and real PostgreSQL worker contention."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from test_agent_model import model_service as model_service
from test_concurrency import require_postgres
from test_director_creation import install, progress, start
from test_screenwriter_creation import approve, run_once
from test_screenwriter_creation import start as start_writer

from arclight.application.runtime import execute_run
from arclight.application.screenwriter_execution import ScreenwriterExecutionDeferred
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopDecision
from arclight.persistence.models import OutboxEvent, Storyboard, WorkflowRun


def answer_body(question, decision="ANSWER"):
    return {
        "question_id": question["question_id"],
        "input_sha256": question["input_sha256"],
        "decision": decision,
        "answer": question["human_request"]["options"][0] if decision == "ANSWER" else None,
    }


def endpoint(run_id, question, role="director"):
    return f"/api/v1/runs/{run_id}/{role}/{question['loop_id']}/answers"


@pytest.mark.parametrize("human", ["plan", 2])
@pytest.mark.parametrize("combined", [False, True])
def test_question_restarts_and_reenters_same_working_loops(
    client, episode, settings, model_service, monkeypatch, human, combined
):
    sent, errors = install(model_service, monkeypatch, settings, human=human)
    if combined:
        run_id = start_writer(client, episode, professional_director=True)
        run = run_once(client, settings, run_id)
        approve(client, run)
    else:
        run_id, _ = start(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING" and run["pending_review"] is None, (run, errors)
    before = copy.deepcopy(progress(client, run_id))
    question = before["operations"][-1]
    assert question["status"] == "WAITING_HUMAN" and before["can_cancel"]
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
    if combined:
        assert (
            client.post(
                endpoint(run_id, question, "screenwriter"), json=answer_body(question)
            ).status_code
            == 404
        )
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        body = answer_body(question)
        assert restarted.post(endpoint(run_id, question), json=body).status_code == 202
        assert restarted.post(endpoint(run_id, question), json=body).status_code == 202
        assert (
            restarted.post(
                endpoint(run_id, question), json=body | {"answer": "冲突答案"}
            ).status_code
            == 409
        )
        completed = run_once(restarted, settings, run_id)
        assert completed["status"] == "WAITING_REVIEW", (completed, errors)
        assert completed["pending_review"]["resource_type"] == "storyboard"
        after = progress(restarted, run_id)["operations"]
        assert [x["loop_id"] for x in after[: len(before["operations"])]] == [
            x["loop_id"] for x in before["operations"]
        ]
        approve(restarted, completed)
        assert run_once(restarted, settings, run_id)["status"] == "SUCCEEDED"
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
    assert len(sent) == (5 if combined else 6) and not errors


@pytest.mark.parametrize("cancel", [False, True])
def test_stop_retains_work_and_cancel_needs_no_new_deadline_admission(
    client, episode, settings, model_service, monkeypatch, cancel
):
    sent, errors = install(model_service, monkeypatch, settings, human=2)
    run_id, _ = start(client, episode)
    run_once(client, settings, run_id)
    before = progress(client, run_id)["operations"]
    question = before[-1]
    if cancel:
        from arclight.application import agent_limits
        from arclight.application.model_gateway import utc

        with client.app.state.session_factory() as db:
            deadline = utc(db.get(AgentRunLimit, run_id).deadline_at)
        monkeypatch.setattr(agent_limits, "utcnow", lambda: deadline + timedelta(seconds=1))
        response = client.post(f"/api/v1/runs/{run_id}/cancel")
        assert response.status_code == 200, response.text
    else:
        response = client.post(endpoint(run_id, question), json=answer_body(question, "STOP"))
        assert response.status_code == 202, response.text
        run_once(client, settings, run_id)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "CANCELLED", (run, errors)
    after = progress(client, run_id)
    assert not after["can_cancel"]
    assert after["operations"][0:2] == before[0:2]
    assert len(sent) == 4
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_answer_and_outbox_transaction_roll_back_together(
    client, episode, settings, model_service, monkeypatch
):
    install(model_service, monkeypatch, settings, human=2)
    run_id, _ = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)["operations"][-1]
    import arclight.application.screenwriter_progress as service

    def die(*args):
        raise RuntimeError("isolated enqueue failure")

    monkeypatch.setattr(service, "emit", die)
    with pytest.raises(RuntimeError, match="isolated enqueue failure"):
        client.post(endpoint(run_id, question), json=answer_body(question))
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 0
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 1
        )
        assert db.get(WorkflowRun, run_id).status == "RUNNING"


@pytest.mark.parametrize("combined", [False, True])
def test_explicit_retry_reuses_completed_scenes_and_original_root_limits(
    client, episode, settings, model_service, monkeypatch, combined
):
    invalid = [True]

    def mutate(phase, result, seed):
        if invalid[0] and phase == "scene" and seed["scene"]["scene_no"] == 2:
            result["shots"][0]["dialogue_line_ids"] = [str(uuid4())]

    sent, errors = install(model_service, monkeypatch, settings, mutate=mutate)
    if combined:
        run_id = start_writer(client, episode, professional_director=True)
        approve(client, run_once(client, settings, run_id))
    else:
        run_id, _ = start(client, episode)
    assert run_once(client, settings, run_id)["status"] == "FAILED", errors
    saved = progress(client, run_id)["operations"]
    with client.app.state.session_factory() as db:
        limits = db.get(AgentRunLimit, run_id)
        deadline, policy = limits.deadline_at, copy.deepcopy(limits.limits_json)
    invalid[0] = False
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    after = progress(client, run_id)["operations"]
    assert after[0:2] == saved[0:2]
    assert after[2]["status"] == "STOPPED" and after[3]["status"] == "COMPLETED"
    assert len(sent) == (5 if combined else 6)
    with client.app.state.session_factory() as db:
        limits = db.get(AgentRunLimit, run_id)
        assert (limits.deadline_at, limits.limits_json) == (deadline, policy)


@pytest.mark.parametrize("crash_at", ["fragment", "candidate"])
def test_crash_after_durable_write_replays_without_duplicate_io(
    client, episode, settings, model_service, monkeypatch, crash_at
):
    from arclight.application import director_execution as execution
    from arclight.application.artifacts import ArtifactStore

    sent, errors = install(model_service, monkeypatch, settings)
    run_id, _ = start(client, episode)

    class WorkerDied(BaseException):
        pass

    original = execution.create_fragment if crash_at == "fragment" else ArtifactStore.storyboard

    def die(*args, **kwargs):
        original(*args, **kwargs)
        raise WorkerDied

    target, name = (
        (execution, "create_fragment") if crash_at == "fragment" else (ArtifactStore, "storyboard")
    )
    monkeypatch.setattr(target, name, die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    assert len(sent) == (3 if crash_at == "fragment" else 5)
    monkeypatch.setattr(target, name, original)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        run = run_once(restarted, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert len(sent) == 5 and not errors
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


def test_postgres_parallel_workers_are_fenced_and_http_holds_no_business_lock(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    entered, release = Event(), Event()

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"])["data"]
        if seed["phase"] == "DIRECT_SCENE_ONLY" and seed["scene"]["scene_no"] == 1:
            entered.set()
            assert release.wait(20)

    sent, errors = install(model_service, monkeypatch, settings, on_http=on_http)
    run_id, _ = start(client, episode)
    factory = client.app.state.session_factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(execute_run, factory, settings, run_id, 0)
        try:
            assert entered.wait(15)
            with factory.begin() as db:
                db.execute(text("SET LOCAL lock_timeout='2s'"))
                assert (
                    db.scalar(
                        select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update()
                    ).status
                    == "RUNNING"
                )
            duplicate = pool.submit(execute_run, factory, settings, run_id, 0)
            with pytest.raises(ScreenwriterExecutionDeferred):
                duplicate.result(timeout=10)
        finally:
            release.set()
        future.result(timeout=30)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 5, (run, errors)
