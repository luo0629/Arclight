"""Producer plans use the real journal and gateway; only model HTTP is replaced."""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient
from test_agent_loop_recovery import answer
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback

from arclight.application import producer_planning as planning
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentAction, AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopQuestion
from arclight.persistence.models import (
    AgentToolGrant,
    OutboxEvent,
    Project,
    Script,
    Storyboard,
    VersionBinding,
    WorkflowRun,
)


def writer_task(key="write", *, story="雨夜，主角决定寻找纸船的失主。", depends_on=()):
    return {
        "task_key": key,
        "depends_on": list(depends_on),
        "reason": "当前缺少本集可审核的剧本，先生成专业编剧候选。",
        "task": {"kind": "screenwriter", "story": story, "revision_rounds": 1},
    }


def begin_producer(
    client,
    episode,
    settings,
    *,
    goal="制作纸船故事的本集剧本",
    limits=None,
    key=None,
    quote="0",
    actor=None,
):
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=quote, pricing_version="prod02:1"
    )
    factory = client.app.state.session_factory
    with factory.begin() as db:
        actor = actor or db.get(Project, UUID(episode["project_id"])).owner_id
        run = planning.start_producer(
            db,
            settings,
            UUID(episode["id"]),
            actor,
            goal,
            key or uuid4().hex,
            "producer-test-" + uuid4().hex,
            limits=limits or AgentRunLimits(maximum_cost=Decimal("0"), max_duration_seconds=3600),
        )
        run_id = run.id
    return run_id, actor


def install_plan_model(
    model_service, *, tasks=None, patch=None, on_http=None, cost="0", human=False
):
    sent, errors = [], []

    def respond(request):
        try:
            payload = json.loads(request.content)
            sent.append(payload)
            if on_http:
                on_http(payload)
            seed = json.loads(payload["messages"][1]["content"])["data"]
            feedback = tool_feedback(payload)
            answered = any(
                item["role"] == "user" and "human_decision" in (item.get("content") or "")
                for item in payload["messages"]
            )
            if not feedback:
                value = call_response(
                    ("project-state", "investigate_project", {"query": {"path": "overview"}})
                )
            elif human and not answered:
                value = final("HUMAN_REVIEW")
            else:
                evidence = [
                    {
                        "tool_call_key": item["idempotency_key"],
                        "evidence_sha256": item["result"]["evidence_sha256"],
                    }
                    for item in feedback
                    if item["status"] == "SUCCEEDED"
                ]
                proposed = tasks(seed, feedback) if callable(tasks) else tasks
                result = {
                    "schema_version": "producer-plan/1",
                    "goal_sha256": seed["goal_sha256"],
                    "summary": seed["goal"],
                    "evidence": evidence,
                    "tasks": deepcopy(proposed or [writer_task()]),
                }
                if patch:
                    patch(result)
                value = final(result=result)
            value["usage"] = {"prompt_tokens": 10, "completion_tokens": 10, "actual_cost": cost}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    return sent, errors


def completed_plan(
    client,
    episode,
    settings,
    model_service,
    *,
    tasks=None,
    limits=None,
    goal=None,
    quote="0",
    cost="0",
    actor=None,
):
    sent, errors = install_plan_model(model_service, tasks=tasks, cost=cost)
    run_id, actor = begin_producer(
        client,
        episode,
        settings,
        limits=limits,
        quote=quote,
        actor=actor,
        **({"goal": goal} if goal else {}),
    )
    outcome = planning.plan_producer(client.app.state.session_factory, settings, run_id, actor)
    assert outcome.status == "COMPLETED", (outcome.model_dump_json(), errors)
    assert not errors, errors
    with client.app.state.session_factory() as db:
        reference = planning.read_producer_plan(db, outcome.loop_id, actor)
    return run_id, actor, outcome, reference, sent


