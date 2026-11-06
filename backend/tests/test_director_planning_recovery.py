"""Recovery, mutable authority and shared limits around actual director model I/O."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_director_planning import install_model, prepare

from arclight.application import director_planning as planning
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction, AgentLoopQuestion
from arclight.persistence.models import AssetVersion, Project, Storyboard, User, WorkflowRun


@pytest.mark.parametrize("decision", ["ANSWER", "STOP"])
def test_saved_question_restarts_same_loop_and_exact_decision_replays(
    client, episode, settings, model_service, decision
):
    run_id, actor, _ = prepare(client, episode, settings)
    sent, errors = install_model(model_service, human=True)
    factory = client.app.state.session_factory
    waiting = planning.plan_director(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", (waiting.model_dump_json(), errors)
    with factory() as db:
        original_limit = db.get(AgentRunLimit, run_id).deadline_at
        thread = db.get(AgentLoop, waiting.loop_id).thread_id
    settings.text_duration_tolerance_percent = 0
    with TestClient(create_app(settings)) as restarted:
        fresh = restarted.app.state.session_factory
        assert planning.resume_director_plan(fresh, settings, waiting.loop_id, actor) == waiting
        assert len(sent) == 2
        choice = answer(waiting, decision=decision, answer="补充" if decision == "ANSWER" else None)
        outcome = planning.resume_director_plan(
            fresh, settings, waiting.loop_id, actor, decision=choice
        )
        assert outcome.status == ("COMPLETED" if decision == "ANSWER" else "STOPPED"), (
            outcome.model_dump_json()
        )
        assert (
            planning.resume_director_plan(fresh, settings, waiting.loop_id, actor, decision=choice)
            == outcome
        )
    with factory() as db:
        assert db.get(AgentLoop, waiting.loop_id).thread_id == thread
        assert db.get(AgentRunLimit, run_id).deadline_at == original_limit
        assert db.scalar(select(func.count()).select_from(AgentLoopQuestion)) == 1
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
    assert len(sent) == (3 if decision == "ANSWER" else 2)


@pytest.mark.parametrize("change", ["disabled", "archived", "cancelled", "input", "stale"])
def test_authority_exact_input_and_usability_rechecked_after_model_io(
    client, settings, model_service, change
):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    run_id, actor, source = prepare(client, episode, settings)
    factory = client.app.state.session_factory
    changed = []
    immutable_rejections = []

    def mutate(payload):
        if not tool_feedback(payload) or changed:
            return
        changed.append(True)
        if change == "input":
            with pytest.raises(IntegrityError) as rejected, factory.begin() as db:
                run = db.get(WorkflowRun, run_id)
                run.input_json = run.input_json | {"target_duration_ms": 60000}
            assert "IMMUTABLE_HISTORY" in str(rejected.value)
            immutable_rejections.append(True)
            return
        with factory.begin() as db:
            if change == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif change == "archived":
                db.get(Project, UUID(episode["project_id"])).archived = True
            elif change == "cancelled":
                db.get(WorkflowRun, run_id).status = "CANCELLED"
            else:
                db.get(AssetVersion, UUID(source["id"])).stale = True

    _, errors = install_model(model_service, on_http=mutate)
    outcome = planning.plan_director(factory, settings, run_id, actor)
    assert not errors
    if change == "input":
        assert immutable_rejections == [True]
        assert outcome.status == "COMPLETED"
        assert outcome.result.target_duration_ms == episode["target_duration_ms"]
        with factory() as db:
            assert (
                db.get(WorkflowRun, run_id).input_json["target_duration_ms"]
                == episode["target_duration_ms"]
            )
        return
    assert outcome.status == "STOPPED", (outcome.model_dump_json(), errors)
    assert outcome.result is None
    assert outcome.stop_code in {
        "FORBIDDEN",
        "PROJECT_ARCHIVED",
        "AGENT_CANCELLED",
        "AGENT_INPUT_CHANGED",
        "CONTEXT_INPUT_NOT_USABLE",
    }, (outcome.model_dump_json(), errors)
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0


def test_root_call_limit_is_not_reset_by_explicit_plan_retry(
    client, episode, settings, model_service
):
    run_id, actor, _ = prepare(client, episode, settings)
    sent, errors = install_model(model_service)
    factory = client.app.state.session_factory
    limits = AgentRunLimits(max_calls=4)
    first = planning.plan_director(factory, settings, run_id, actor, limits=limits)
    assert first.status == "STOPPED" and first.stop_code == "AGENT_CALL_LIMIT", (
        first.model_dump_json(),
        errors,
    )
    assert len(sent) == 1
    with factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
        original = db.get(AgentLoop, first.loop_id).outcome_json
    retry = planning.plan_director(factory, settings, run_id, actor, attempt=1)
    assert retry.status == "STOPPED" and retry.stop_code == "AGENT_CALL_LIMIT"
    assert len(sent) == 1
    with factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline
        assert db.get(AgentLoop, first.loop_id).outcome_json == original


def test_postgres_duplicate_plans_are_fenced_and_model_io_does_not_hold_run_lock(
    client, episode, settings, model_service
):
    require_postgres(settings)
    run_id, actor, _ = prepare(client, episode, settings)
    factory = client.app.state.session_factory
    entered, release = Event(), Event()

    def pause(payload):
        if not tool_feedback(payload):
            entered.set()
            assert release.wait(25)

    sent, errors = install_model(model_service, on_http=pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(planning.plan_director, factory, settings, run_id, actor)
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
            duplicate = pool.submit(planning.plan_director, factory, settings, run_id, actor)
            with pytest.raises(DomainError) as raised:
                duplicate.result(timeout=10)
            assert raised.value.code == "AGENT_LOOP_BUSY"
            different = pool.submit(
                planning.plan_director, factory, settings, run_id, actor, attempt=1
            )
            with pytest.raises(DomainError) as conflict:
                different.result(timeout=10)
            assert conflict.value.code == "DIRECTOR_PLAN_CONFLICT"
        finally:
            release.set()
        outcome = pending.result(timeout=30)
    assert outcome.status == "COMPLETED", (outcome.model_dump_json(), errors)
    assert len(sent) == 2
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 1
        assert db.scalar(select(func.count()).select_from(AgentLoopAction)) == 5
