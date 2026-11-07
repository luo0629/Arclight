"""Durable art decisions reuse the original limits, grants and immutable history."""

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID

import pytest
from art_direction_support import install_direction_model, prepare_direction, reference_set
from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_agent_runtime import tool_feedback
from test_art_direction import facts
from test_assets import path as asset_path
from test_assets import rename
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres

from arclight.application import agent_loop_recovery as recovery
from arclight.application.art_direction import propose_art, resume_art_direction
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import AgentToolGrant, AssetVersion, Project, User, WorkflowRun


def test_human_pause_restart_and_idempotent_answer_preserve_original_art_loop(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    factory = client.app.state.session_factory
    sent, errors, _ = install_direction_model(model_service, human=True)
    waiting = propose_art(factory, settings, run_id, actor, task)
    assert waiting.status == "HUMAN_REVIEW", waiting.model_dump_json()
    assert len(sent) == 2
    assert propose_art(factory, settings, run_id, actor, task) == waiting
    assert resume_art_direction(factory, settings, waiting.loop_id, actor) == waiting
    assert len(sent) == 2
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        original = (control.deadline_at, control.limits_json, control.root_run_id)
    decision = answer(waiting)
    finished = resume_art_direction(factory, settings, waiting.loop_id, actor, decision=decision)
    assert not errors, errors
    assert finished.status == "COMPLETED" and len(sent) == 5, finished.model_dump_json()
    assert finished.loop_id == waiting.loop_id and finished.input_sha256 == waiting.input_sha256
    assert (
        resume_art_direction(factory, settings, waiting.loop_id, actor, decision=decision)
        == finished
    )
    assert len(sent) == 5
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        assert (control.deadline_at, control.limits_json, control.root_run_id) == original
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 1


@pytest.mark.parametrize("decision", ["ANSWER", "STOP"])
def test_human_response_cannot_replace_missing_approved_image_evidence(
    client, episode, settings, model_service, decision
):
    reference_set(client, episode, count=0)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    factory = client.app.state.session_factory
    waiting = propose_art(factory, settings, run_id, actor, task)
    before = facts(client)
    if decision == "ANSWER":
        with pytest.raises(DomainError) as invalid:
            resume_art_direction(
                factory,
                settings,
                waiting.loop_id,
                actor,
                decision=answer(waiting, answer="直接批准"),
            )
        assert invalid.value.code == "AGENT_DECISION_INVALID"
        assert len(sent) == 2
    finished = resume_art_direction(
        factory,
        settings,
        waiting.loop_id,
        actor,
        decision=answer(waiting, decision=decision, answer=None if decision == "STOP" else "补充"),
    )
    assert not errors, errors
    if decision == "STOP":
        assert finished.status == "STOPPED" and finished.stop_code == "AGENT_HUMAN_STOPPED"
        assert len(sent) == 2
    else:
        assert finished.status == "HUMAN_REVIEW" and finished.question_id != waiting.question_id
        assert len(sent) == 3
    assert facts(client) == before


@pytest.mark.parametrize(
    "patch,code",
    [
        ({"max_model_calls": 1}, "AGENT_MODEL_CALL_LIMIT"),
        ({"max_tool_calls": 0}, "AGENT_TOOL_CALL_LIMIT"),
        ({"max_steps": 0}, "AGENT_STEP_LIMIT"),
        ({"max_calls": 1}, "AGENT_CALL_LIMIT"),
    ],
)
def test_art_loop_exhaustion_obeys_shared_root_limits_without_formal_writes(
    client, episode, settings, model_service, patch, code
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    before = facts(client)
    result = propose_art(
        client.app.state.session_factory,
        settings,
        run_id,
        actor,
        task,
        limits=AgentRunLimits(**patch),
    )
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == code, result.model_dump_json()
    assert len(sent) <= 1
    assert facts(client) == before


@pytest.mark.parametrize("node", ["decide", "tools"])
def test_crash_reentry_reuses_committed_model_and_tool_actions(
    client, episode, settings, model_service, monkeypatch, node
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service)
    factory = client.app.state.session_factory
    original = getattr(recovery._PersistentRuntime, node)
    crashed = []

    def crash_once(*args, **kwargs):
        result = original(*args, **kwargs)
        if not crashed:
            crashed.append(True)
            raise RuntimeError("simulated art process interruption")
        return result

    monkeypatch.setattr(recovery._PersistentRuntime, node, crash_once)
    with pytest.raises(RuntimeError, match="simulated art"):
        propose_art(factory, settings, run_id, actor, task)
    with factory() as db:
        loop_id = db.scalar(select(AgentLoop.id).where(AgentLoop.run_id == run_id))
    result = resume_art_direction(factory, settings, loop_id, actor)
    assert not errors, errors
    assert result.status == "COMPLETED" and len(sent) == 4, result.model_dump_json()
    assert propose_art(factory, settings, run_id, actor, task) == result
    assert len(sent) == 4
    with factory() as db:
        actions = list(
            db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop_id))
        )
        assert len(actions) == 9 and all(action.result_json is not None for action in actions)
        # Valid query names in trusted JSON Schemas are encoded; runtime data still uses
        # the original credential guard and the model sees the full real declaration.
        declaration = next(
            tool
            for action in actions
            if action.kind == "MODEL"
            for tool in action.request_json["tools"]
            if tool["name"] == "investigate_art"
        )
        assert isinstance(declaration["parameters"], str)
        assert (
            "authorization"
            in json.loads(declaration["parameters"])["properties"]["query"]["discriminator"][
                "mapping"
            ]
        )


