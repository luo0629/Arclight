"""Exercise specialist delegation failures and durable recovery through the real workflow."""

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, timedelta
from threading import Event
from uuid import UUID, uuid4

import httpx
import pytest
from art_direction_support import reference_set
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_art_direction import facts
from test_assets import path as asset_path
from test_assets import rename
from test_concurrency import require_postgres
from test_director_collaboration import begin, install_collaboration, opinion_operations
from test_director_creation import progress
from test_director_revision import answer_body
from test_screenwriter_creation import approve, run_once

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.agent_limits import delegation_count
from arclight.application.runtime import execute_run
from arclight.application.screenwriter_execution import ScreenwriterExecutionDeferred
from arclight.domain.director_collaboration import OBSERVE_OPINION, REQUEST_OPINION
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopDecision
from arclight.persistence.model_gateway import GatewayReceipt
from arclight.persistence.models import (
    AgentToolGrant,
    AgentToolInvocation,
    AssetVersion,
    OutboxEvent,
    Project,
    Script,
    Storyboard,
    WorkflowRun,
)


@pytest.mark.parametrize("choice", ["ADOPT", "DECLINE"])
def test_human_can_refuse_opinion_but_director_cannot_adopt_refusal(
    client, episode, settings, model_service, monkeypatch, choice
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, art_human=True, decision=choice
    )
    run_id, _ = begin(client, episode)
    before = facts(client)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    waiting = opinion_operations(client, run_id)[0]
    response = client.post(
        f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
        json=answer_body(waiting, decision="STOP"),
    )
    assert response.status_code == 202, response.text
    count = len(art_sent)
    run = run_once(client, settings, run_id)
    assert not any(errors), errors
    assert len(art_sent) == count
    if choice == "ADOPT":
        assert run["status"] == "FAILED" and run["error_code"] == "ART_OPINION_UNVERIFIED", run
        assert facts(client) == before
    else:
        assert run["status"] == "WAITING_REVIEW", run
        checks = [
            op for op in progress(client, run_id)["operations"] if op["kind"] == "director_check"
        ]
        observed = next(
            item["feedback"]["result"]
            for item in checks[0]["observations"]
            if item["tool"] == OBSERVE_OPINION
        )
        assert observed["status"] == "REFUSED" and observed["stop_code"] == "AGENT_HUMAN_STOPPED"
        assert checks[0]["result"]["opinion_decision"]["decision"] == "DECLINE"


def test_missing_evidence_stays_human_and_answer_cannot_invent_references(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode, count=0)
    _, art_sent, *errors = install_collaboration(model_service, monkeypatch, settings)
    run_id, _ = begin(client, episode)
    before = facts(client)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    waiting = opinion_operations(client, run_id)[0]
    assert waiting["human_request"]["reason"] == "MISSING_EVIDENCE"
    response = client.post(
        f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
        json=answer_body(waiting) | {"answer": waiting["human_request"]["options"][0]},
    )
    assert response.status_code == 202, response.text
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    current = opinion_operations(client, run_id)[0]
    assert current["loop_id"] == waiting["loop_id"]
    assert current["question_id"] != waiting["question_id"]
    assert current["status"] == "WAITING_HUMAN" and len(art_sent) == 3
    assert facts(client) == before and not any(errors), errors


def test_external_scan_preserves_art_question_and_answer_resumes_original_run(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    director_sent, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, art_human=True
    )
    run_id, _ = begin(client, episode)
    run = run_once(client, settings, run_id)
    assert run["status"] == "RUNNING"
    waiting = opinion_operations(client, run_id)[0]
    assert waiting["status"] == "WAITING_HUMAN"
    original_calls = len(director_sent), len(art_sent)
    factory = client.app.state.session_factory
    with factory() as db:
        outbox_count = db.scalar(
            select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
        )
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        restarted.cookies.update(client.cookies)
        for _ in range(3):
            assert queue_ready_external(restarted.app.state.session_factory, settings) == 0
        current = restarted.get(f"/api/v1/runs/{run_id}").json()
        assert (current["status"], current["revision"]) == ("RUNNING", run["revision"])
        assert opinion_operations(restarted, run_id)[0] == waiting
        assert (len(director_sent), len(art_sent)) == original_calls
        with factory() as db:
            assert (
                db.scalar(
                    select(func.count())
                    .select_from(OutboxEvent)
                    .where(OutboxEvent.run_id == run_id)
                )
                == outbox_count
            )
        endpoint = f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers"
        body = answer_body(waiting) | {"answer": waiting["human_request"]["options"][0]}
        accepted = restarted.post(endpoint, json=body)
        assert accepted.status_code == 202, accepted.text
        repeated = restarted.post(endpoint, json=body)
        assert repeated.status_code == 202 and repeated.json() == accepted.json()
        assert queue_ready_external(restarted.app.state.session_factory, settings) == 0
        result = run_once(restarted, settings, run_id)
        assert result["status"] == "WAITING_REVIEW", (result, errors)
        completed = opinion_operations(restarted, run_id)
        assert len(completed) == 1
        assert completed[0]["loop_id"] == waiting["loop_id"]
        assert completed[0]["status"] == "COMPLETED"
    assert not any(errors), errors
    with factory() as db:
        assert delegation_count(db, run_id) == 1
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert (
            db.scalar(
                select(func.count()).select_from(OutboxEvent).where(OutboxEvent.run_id == run_id)
            )
            == outbox_count + 1
        )


