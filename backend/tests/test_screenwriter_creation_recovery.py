"""Current authority, saved history and real PostgreSQL contention around professional runs."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_screenwriter_creation import approve, install, progress, run_once, start

from arclight.application.runtime import execute_run
from arclight.application.screenwriter_execution import ScreenwriterExecutionDeferred
from arclight.domain.model_gateway import GatewayPolicy
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    OutboxEvent,
    Project,
    ProjectMember,
    Script,
    User,
    WorkflowRun,
)


def test_answer_survives_application_restart_and_conflicting_answer_is_rejected(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
):
    sent, errors = install(model_service, monkeypatch, settings, human=2)
    run_id = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)[-1]
    body = {
        "question_id": question["question_id"],
        "input_sha256": question["input_sha256"],
        "decision": "ANSWER",
        "answer": "补充",
    }
    endpoint = f"/api/v1/runs/{run_id}/screenwriter/{question['loop_id']}/answers"
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        assert restarted.post(endpoint, json=body).status_code == 202
        assert restarted.post(endpoint, json=body | {"answer": "停止"}).status_code == 409
        run = run_once(restarted, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run["error_code"], errors)
        assert len(progress(restarted, run_id)) == 3
    assert len(sent) == 5


def test_answer_and_outbox_roll_back_together(
    client, episode, settings, model_service, monkeypatch
):
    sent, _ = install(model_service, monkeypatch, settings, human=2)
    run_id = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)[-1]
    import arclight.application.screenwriter_progress as service

    def die(*args):
        raise RuntimeError("isolated enqueue failure")

    monkeypatch.setattr(service, "emit", die)
    with pytest.raises(RuntimeError, match="isolated enqueue failure"):
        client.post(
            f"/api/v1/runs/{run_id}/screenwriter/{question['loop_id']}/answers",
            json={
                "question_id": question["question_id"],
                "input_sha256": question["input_sha256"],
                "decision": "ANSWER",
                "answer": "补充",
            },
        )
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 0
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 1
        )
    assert len(sent) == 4


@pytest.mark.parametrize(
    "case", ["foreign", "viewer", "disabled", "archived", "stale", "other_question"]
)
def test_progress_and_answer_authority_scope(client, settings, model_service, monkeypatch, case):
    owner = register(client, "writer-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    sent, _ = install(model_service, monkeypatch, settings, human=2)
    run_id = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)[-1]
    body = {
        "question_id": question["question_id"],
        "input_sha256": question["input_sha256"],
        "decision": "ANSWER",
        "answer": "补充",
    }
    if case in {"foreign", "viewer"}:
        other = register(client, "other-writer@example.test")
        if case == "viewer":
            with client.app.state.session_factory.begin() as db:
                db.add(
                    ProjectMember(
                        project_id=UUID(project["id"]),
                        user_id=UUID(other["user"]["id"]),
                        role="viewer",
                    )
                )
    elif case == "disabled":
        with client.app.state.session_factory.begin() as db:
            db.get(User, UUID(owner["user"]["id"])).disabled = True
    elif case == "archived":
        with client.app.state.session_factory.begin() as db:
            db.get(Project, UUID(project["id"])).archived = True
    elif case == "stale":
        body["input_sha256"] = "f" * 64
    else:
        body["question_id"] = str(uuid4())
    read = client.get(f"/api/v1/runs/{run_id}/screenwriter")
    if case == "foreign":
        assert read.status_code == 404
    elif case != "disabled":
        assert read.status_code == 200
    response = client.post(
        f"/api/v1/runs/{run_id}/screenwriter/{question['loop_id']}/answers", json=body
    )
    assert response.status_code in {401, 403, 404, 409}, response.text
    assert len(sent) == 4
    if read.status_code == 200:
        serialized = read.text
        assert all(
            word not in serialized
            for word in (
                "system_instruction",
                "seed_json",
                "llm_api_key",
                "private-agent-transport-key",
            )
        )


@pytest.mark.parametrize("case", ["disabled", "archived", "cancelled", "changed_input"])
def test_authority_changes_during_model_io_prevent_publication(
    client, settings, model_service, monkeypatch, case
):
    owner = register(client, "active-writer@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    target = []

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"]).get("data", {})
        if seed.get("phase") != "WRITE_SCENE_ONLY" or seed["scene_no"] != 2:
            return
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, target[0])
            if case == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif case == "archived":
                db.get(Project, UUID(project["id"])).archived = True
            elif case == "cancelled":
                run.status = "CANCELLED"
            else:
                run.input_json = run.input_json | {"target_duration_ms": 60000}

    _, errors = install(model_service, monkeypatch, settings, on_http=on_http)
    target.append(start(client, episode))
    execute_run(client.app.state.session_factory, settings, target[0], 0)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, target[0])
        assert run.status in {"FAILED", "CANCELLED"}, errors
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == target[0]))
            == 0
        )


@pytest.mark.parametrize("limits", [None, {}, {"max_model_calls": 32}])
def test_optional_spending_limit_keeps_unknown_cost_and_requires_quote_only_when_set(
    client, episode, settings, model_service, monkeypatch, limits
):
    sent, errors = install(model_service, monkeypatch, settings)
    settings.model_gateway_policy = GatewayPolicy()
    run_id = start(client, episode, **({"agent_limits": limits} if limits is not None else {}))
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).limits_json["maximum_cost"] is None


def test_setting_spending_limit_without_quote_stops_before_http(
    client, episode, settings, model_service, monkeypatch
):
    sent, _ = install(model_service, monkeypatch, settings)
    settings.model_gateway_policy = GatewayPolicy()
    run_id = start(client, episode, agent_limits={"maximum_cost": "1"})
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "BUDGET_QUOTE_UNAVAILABLE"
    assert len(sent) == 0


def test_completed_run_history_remains_readable_after_another_run(
    client, episode, settings, model_service, monkeypatch
):
    _, errors = install(model_service, monkeypatch, settings)
    original = start(client, episode)
    run = run_once(client, settings, original)
    saved = copy.deepcopy(progress(client, original))
    for _ in range(2):
        approve(client, run)
        run = run_once(client, settings, original)
    assert run["status"] == "SUCCEEDED", errors
    another = start(client, episode)
    assert run_once(client, settings, another)["status"] == "WAITING_REVIEW"
    assert progress(client, original) == saved
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Script)) == 2


def test_postgres_duplicate_workers_do_not_hold_run_lock_during_http(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    entered, release = Event(), Event()
    first = [True]

    def on_http(payload):
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(20)

    sent, errors = install(model_service, monkeypatch, settings, on_http=on_http)
    run_id = start(client, episode)
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
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, (run["error_code"], errors)


def test_worker_redelivery_retries_live_lease_and_recovers_after_expiry(
    client, episode, settings, model_service, monkeypatch
):
    from celery.exceptions import Retry

    from arclight.application import screenwriter_execution as execution
    from arclight.workers import tasks

    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    original = execution._review_graph

    class WorkerDied(BaseException):
        pass

    def die(*args):
        raise WorkerDied()

    monkeypatch.setattr(execution, "_review_graph", die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    monkeypatch.setattr(execution, "_review_graph", original)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        saved = db.get(WorkflowRun, run_id)
        plan = db.get(
            AgentLoop,
            UUID(saved.agent_state_json["extensions"]["screenwriter.planning"]["loop_id"]),
        )
        plan.lease_token = uuid4()
        plan.lease_expires_at = utcnow() + timedelta(seconds=60)
        plan_id = plan.id
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    monkeypatch.setattr(tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(tasks, "build_engine", lambda _: SimpleNamespace(dispose=lambda: None))
    monkeypatch.setattr(tasks, "session_factory", lambda *args: factory)
    retries = []

    def broker_retry(**kwargs):
        retries.append(kwargs)
        raise Retry()

    monkeypatch.setattr(tasks.execute_workflow, "retry", broker_retry)
    with pytest.raises(Retry):
        tasks.execute_workflow.run(str(run_id), 0)
    assert len(retries) == 1 and retries[0]["countdown"] == 10
    assert tasks.execute_workflow.max_retries is None
    assert isinstance(retries[0]["exc"], ScreenwriterExecutionDeferred)
    assert len(sent) == 4
    with factory.begin() as db:
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        db.get(AgentLoop, plan_id).lease_expires_at = utcnow() - timedelta(seconds=1)
    tasks.execute_workflow.run(str(run_id), 0)
    run = client.get(f"/api/v1/runs/{run_id}").json()
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 4, errors
    with factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline


def test_postgres_concurrent_answers_enqueue_once(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    from arclight.application.screenwriter_progress import queue_answer
    from arclight.domain.agent_runtime import AgentHumanDecision

    sent, _ = install(model_service, monkeypatch, settings, human=2)
    run_id = start(client, episode)
    run_once(client, settings, run_id)
    question = progress(client, run_id)[-1]
    decision = AgentHumanDecision(
        question_id=question["question_id"],
        input_sha256=question["input_sha256"],
        decision="ANSWER",
        answer="补充",
    )
    factory = client.app.state.session_factory

    def answer():
        with factory.begin() as db:
            return queue_answer(
                db, run_id, UUID(question["loop_id"]), "service-owner", decision
            ).revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: answer(), range(2)))
    assert results == [1, 1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 2
        )
    assert len(sent) == 4