def test_reentry_rejects_changed_task_and_database_rejects_frozen_seed_mutation(
    client, episode, settings, model_service
):
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    sent, errors, _ = install_direction_model(model_service, human=True)
    factory = client.app.state.session_factory
    waiting = propose_art(factory, settings, run_id, actor, task)
    with pytest.raises(DomainError) as conflict:
        propose_art(
            factory, settings, run_id, actor, task.model_copy(update={"visual_goal": "换任务"})
        )
    assert conflict.value.code == "ART_TASK_CONFLICT"
    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        loop = db.get(AgentLoop, waiting.loop_id)
        original_seed = loop.seed_json
        loop.seed_json = loop.seed_json | {"operation_key": "another-art-task"}
    with factory() as db:
        assert db.get(AgentLoop, waiting.loop_id).seed_json == original_seed
    assert not errors and len(sent) == 2


@pytest.mark.parametrize("change", ["disabled", "archived", "cancelled", "stale", "grant"])
def test_art_final_rechecks_authority_and_usability_after_model_io(
    client, settings, model_service, change
):
    owner = register(client)
    episode = create_episode(client, create_project(client))
    _, versions = reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    changed = []
    factory = client.app.state.session_factory

    def mutate(payload):
        if (
            not any(f["tool_name"] == "compare_art_options" for f in tool_feedback(payload))
            or changed
        ):
            return
        changed.append(True)
        with factory.begin() as db:
            if change == "disabled":
                db.get(User, UUID(owner["user"]["id"])).disabled = True
            elif change == "archived":
                db.get(Project, UUID(episode["project_id"])).archived = True
            elif change == "cancelled":
                db.get(WorkflowRun, run_id).status = "CANCELLED"
            elif change == "grant":
                grant = db.scalar(
                    select(AgentToolGrant).where(
                        AgentToolGrant.run_id == run_id, AgentToolGrant.agent_name == "art_director"
                    )
                )
                grant.allowed_tools_json = []
            else:
                db.get(AssetVersion, UUID(versions[0]["id"])).stale = True

    _, errors, _ = install_direction_model(model_service, on_http=mutate)
    result = propose_art(factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "STOPPED" and result.result is None, result.model_dump_json()
    assert result.stop_code in {
        "FORBIDDEN",
        "PROJECT_ARCHIVED",
        "AGENT_CANCELLED",
        "USER_NOT_AUTHORIZED",
        "ART_EVIDENCE_REQUIRED",
    }


def test_changed_directory_baseline_after_comparison_cannot_be_delivered(
    client, episode, settings, model_service
):
    asset, _ = reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings, target_ids=[asset["id"]])
    changed = []

    def mutate(payload):
        if (
            any(f["tool_name"] == "compare_art_options" for f in tool_feedback(payload))
            and not changed
        ):
            changed.append(True)
            current = client.get(asset_path(episode["project_id"]) + "/" + asset["id"]).json()
            assert rename(client, current, "已被人工修改").status_code == 200

    _, errors, _ = install_direction_model(model_service, existing=True, on_http=mutate)
    result = propose_art(client.app.state.session_factory, settings, run_id, actor, task)
    assert not errors, errors
    assert result.status == "STOPPED" and result.stop_code == "ART_PROPOSAL_BASE_STALE", (
        result.model_dump_json()
    )


def test_postgres_duplicate_art_loops_are_fenced_without_locking_during_model_io(
    client, episode, settings, model_service
):
    require_postgres(settings)
    reference_set(client, episode)
    run_id, actor, _, task = prepare_direction(client, episode, settings)
    factory = client.app.state.session_factory
    entered, release = Event(), Event()

    def pause(payload):
        if not tool_feedback(payload):
            entered.set()
            assert release.wait(25)

    sent, errors, _ = install_direction_model(model_service, on_http=pause)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(propose_art, factory, settings, run_id, actor, task)
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
            duplicate = pool.submit(propose_art, factory, settings, run_id, actor, task)
            with pytest.raises(DomainError) as busy:
                duplicate.result(timeout=10)
            assert busy.value.code == "AGENT_LOOP_BUSY"
        finally:
            release.set()
        result = pending.result(timeout=30)
    assert result.status == "COMPLETED", (result.model_dump_json(), errors)
    assert not errors and len(sent) == 4
