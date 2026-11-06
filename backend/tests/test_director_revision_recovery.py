"""Native human decisions, frozen repair counts, crash replay and real PG contention."""

import copy
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_director_creation import start
from test_director_revision import answer_body, checks, install_revision
from test_director_revision_boundaries import missing
from test_screenwriter_creation import run_once

from arclight.application.runtime import execute_run
from arclight.application.screenwriter_execution import ScreenwriterExecutionDeferred
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopDecision
from arclight.persistence.models import (
    AssetVersion,
    OutboxEvent,
    Project,
    Script,
    Storyboard,
    User,
    WorkflowRun,
)


def test_zero_repairs_waits_across_restart_and_repeated_answer_without_reset(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=missing, human_unresolved=True
    )
    run_id, _ = start(client, episode, director_revision_rounds=0)
    assert run_once(client, settings, run_id)["status"] == "RUNNING", (errors, base_errors)
    original = checks(client, run_id)[-1]
    assert (
        original["status"] == "WAITING_HUMAN"
        and original["director_revision"]["remaining_patches"] == 0
    )
    assert original["director_revision"]["initial_check"]["issue_count"] > 0
    endpoint = f"/api/v1/runs/{run_id}/director/{original['loop_id']}/answers"
    count = len(sent)
    assert run_once(client, settings, run_id)["status"] == "RUNNING" and len(sent) == count
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        assert checks(restarted, run_id)[-1] == original
        assert restarted.post(endpoint, json=answer_body(original)).status_code == 202
        assert run_once(restarted, settings, run_id)["status"] == "RUNNING", (errors, base_errors)
        current = checks(restarted, run_id)[-1]
        assert (
            current["loop_id"] == original["loop_id"]
            and current["question_id"] != original["question_id"]
        )
        assert current["director_revision"]["remaining_patches"] == 0
        assert restarted.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
        assert checks(restarted, run_id)[-1] == current
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_partial_repairs_exhaust_then_failed_answer_retry_retains_receipts_and_root_limits(
    client, episode, settings, model_service, monkeypatch
):
    def mutate(result, seed):
        if seed["scene"]["scene_no"] <= 2:
            result["shots"][0]["dialogue_line_ids"] = []

    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=mutate, accept_invalid_after_answer=True
    )
    run_id, _ = start(client, episode, director_revision_rounds=1)
    assert run_once(client, settings, run_id)["status"] == "RUNNING", (errors, base_errors)
    reports = checks(client, run_id)
    assert [op["status"] for op in reports] == ["COMPLETED", "WAITING_HUMAN"]
    assert reports[-1]["director_revision"]["remaining_patches"] == 0
    saved_source = copy.deepcopy(reports[-1]["director_revision"]["working_storyboard"])
    waiting = reports[-1]
    with client.app.state.session_factory() as db:
        limits = db.get(AgentRunLimit, run_id)
        frozen = (limits.deadline_at, limits.limits_json)
    assert (
        client.post(
            f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
            json=answer_body(waiting),
        ).status_code
        == 202
    )
    assert run_once(client, settings, run_id)["status"] == "FAILED"
    count = len(sent)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["status"] == "RUNNING", (errors, base_errors)
    assert len(sent) == count
    after = checks(client, run_id)
    assert after[0] == reports[0] and after[-1]["director_revision"]["remaining_patches"] == 0
    assert after[-1]["director_revision"]["working_storyboard"] == saved_source
    with client.app.state.session_factory() as db:
        limits = db.get(AgentRunLimit, run_id)
        assert (limits.deadline_at, limits.limits_json) == frozen
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_exhausted_question_can_cancel_after_deadline(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.application.model_gateway import utc

    install_revision(model_service, monkeypatch, settings, mutate=missing)
    run_id, _ = start(client, episode, director_revision_rounds=0)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    monkeypatch.setattr(
        "arclight.application.agent_limits.utcnow", lambda: utc(deadline) + timedelta(seconds=1)
    )
    assert client.post(f"/api/v1/runs/{run_id}/cancel").status_code == 200
    assert checks(client, run_id)[-1]["director_revision"]["remaining_patches"] == 0


@pytest.mark.parametrize("crash_at", ["patch", "accept", "candidate"])
def test_crash_after_durable_receipt_replays_without_duplicate_model_calls(
    client, episode, settings, model_service, monkeypatch, crash_at
):
    from arclight.application import director_revision as revision
    from arclight.application.artifacts import ArtifactStore

    sent, errors, base_errors = install_revision(
        model_service, monkeypatch, settings, mutate=missing
    )
    run_id, _ = start(client, episode, director_revision_rounds=1)

    class WorkerDied(BaseException):
        pass

    target, name = (
        (ArtifactStore, "storyboard")
        if crash_at == "candidate"
        else (revision, "publication_content" if crash_at == "accept" else "merge_patch")
    )
    original = getattr(target, name)
    calls = []

    def die(*args, **kwargs):
        value = original(*args, **kwargs)
        calls.append(True)
        if crash_at != "patch" or len(calls) == 2:
            raise WorkerDied
        return value

    monkeypatch.setattr(target, name, die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    count = len(sent)
    monkeypatch.setattr(target, name, original)
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        run = run_once(restarted, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors, base_errors)
    assert len(sent) == count + (2 if crash_at == "patch" else 0)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


@pytest.mark.parametrize(
    "change", ["disabled", "archived", "revoked", "cancelled", "input", "stale"]
)
def test_authority_and_exact_input_rechecked_during_revision_http(
    client, settings, model_service, monkeypatch, change
):
    owner = register(client, "checked-director-owner@example.test")
    project = create_project(client)
    episode = create_episode(client, project)
    targets, sources, changed = [], [], []

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"])["data"]
        if seed["phase"] != "CHECK_DIRECTOR" or changed:
            return
        changed.append(True)
        with client.app.state.session_factory.begin() as db:
            run = db.get(WorkflowRun, targets[0])
            if change == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif change == "archived":
                db.get(Project, UUID(project["id"])).archived = True
            elif change == "revoked":
                db.get(Project, UUID(project["id"])).owner_id = "service-owner"
            elif change == "cancelled":
                run.status = "CANCELLED"
            elif change == "input":
                run.input_json = run.input_json | {"target_duration_ms": 60000}
            else:
                db.get(AssetVersion, UUID(sources[0]["id"])).stale = True

    install_revision(model_service, monkeypatch, settings, on_http=on_http)
    run_id, source = start(client, episode, director_revision_rounds=1)
    targets.append(run_id)
    sources.append(source)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    with client.app.state.session_factory() as db:
        assert db.get(WorkflowRun, run_id).status in {"FAILED", "CANCELLED"}
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(Script, UUID(source["id"])).status == "APPROVED"


def test_shared_model_limit_cannot_reset_on_retry(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors, base_errors = install_revision(model_service, monkeypatch, settings)
    run_id, _ = start(
        client, episode, director_revision_rounds=1, agent_limits={"max_model_calls": 5}
    )
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT", (
        run,
        errors,
        base_errors,
    )
    count = len(sent)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["status"] == "FAILED" and len(sent) == count


def test_registered_iteration_definition_changes_block_resume_before_more_http(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.domain import director_revision

    sent, _, _ = install_revision(model_service, monkeypatch, settings, mutate=missing)
    run_id, _ = start(client, episode, director_revision_rounds=0)
    run_once(client, settings, run_id)
    waiting = checks(client, run_id)[-1]
    monkeypatch.setattr(
        director_revision, "REVISION_PROMPT", director_revision.REVISION_PROMPT + " changed"
    )
    count = len(sent)
    endpoint = f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers"
    response = client.post(endpoint, json=answer_body(waiting))
    if response.status_code == 202:
        run = run_once(client, settings, run_id)
        assert run["status"] == "FAILED" and run["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE"
    else:
        assert response.status_code == 409
    assert len(sent) == count


def test_postgres_duplicate_workers_are_fenced_and_http_holds_no_run_lock(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    entered, release = Event(), Event()
    once = []

    def on_http(payload):
        seed = json.loads(payload["messages"][1]["content"])["data"]
        if seed["phase"] == "CHECK_DIRECTOR" and not once:
            once.append(True)
            entered.set()
            assert release.wait(20)

    _, errors, base_errors = install_revision(model_service, monkeypatch, settings, on_http=on_http)
    run_id, _ = start(client, episode, director_revision_rounds=1)
    factory = client.app.state.session_factory
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(execute_run, factory, settings, run_id, 0)
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
        first.result(timeout=30)
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW", (
        errors,
        base_errors,
    )
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


def test_postgres_parallel_human_answers_enqueue_once(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    from arclight.application.screenwriter_progress import queue_answer
    from arclight.domain.agent_runtime import AgentHumanDecision

    install_revision(model_service, monkeypatch, settings, mutate=missing)
    run_id, _ = start(client, episode, director_revision_rounds=0)
    run_once(client, settings, run_id)
    waiting = checks(client, run_id)[-1]
    decision = AgentHumanDecision.model_validate(answer_body(waiting))
    factory = client.app.state.session_factory

    def submit(_):
        with factory.begin() as db:
            return queue_answer(
                db,
                run_id,
                UUID(waiting["loop_id"]),
                "service-owner",
                decision,
                agent_name="director",
            ).revision

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert list(pool.map(submit, range(2))) == [1, 1]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == 2
        )