@pytest.mark.parametrize("mutation", ["revoked", "deadline"])
def test_external_opinion_scan_routes_invalid_authority_to_executor_without_model_io(
    client, episode, settings, model_service, monkeypatch, mutation
):
    reference_set(client, episode)
    director_sent, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, art_human=True
    )
    run_id, _ = begin(client, episode)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    original_calls = len(director_sent), len(art_sent)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        if mutation == "revoked":
            db.get(Project, UUID(episode["project_id"])).owner_id = "other-owner"
        else:
            deadline = db.get(AgentRunLimit, run_id).deadline_at
    if mutation == "deadline":
        monkeypatch.setattr(
            "arclight.application.agent_limits.utcnow",
            lambda: deadline.replace(tzinfo=UTC) + timedelta(seconds=1),
        )
    assert queue_ready_external(factory, settings) == 1
    assert queue_ready_external(factory, settings) == 0
    with factory() as db:
        revision = db.get(WorkflowRun, run_id).revision
    execute_run(factory, settings, run_id, revision)
    with factory() as db:
        assert db.get(WorkflowRun, run_id).status == "FAILED"
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 0
    assert (len(director_sent), len(art_sent)) == original_calls
    assert not any(errors), errors


def test_unknown_art_http_result_keeps_receipt_and_never_replays_blindly(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)

    def timeout(_):
        raise httpx.ReadTimeout("opinion result unknown")

    _, art_sent, *_ = install_collaboration(model_service, monkeypatch, settings, on_art=timeout)
    run_id, _ = begin(client, episode)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "GATEWAY_RECONCILIATION_REQUIRED", run
    assert len(art_sent) == 1 and facts(client) == before
    loop = opinion_operations(client, run_id)[0]
    assert loop["status"] == "RECOVERY_REQUIRED"
    with client.app.state.session_factory() as db:
        receipt = db.scalar(select(GatewayReceipt).where(GatewayReceipt.status == "RESULT_UNKNOWN"))
        assert receipt is not None and receipt.actual_cost is None
        assert delegation_count(db, run_id) == 1
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    retried = run_once(client, settings, run_id)
    assert (
        retried["status"] == "FAILED" and retried["error_code"] == "GATEWAY_RECONCILIATION_REQUIRED"
    ), retried
    assert len(art_sent) == 1 and facts(client) == before


def test_root_model_limit_counts_director_and_art_and_cannot_reset_on_retry(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(model_service, monkeypatch, settings)
    run_id, _ = begin(client, episode, agent_limits={"max_model_calls": 10})
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_MODEL_CALL_LIMIT", (
        run,
        errors,
    )
    assert 0 < len(art_sent) < 4 and facts(client) == before
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        count = db.scalar(select(func.count()).select_from(GatewayReceipt))
        assert count == 10
    calls = len(art_sent)
    assert client.post(f"/api/v1/runs/{run_id}/retry").status_code == 202
    assert run_once(client, settings, run_id)["error_code"] == "AGENT_MODEL_CALL_LIMIT"
    assert len(art_sent) == calls


