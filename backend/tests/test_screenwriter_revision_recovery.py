"""Revision boundaries, current authority and real PostgreSQL execution contention."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from sqlalchemy import func, select, text
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_screenwriter_creation import progress, run_once, start
from test_screenwriter_revision import answer_body, install_revision
from test_script_rewrite import body as rewrite_body
from test_script_rewrite import source as rewrite_source
from test_script_rewrite import start as start_rewrite

from arclight.application.runtime import execute_run
from arclight.application.screenwriter_execution import ScreenwriterExecutionDeferred
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.models import OutboxEvent, Project, Script, User, WorkflowRun


@pytest.mark.parametrize(
    "case", ["source", "scene", "instructions", "line", "speaker", "operation"]
)
def test_invalid_patch_preserves_user_contract_and_returns_feedback_before_correction(
    client, episode, settings, model_service, monkeypatch, case
):
    def invalid(arguments, seed):
        if case == "source":
            arguments["source_sha256"] = "f" * 64
        elif case == "scene":
            arguments["target"]["scene_id"] = seed["source_script"]["scenes"][2]["scene_id"]
        elif case == "instructions":
            arguments["instructions"] = {"instruction": "扩大改写授权"}
        elif case == "line":
            arguments["patch"]["scenes"][0]["dialogue"][0]["line_id"] = seed["source_script"][
                "scenes"
            ][0]["dialogue"][0]["line_id"]
        elif case == "speaker":
            arguments["patch"]["scenes"][0]["dialogue"][0]["speaker"] = "未定义人物"
        else:
            arguments["operation_key"] = "other-operation"

    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, invalid_first=invalid
    )
    base = rewrite_source(client, episode)
    response = start_rewrite(client, episode, rewrite_body(base) | {"professional": True})
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors, ordinary_errors)
    report = next(item for item in progress(client, run_id) if item["kind"] == "critique")
    checks = report["revision"]["patch_checks"]
    assert checks[-1]["accepted"] is True
    if case != "operation":
        assert checks[0]["accepted"] is False
    assert any("SCREENWRITER_PATCH" in str(item["messages"]) for item in sent)
    with client.app.state.session_factory() as db:
        candidate = db.scalar(select(Script).where(Script.run_id == run_id))
        assert candidate.content_json["scenes"][0] == base["content_json"]["scenes"][0]
        assert candidate.content_json["scenes"][2] == base["content_json"]["scenes"][2]


@pytest.mark.parametrize("mutation", ["disabled", "archived", "cancelled", "input"])
def test_authority_and_frozen_input_are_rechecked_after_critique_http(
    client, settings, model_service, monkeypatch, mutation
):
    owner = register(client, "revision-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    target = []
    changed = []

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"]).get("data", {})
        if seed.get("phase") != "CRITIQUE_SCRIPT" or changed:
            return
        changed.append(True)
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, target[0])
            if mutation == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif mutation == "archived":
                db.get(Project, UUID(project["id"])).archived = True
            elif mutation == "cancelled":
                run.status = "CANCELLED"
            else:
                run.input_json = run.input_json | {"target_duration_ms": 60000}

    _, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, on_http=on_http
    )
    target.append(start(client, episode, revision_rounds=1))
    execute_run(client.app.state.session_factory, settings, target[0], 0)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, target[0])
        assert run.status in {"FAILED", "CANCELLED"}, (errors, ordinary_errors)
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run.id)) == 0
        )


def test_zero_repairs_escalates_and_can_cancel_after_deadline_with_history(
    client, episode, settings, model_service, monkeypatch
):
    sent, _, _ = install_revision(model_service, monkeypatch, settings, patches=1)
    run_id = start(client, episode, revision_rounds=0)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    original = copy.deepcopy(progress(client, run_id))
    assert original[-1]["revision"]["remaining_patches"] == 0
    from arclight.application.model_gateway import utc

    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    monkeypatch.setattr(
        "arclight.application.agent_limits.utcnow", lambda: utc(deadline) + timedelta(seconds=1)
    )
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert progress(client, run_id) == original
    assert len(sent) == 5


def test_unresolved_human_answer_asks_again_without_resetting_repairs(
    client, episode, settings, model_service, monkeypatch
):
    _, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, patches=1, human_unresolved=True
    )
    run_id = start(client, episode, revision_rounds=0)
    run_once(client, settings, run_id)
    waiting = progress(client, run_id)[-1]
    endpoint = f"/api/v1/runs/{run_id}/screenwriter/{waiting['loop_id']}/answers"
    assert client.post(endpoint, json=answer_body(waiting)).status_code == 202
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING", (run, errors, ordinary_errors)
    current = progress(client, run_id)[-1]
    assert (
        current["loop_id"] == waiting["loop_id"]
        and current["question_id"] != waiting["question_id"]
    )
    assert current["revision"]["remaining_patches"] == 0
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 0
        )


def test_postgres_duplicate_workers_are_fenced_without_holding_run_lock_during_critique(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    entered, release = Event(), Event()
    first = [True]

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"]).get("data", {})
        if seed.get("phase") == "CRITIQUE_SCRIPT" and first[0]:
            first[0] = False
            entered.set()
            assert release.wait(20)

    sent, errors, ordinary_errors = install_revision(
        model_service, monkeypatch, settings, on_http=on_http
    )
    run_id = start(client, episode, revision_rounds=1)
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
    assert run["status"] == "WAITING_REVIEW" and len(sent) == 7, (run, errors, ordinary_errors)
    with factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Script).where(Script.run_id == run_id)) == 1
        )


def test_postgres_concurrent_escalation_answers_enqueue_once(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    from arclight.application.screenwriter_progress import queue_answer
    from arclight.domain.agent_runtime import AgentHumanDecision

    install_revision(model_service, monkeypatch, settings, patches=1)
    run_id = start(client, episode, revision_rounds=0)
    run_once(client, settings, run_id)
    waiting = progress(client, run_id)[-1]
    decision = AgentHumanDecision.model_validate(answer_body(waiting))
    factory = client.app.state.session_factory

    def submit(_):
        with factory.begin() as db:
            return queue_answer(
                db, run_id, UUID(waiting["loop_id"]), "service-owner", decision
            ).revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, range(2)))
    assert results == [1, 1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 2
        )
        assert (
            db.scalar(select(func.count()).select_from(AgentLoop).where(AgentLoop.run_id == run_id))
            == 5
        )