def test_producer_start_has_frozen_scope_and_grants_without_dispatch(client, episode, settings):
    run_id, actor = begin_producer(client, episode, settings)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        control = db.get(AgentRunLimit, run_id)
        grant = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == run_id))
        assert run.workflow_type == "producer" and run.workflow_version == "producer/1"
        assert run.status == "RUNNING" and run.pending_review is None
        assert run.input_json["created_by"] == actor
        assert run.input_json["goal_sha256"] == digest({"goal": run.input_json["goal"]})
        assert [(item["step_id"], item["status"]) for item in run.agent_state_json["steps"]] == [
            ("producer", "RUNNING")
        ]
        assert control.root_run_id == run.id and control.parent_run_id is None
        assert set(grant.allowed_tools_json) == {"investigate_project", "delegate_production"}
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert db.scalar(select(func.count()).select_from(AgentAction)) == 0


def test_start_replay_is_identical_and_changed_goal_or_limits_conflicts(client, episode, settings):
    limits = AgentRunLimits(maximum_cost=Decimal("0"), max_duration_seconds=3600)
    run_id, actor = begin_producer(client, episode, settings, key="frozen-goal", limits=limits)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        original = db.get(WorkflowRun, run_id)
        same = planning.start_producer(
            db,
            settings,
            original.episode_id,
            actor,
            original.input_json["goal"],
            "frozen-goal",
            "retry",
            limits=limits,
        )
        assert same.id == run_id
    for goal, policy in [("另一个目标", limits), ("制作纸船故事的本集剧本", AgentRunLimits())]:
        with factory.begin() as db, pytest.raises(DomainError) as error:
            planning.start_producer(
                db,
                settings,
                UUID(episode["id"]),
                actor,
                goal,
                "frozen-goal",
                "changed",
                limits=policy,
            )
        assert error.value.code == "IDEMPOTENCY_CONFLICT"


def test_completed_plan_is_exact_version_and_replay_does_not_call_model(
    client, episode, settings, model_service
):
    run_id, actor, outcome, reference, sent = completed_plan(
        client, episode, settings, model_service
    )
    assert outcome.model_turns == 2 and len(sent) == 2
    assert reference.plan_id == outcome.loop_id and reference.version_no == 1
    assert reference.plan_sha256 == digest(outcome.result.model_dump(mode="json"))
    again = planning.plan_producer(client.app.state.session_factory, settings, run_id, actor)
    assert again == outcome and len(sent) == 2
    with client.app.state.session_factory() as db:
        binding = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.resource_id == outcome.loop_id,
            )
        )
        assert binding.snapshot_json["plan"] == outcome.result.model_dump(mode="json")
        assert db.get(AgentLoop, outcome.loop_id).status == "COMPLETED"
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert db.scalar(select(func.count()).select_from(Script)) == 0
        assert db.scalar(select(func.count()).select_from(Storyboard)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("goal", ["先写纸船失主的动机", "先写暴雨中的行动"])
def test_goal_and_actual_observation_drive_dynamic_task_parameters(
    client, episode, settings, model_service, goal
):
    def choose(seed, feedback):
        state = feedback[-1]["result"]["production"]
        assert state["script_version_id"] is None
        return [writer_task(story=seed["goal"])]

    _, _, outcome, _, _ = completed_plan(
        client, episode, settings, model_service, goal=goal, tasks=choose
    )
    assert outcome.result.tasks[0].task.story == goal
    assert outcome.result.goal_sha256 == digest({"goal": goal})


@pytest.mark.parametrize("tamper", ["goal", "tool_key", "evidence_hash"])
def test_forged_goal_or_unread_investigation_cannot_publish_plan(
    client, episode, settings, model_service, tamper
):
    def patch(result):
        if tamper == "goal":
            result["goal_sha256"] = "f" * 64
        elif tamper == "tool_key":
            result["evidence"][0]["tool_call_key"] = "not-an-executed-tool"
        else:
            result["evidence"][0]["evidence_sha256"] = "f" * 64

    sent, errors = install_plan_model(model_service, patch=patch)
    run_id, actor = begin_producer(client, episode, settings)
    outcome = planning.plan_producer(client.app.state.session_factory, settings, run_id, actor)
    assert outcome.status == "STOPPED", (outcome.model_dump_json(), errors)
    assert not errors and len(sent) == 2
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 0
        )
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1