def test_root_deadline_expiring_during_art_io_cannot_be_reset(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    targets = []

    def expire(_):
        from arclight.application import agent_limits
        from arclight.application.model_gateway import utc

        with client.app.state.session_factory() as db:
            deadline = utc(db.get(AgentRunLimit, targets[0]).deadline_at)
        monkeypatch.setattr(agent_limits, "utcnow", lambda: deadline + timedelta(seconds=1))

    _, art_sent, *_ = install_collaboration(model_service, monkeypatch, settings, on_art=expire)
    run_id, _ = begin(client, episode)
    targets.append(run_id)
    before = facts(client)
    run = run_once(client, settings, run_id)
    assert run["status"] == "FAILED" and run["error_code"] == "AGENT_DEADLINE_EXCEEDED", run
    assert len(art_sent) == 1 and facts(client) == before


@pytest.mark.parametrize("phase", ["art", "director"])
@pytest.mark.parametrize(
    "change", ["reference", "script", "archived", "baseline", "grant", "revoked"]
)
def test_current_authority_and_versions_rechecked_after_opinion_and_before_decision(
    client, episode, settings, model_service, monkeypatch, phase, change
):
    asset, versions = reference_set(client, episode)
    targets, sources, changed = [], [], []

    def mutate(payload):
        trigger = "compare_art_options" if phase == "art" else OBSERVE_OPINION
        if changed or not any(item["tool_name"] == trigger for item in tool_feedback(payload)):
            return
        changed.append(True)
        if change == "baseline":
            current = client.get(asset_path(episode["project_id"]) + "/" + asset["id"]).json()
            assert rename(client, current, "人工更新的雨夜角色").status_code == 200
            return
        with client.app.state.session_factory.begin() as db:
            if change == "reference":
                db.get(AssetVersion, UUID(versions[0]["id"])).stale = True
            elif change == "script":
                db.get(AssetVersion, UUID(sources[0]["id"])).stale = True
            elif change == "grant":
                grant = db.scalar(
                    select(AgentToolGrant).where(
                        AgentToolGrant.run_id == targets[0],
                        AgentToolGrant.agent_name == "art_director",
                    )
                )
                grant.allowed_tools_json = []
            elif change == "revoked":
                db.get(Project, UUID(episode["project_id"])).owner_id = "other-owner"
            else:
                db.get(Project, UUID(episode["project_id"])).archived = True

    install_collaboration(
        model_service,
        monkeypatch,
        settings,
        on_art=mutate if phase == "art" else None,
        on_director=mutate if phase == "director" else None,
        art_options={"existing": True},
        mutate_request=lambda value, _: value["task"].update(target_asset_ids=[asset["id"]]),
    )
    run_id, source = begin(client, episode)
    targets.append(run_id)
    sources.append(source)
    execute_run(client.app.state.session_factory, settings, run_id, 0)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert changed and run.status == "FAILED", run.error_code
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.get(Script, UUID(source["id"])).status == "APPROVED"
    if change == "revoked":
        assert client.get(f"/api/v1/runs/{run_id}").status_code == 404


@pytest.mark.parametrize("crash_at", ["request", "art_complete"])
def test_worker_crash_preserves_original_delegation_and_committed_art_calls(
    client, episode, settings, model_service, monkeypatch, crash_at
):
    from arclight.application import art_direction, director_collaboration

    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(model_service, monkeypatch, settings)
    run_id, _ = begin(client, episode)

    class WorkerDied(BaseException):
        pass

    target, name = (
        (director_collaboration, "drive_opinion")
        if crash_at == "request"
        else (art_direction, "propose_art")
    )
    original = getattr(target, name)

    def die(*args, **kwargs):
        if crash_at == "request" and args[3].status != "WAITING_EXTERNAL":
            return original(*args, **kwargs)
        if crash_at == "art_complete":
            result = original(*args, **kwargs)
            assert result.status == "COMPLETED"
        raise WorkerDied

    monkeypatch.setattr(target, name, die)
    with pytest.raises(WorkerDied):
        run_once(client, settings, run_id)
    before = progress(client, run_id)["art_opinion_request"]
    count = len(art_sent)
    assert count == (0 if crash_at == "request" else 4)
    monkeypatch.setattr(target, name, original)
    assert queue_ready_external(client.app.state.session_factory, settings) == (
        1 if crash_at == "art_complete" else 0
    )
    assert queue_ready_external(client.app.state.session_factory, settings) == 0
    with TestClient(create_app(settings)) as restarted:
        restarted.headers.update(client.headers)
        run = run_once(restarted, settings, run_id)
        assert run["status"] == "WAITING_REVIEW", (run, errors)
        assert progress(restarted, run_id)["art_opinion_request"] == before
    assert len(art_sent) == 4
    with client.app.state.session_factory() as db:
        assert delegation_count(db, run_id) == 1
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1
        receipts = list(
            db.scalars(
                select(AgentToolInvocation).where(AgentToolInvocation.tool_name == REQUEST_OPINION)
            )
        )
        assert len(receipts) == 1 and receipts[0].status == "SUCCEEDED"


def test_changed_registered_collaboration_definition_blocks_resume_before_io(
    client, episode, settings, model_service, monkeypatch
):
    from arclight.domain import director_collaboration

    reference_set(client, episode)
    _, art_sent, *_ = install_collaboration(model_service, monkeypatch, settings, art_human=True)
    run_id, _ = begin(client, episode)
    run_once(client, settings, run_id)
    waiting = opinion_operations(client, run_id)[0]
    monkeypatch.setattr(
        director_collaboration, "OPINION_PROMPT", director_collaboration.OPINION_PROMPT + " changed"
    )
    count = len(art_sent)
    response = client.post(
        f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
        json=answer_body(waiting) | {"answer": waiting["human_request"]["options"][0]},
    )
    if response.status_code == 202:
        run = run_once(client, settings, run_id)
        assert (
            run["status"] == "FAILED" and run["error_code"] == "PRODUCTION_VERSION_INCOMPATIBLE"
        ), run
    else:
        assert response.status_code == 409, response.text
    assert len(art_sent) == count


@pytest.mark.parametrize(
    "revised,consult,version",
    [
        (False, True, "production/5.0"),
        (True, True, "production/6.0"),
        (False, False, "production/5.0"),
    ],
)
def test_both_combined_workflows_keep_script_and_storyboard_reviews(
    client, episode, settings, model_service, monkeypatch, revised, consult, version
):
    reference_set(client, episode)
    _, art_sent, *errors = install_collaboration(
        model_service, monkeypatch, settings, revised=revised, consult=consult
    )
    response = client.post(
        f"/api/v1/episodes/{episode['id']}/script-runs",
        headers={"Idempotency-Key": uuid4().hex},
        json={
            "story": "雨夜寻找纸船失主",
            "professional": True,
            "professional_director": True,
            "director_revision_rounds": 2,
            "director_art_collaboration": True,
            **({"revision_rounds": 1} if revised else {}),
        },
    )
    assert response.status_code == 202, response.text
    run_id = UUID(response.json()["run_id"])
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW" and run["workflow_version"] == version, (run, errors)
    assert not progress(client, run_id)["operations"] and not art_sent
    approve(client, run)
    run = run_once(client, settings, run_id)
    assert run["status"] == "WAITING_REVIEW", (run, errors)
    assert len(opinion_operations(client, run_id)) == int(consult)
    assert len(art_sent) == (4 if consult else 0)
    approve(client, run)
    assert run_once(client, settings, run_id)["status"] == "SUCCEEDED"


def test_postgres_duplicate_workers_are_fenced_during_art_http_without_long_run_lock(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    reference_set(client, episode)
    entered, release, blocked = Event(), Event(), []

    def block(_):
        if not blocked:
            blocked.append(True)
            entered.set()
            assert release.wait(20)

    install_collaboration(model_service, monkeypatch, settings, on_art=block)
    run_id, _ = begin(client, episode)
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
    assert client.get(f"/api/v1/runs/{run_id}").json()["status"] == "WAITING_REVIEW"
    with factory() as db:
        assert delegation_count(db, run_id) == 1
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 1


def test_postgres_parallel_opinion_answers_resume_original_question_once(
    client, episode, settings, model_service, monkeypatch
):
    require_postgres(settings)
    from arclight.application.agent_workflows import queue_answer
    from arclight.domain.agent_runtime import AgentHumanDecision

    reference_set(client, episode)
    install_collaboration(model_service, monkeypatch, settings, art_human=True)
    run_id, _ = begin(client, episode)
    run_once(client, settings, run_id)
    waiting = opinion_operations(client, run_id)[0]
    decision = AgentHumanDecision.model_validate(
        answer_body(waiting) | {"answer": waiting["human_request"]["options"][0]}
    )
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
    assert run_once(client, settings, run_id)["status"] == "WAITING_REVIEW"


def test_waiting_opinion_can_cancel_without_more_model_io(
    client, episode, settings, model_service, monkeypatch
):
    reference_set(client, episode)
    _, art_sent, *_ = install_collaboration(model_service, monkeypatch, settings, art_human=True)
    run_id, _ = begin(client, episode)
    before = facts(client)
    assert run_once(client, settings, run_id)["status"] == "RUNNING"
    waiting = opinion_operations(client, run_id)[0]
    count = len(art_sent)
    response = client.post(f"/api/v1/runs/{run_id}/cancel")
    assert response.status_code == 200 and response.json()["status"] == "CANCELLED", response.text
    assert run_once(client, settings, run_id)["status"] == "CANCELLED"
    assert len(art_sent) == count and facts(client) == before
    answer = client.post(
        f"/api/v1/runs/{run_id}/director/{waiting['loop_id']}/answers",
        json=answer_body(waiting) | {"answer": waiting["human_request"]["options"][0]},
    )
    assert answer.status_code == 409 and len(art_sent) == count