@pytest.mark.parametrize("mutation", ["shell", "limits", "cycle"])
def test_illegal_task_or_model_supplied_limits_never_create_a_child(
    client, episode, settings, model_service, mutation
):
    def patch(result):
        if mutation == "shell":
            result["tasks"][0]["task"] = {"kind": "shell", "command": "echo denied"}
        elif mutation == "limits":
            result["tasks"][0]["task"]["agent_limits"] = {"maximum_cost": "999"}
        else:
            result["tasks"][0]["depends_on"] = ["write"]

    install_plan_model(model_service, patch=patch)
    run_id, actor = begin_producer(client, episode, settings)
    outcome = planning.plan_producer(client.app.state.session_factory, settings, run_id, actor)
    assert outcome.status == "STOPPED"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_human_answer_after_application_restart_resumes_same_durable_producer_plan(
    client, episode, settings, model_service
):
    run_id, actor = begin_producer(client, episode, settings)
    sent, errors = install_plan_model(model_service, human=True)
    factory = client.app.state.session_factory
    waiting = planning.plan_producer(factory, settings, run_id, actor)
    assert waiting.status == "HUMAN_REVIEW", (waiting.model_dump_json(), errors)
    with factory() as db:
        original_input = digest(db.get(WorkflowRun, run_id).input_json)
        thread = db.get(AgentLoop, waiting.loop_id).thread_id
        assert db.scalar(select(func.count()).select_from(AgentLoopQuestion)) == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 0
        )
    with TestClient(create_app(settings)) as restarted:
        restored = restarted.app.state.session_factory
        assert planning.resume_producer_plan(restored, settings, waiting.loop_id, actor) == waiting
        assert len(sent) == 2
        outcome = planning.resume_producer_plan(
            restored, settings, waiting.loop_id, actor, decision=answer(waiting)
        )
        assert outcome.status == "COMPLETED", (outcome.model_dump_json(), errors)
        assert outcome.loop_id == waiting.loop_id
        assert (
            planning.resume_producer_plan(
                restored, settings, waiting.loop_id, actor, decision=answer(waiting)
            )
            == outcome
        )
    assert not errors and len(sent) == 3
    with factory() as db:
        reference = planning.read_producer_plan(db, outcome.loop_id, actor)
        assert reference.plan_sha256 == digest(outcome.result.model_dump(mode="json"))
        assert digest(db.get(WorkflowRun, run_id).input_json) == original_input
        assert db.get(AgentLoop, outcome.loop_id).thread_id == thread
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_wrapper_crash_after_durable_output_recovers_without_repeat_model_or_plan(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor = begin_producer(client, episode, settings)
    sent, errors = install_plan_model(model_service)
    save = planning._save

    def crash(*args):
        raise RuntimeError("crash after durable Producer output")

    factory = client.app.state.session_factory
    monkeypatch.setattr(planning, "_save", crash)
    with pytest.raises(RuntimeError, match="durable Producer output"):
        planning.plan_producer(factory, settings, run_id, actor)
    with factory() as db:
        loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id))
        assert loop.status == "COMPLETED"
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 0
        )
    monkeypatch.setattr(planning, "_save", save)
    recovered = planning.plan_producer(factory, settings, run_id, actor)
    assert recovered.status == "COMPLETED" and not errors
    assert planning.plan_producer(factory, settings, run_id, actor) == recovered
    assert len(sent) == 2
    with factory() as db:
        assert planning.read_producer_plan(db, recovered.loop_id, actor).version_no == 1
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 1
        )
        assert db.scalar(select(func.count()).select_from(AgentLoop)) == 1
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("has_script", [False, True])
def test_investigated_actual_approved_script_changes_professional_task_selection(
    client, episode, settings, model_service, has_script
):
    from test_director_creation import approved_script

    source = approved_script(client, episode, count=1) if has_script else None

    def choose(seed, feedback):
        source_id = feedback[-1]["result"]["production"]["script_version_id"]
        if source_id is None:
            return [writer_task()]
        return [
            {
                "task_key": "direct",
                "depends_on": [],
                "reason": "已有精确批准剧本，按当前状态委派导演。",
                "task": {"kind": "director", "script_version_id": source_id},
            }
        ]

    _, _, outcome, _, _ = completed_plan(client, episode, settings, model_service, tasks=choose)
    selected = outcome.result.tasks[0].task
    assert selected.kind == ("director" if has_script else "screenwriter")
    if source:
        assert selected.script_version_id == UUID(source["id"])
