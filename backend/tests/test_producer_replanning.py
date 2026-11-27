"""Replanning uses real durable loops, professional work, reviews and accounting."""

import json
from copy import deepcopy
from decimal import Decimal
from types import SimpleNamespace
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from starlette.testclient import TestClient
from test_agent_loop_recovery import answer
from test_agent_model import call_response
from test_agent_model import model_service as model_service
from test_agent_runtime import final, tool_feedback
from test_producer_delegation import delegation_call, dispatch, dispatched
from test_producer_planning import completed_plan, writer_task
from test_screenwriter_creation import approve, run_once
from test_screenwriter_revision import install_revision as install_writer

from arclight.application import producer_replanning as replanning
from arclight.application.agent_limits import action_usage
from arclight.application.agent_runtime.store import submit_agent_decision
from arclight.application.invalidation import mark_stale
from arclight.application.producer_observation import observe_producer
from arclight.application.producer_plan_history import read_plan_record
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import (
    AssetVersion,
    OutboxEvent,
    Script,
    Storyboard,
    VersionBinding,
)


def observation(client, settings, run_id, actor, reference, **changes):
    request = ProducerObservationRequest.model_validate(
        {
            "base_plan_id": reference.plan_id,
            "base_plan_sha256": reference.plan_sha256,
            "trigger": "RECHECK",
            "reason": "按当前权威状态重新核对剩余制作任务。",
            **changes,
        }
    )
    return observe_producer(
        client.app.state.session_factory, settings, run_id, actor, uuid4().hex, request
    )


def install_replan_model(model_service, *, tasks=None, human=False, cost="0", on_http=None):
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
                item["role"] == "user" and "human_decision" in json.loads(item["content"])["data"]
                for item in payload["messages"]
            )
            if not feedback:
                value = call_response(
                    ("current-state", "investigate_project", {"query": {"path": "overview"}})
                )
            elif human and not answered:
                value = final("HUMAN_REVIEW")
            else:
                proposed = tasks(seed, feedback) if callable(tasks) else tasks
                plan = {
                    "schema_version": "producer-plan/1",
                    "goal_sha256": seed["goal_sha256"],
                    "summary": "依据当前观察调整剩余制作，保留原有效交付。",
                    "evidence": [
                        {
                            "tool_call_key": item["idempotency_key"],
                            "evidence_sha256": item["result"]["evidence_sha256"],
                        }
                        for item in feedback
                        if item["status"] == "SUCCEEDED"
                    ],
                    "tasks": deepcopy(proposed or seed["base_plan"]["plan"]["tasks"]),
                }
                value = final(
                    result={
                        "schema_version": "producer-replan/1",
                        "base_plan_id": seed["base_plan"]["plan_id"],
                        "base_plan_sha256": seed["base_plan"]["plan_sha256"],
                        "observation_id": seed["observation"]["id"],
                        "observation_sha256": seed["observation"]["snapshot_sha256"],
                        "reason": seed["observation"]["reason"],
                        "plan": plan,
                    }
                )
            value["usage"] = {"prompt_tokens": 10, "completion_tokens": 10, "actual_cost": cost}
            return httpx.Response(200, json=value)
        except Exception as exc:
            errors.append(repr(exc))
            raise

    model_service(respond)
    return sent, errors


def replanned(client, settings, run_id, actor, observed):
    result = replanning.replan_producer(
        client.app.state.session_factory, settings, run_id, actor, observed.id
    )
    assert result.status == "COMPLETED", result.model_dump_json()
    assert result.result is not None
    reference = SimpleNamespace(
        plan_id=result.loop_id, plan_sha256=digest(result.result.plan.model_dump(mode="json"))
    )
    return result, reference


def accounting(client, run_id):
    with client.app.state.session_factory() as db:
        ids = list(
            db.scalars(select(AgentRunLimit.run_id).where(AgentRunLimit.root_run_id == run_id))
        )
        return {
            "calls": {
                receipt.id: (
                    call.id,
                    receipt.status,
                    receipt.actual_cost,
                    deepcopy(receipt.usage_json),
                )
                for call, receipt in db.execute(
                    select(GatewayCall, GatewayReceipt)
                    .join(GatewayReceipt, GatewayReceipt.call_id == GatewayCall.id)
                    .where(GatewayCall.workflow_id.in_(ids))
                )
            },
            "outbox": set(db.scalars(select(OutboxEvent.id).where(OutboxEvent.run_id.in_(ids)))),
            "usage": deepcopy(action_usage(db, run_id)),
        }


def test_partial_replan_preserves_actual_success_outputs_and_fees_across_restart(
    client, episode, settings, model_service, monkeypatch
):
    tasks = [writer_task("keep"), writer_task("repair", story="尚待修订的另一项写作任务。")]
    limits = AgentRunLimits(maximum_cost=Decimal("20"), max_duration_seconds=3600, max_steps=128)
    run_id, actor, initial, original, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks, limits=limits, cost="0.1"
    )
    keep = dispatched(client, settings, delegation_call(run_id, original, task_key="keep"))
    keep_id = UUID(keep["child_run_id"])
    sent_writer, errors, ordinary_errors = install_writer(
        model_service, monkeypatch, settings, patches=0
    )
    waiting = run_once(client, settings, keep_id)
    assert waiting["status"] == "WAITING_REVIEW", (waiting, errors, ordinary_errors)
    approve(client, waiting)
    waiting = run_once(client, settings, keep_id)
    assert waiting["pending_review"]["resource_type"] == "storyboard"
    approve(client, waiting)
    assert run_once(client, settings, keep_id)["status"] == "SUCCEEDED"
    assert sent_writer and not errors and not ordinary_errors
    with client.app.state.session_factory() as db:
        outputs = {
            item.id: deepcopy(item.content_json)
            for model in (Script, Storyboard)
            for item in db.scalars(select(model).where(model.run_id == keep_id))
        }
        assert len(outputs) == 2
        approved_script_id = db.scalar(select(Script.id).where(Script.run_id == keep_id))
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    failed = dispatched(client, settings, delegation_call(run_id, original, task_key="repair"))
    failed_id = UUID(failed["child_run_id"])
    model_service(lambda request: httpx.Response(400, json={"error": {"message": "rejected"}}))
    assert run_once(client, settings, failed_id)["status"] == "FAILED"
    observed = observation(client, settings, run_id, actor, original)
    actual_keep = next(item for item in observed.tasks if item.task_key == "keep")
    actual_failed = next(item for item in observed.tasks if item.task_key == "repair")
    assert actual_keep.status == "SUCCEEDED" and not actual_keep.replaceable
    assert set(actual_keep.output_version_ids) == set(outputs)
    assert actual_failed.status == "FAILED" and actual_failed.replaceable
    changed_tasks = [
        tasks[0],
        writer_task("repair", story="补齐失败写作任务中的行动动机。"),
        {
            "task_key": "next_director",
            "depends_on": ["keep"],
            "reason": "已完成剧本通过精确批准，接续独立导演任务。",
            "task": {"kind": "director", "script_version_id": str(approved_script_id)},
        },
    ]
    sent, errors = install_replan_model(model_service, tasks=changed_tasks, cost="0.1")
    completed, current = replanned(client, settings, run_id, actor, observed)
    assert len(sent) == 2 and not errors
    with client.app.state.session_factory() as db:
        history = read_plan_record(db, UUID(episode["project_id"]), current.plan_id, actor)
        assert history.version_no == 2 and history.previous_plan_id == initial.loop_id
        assert history.carried_tasks["keep"].plan_id == original.plan_id
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline
        for output_id in outputs:
            assert db.get(AssetVersion, output_id).stale is False
    before = accounting(client, run_id)
    with TestClient(create_app(settings)) as restarted:
        recovered = replanning.replan_producer(
            restarted.app.state.session_factory, settings, run_id, actor, observed.id
        )
        assert recovered == completed
        replay = dispatched(
            restarted, settings, delegation_call(run_id, current, task_key="keep", key="carried")
        )
        assert replay["child_run_id"] == keep["child_run_id"]
        assert replay["delegation_id"] == keep["delegation_id"]
        assert replay["plan_id"] == str(current.plan_id)
    assert len(sent) == 2 and accounting(client, run_id)["calls"] == before["calls"]
    assert accounting(client, run_id)["outbox"] == before["outbox"]
    director = dispatched(
        client, settings, delegation_call(run_id, current, task_key="next_director")
    )
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, UUID(director["child_run_id"])).root_run_id == run_id
    stopped = client.post(f"/api/v1/runs/{director['child_run_id']}/cancel")
    assert stopped.status_code == 200 and stopped.json()["status"] == "CANCELLED"
    replacement = dispatched(client, settings, delegation_call(run_id, current, task_key="repair"))
    assert replacement["child_run_id"] != failed["child_run_id"]
    assert replacement["delegation_id"] != failed["delegation_id"]
    old = dispatch(client, settings, delegation_call(run_id, original, task_key="repair"))
    assert old.status == "FAILED"
    with client.app.state.session_factory() as db:
        for model in (Script, Storyboard):
            for item in db.scalars(select(model).where(model.run_id == keep_id)):
                assert item.content_json == outputs[item.id]
        child = db.get(AgentRunLimit, UUID(replacement["child_run_id"]))
        assert child.root_run_id == run_id and child.deadline_at == deadline
        assert child.limits_json == db.get(AgentRunLimit, run_id).limits_json


def test_unchanged_undelegated_task_keeps_canonical_origin_through_three_plans(
    client, episode, settings, model_service
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    previous = original
    for version in (2, 3):
        observed = observation(client, settings, run_id, actor, previous)
        sent, errors = install_replan_model(model_service)
        _, current = replanned(client, settings, run_id, actor, observed)
        assert len(sent) == 2 and not errors
        with client.app.state.session_factory() as db:
            record = read_plan_record(db, UUID(episode["project_id"]), current.plan_id, actor)
            assert record.version_no == version
            assert record.carried_tasks["write"].plan_id == original.plan_id
            assert record.previous_plan_id == previous.plan_id
        previous = current
    first = dispatched(client, settings, delegation_call(run_id, current))
    before = accounting(client, run_id)
    assert dispatched(client, settings, delegation_call(run_id, current)) == first
    assert accounting(client, run_id)["calls"] == before["calls"]
    assert accounting(client, run_id)["outbox"] == before["outbox"]


def test_replan_question_and_decision_survive_restart_without_automatic_asset_approval(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, original)
    factory = client.app.state.session_factory
    decision_observations = []

    def observe_saved_answer(_):
        with factory() as db:
            decision = db.scalar(select(AgentLoopDecision))
            identity = decision.id if decision else None
        if identity is not None and not decision_observations:
            decision_observations.append(
                observation(
                    client,
                    settings,
                    run_id,
                    actor,
                    original,
                    trigger="HUMAN_DECISION",
                    human_decision_id=identity,
                    requested_task_keys=["write"],
                )
            )

    sent, errors = install_replan_model(model_service, human=True, on_http=observe_saved_answer)
    waiting = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert waiting.status == "HUMAN_REVIEW" and waiting.question_id
    instruction = replanning.replanning_instruction("producer/1")
    with factory() as db:
        loop = db.get(AgentLoop, waiting.loop_id)
        assert loop.seed_json["system_instruction"] == instruction
        thread = loop.thread_id
    monkeypatch.setattr(
        replanning, "replanning_instruction", lambda _version: "新登记使用的重规划提示词。"
    )
    with TestClient(create_app(settings)) as restarted:
        restored = restarted.app.state.session_factory
        assert (
            replanning.resume_producer_replan(restored, settings, waiting.loop_id, actor) == waiting
        )
        completed = replanning.resume_producer_replan(
            restored, settings, waiting.loop_id, actor, decision=answer(waiting)
        )
        assert completed.status == "COMPLETED", completed.model_dump_json()
        assert completed.loop_id == waiting.loop_id
        assert (
            replanning.resume_producer_replan(
                restored, settings, waiting.loop_id, actor, decision=answer(waiting)
            )
            == completed
        )
    assert len(sent) == 3 and not errors
    assert len(decision_observations) == 1
    assert decision_observations[0].affected_task_keys == ["write"]
    with factory() as db:
        assert db.get(AgentLoop, completed.loop_id).thread_id == thread
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert db.scalar(select(func.count()).select_from(Script)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_new_delegation_invalidates_observation_before_another_model_call(
    client, episode, settings, model_service
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, original)
    dispatched(client, settings, delegation_call(run_id, original))
    sent, errors = install_replan_model(model_service)
    with pytest.raises(DomainError) as failure:
        replanning.replan_producer(
            client.app.state.session_factory, settings, run_id, actor, observed.id
        )
    assert failure.value.code == "PRODUCER_OBSERVATION_STALE"
    assert not sent and not errors
    with client.app.state.session_factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 1
        )


def test_new_replan_seed_receives_saved_human_answer_and_supersedes_old_question(
    client, episode, settings, model_service
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    factory = client.app.state.session_factory
    initial = observation(client, settings, run_id, actor, original)
    old_sent, old_errors = install_replan_model(model_service, human=True)
    waiting = replanning.replan_producer(factory, settings, run_id, actor, initial.id)
    assert waiting.status == "HUMAN_REVIEW"
    decision = answer(waiting)
    with factory.begin() as db:
        saved = submit_agent_decision(db, waiting.loop_id, actor, decision)
        decision_id = saved.id
    observed = observation(
        client,
        settings,
        run_id,
        actor,
        original,
        trigger="HUMAN_DECISION",
        human_decision_id=decision_id,
        requested_task_keys=["write"],
    )
    sent, errors = install_replan_model(model_service)
    completed, current = replanned(client, settings, run_id, actor, observed)
    seed_message = json.loads(sent[0]["messages"][1]["content"])
    assert seed_message["trust"] == "UNTRUSTED"
    evidence = seed_message["data"]["observation"]["human_evidence"]
    assert evidence["kind"] == "AGENT"
    assert evidence["decision_id"] == str(decision_id)
    assert evidence["source_run_id"] == str(run_id)
    assert evidence["actor_id"] == actor
    assert evidence["loop_id"] == str(waiting.loop_id)
    assert evidence["question"] == waiting.human_request.model_dump(mode="json")
    assert evidence["decision"] == decision.model_dump(mode="json")
    assert completed.loop_id == current.plan_id
    assert len(sent) == 2 and len(old_sent) == 2 and not errors and not old_errors
    with pytest.raises(DomainError) as superseded:
        replanning.resume_producer_replan(
            factory, settings, waiting.loop_id, actor, decision=decision
        )
    assert superseded.value.code == "PRODUCER_PLAN_SUPERSEDED"
    assert len(sent) == 2
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert db.scalar(select(func.count()).select_from(Script)) == 0
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_actual_success_becoming_stale_blocks_reuse_and_allows_local_replacement(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    first = dispatched(client, settings, delegation_call(run_id, original))
    child_id = UUID(first["child_run_id"])
    _, errors, ordinary_errors = install_writer(model_service, monkeypatch, settings, patches=0)
    for kind in ("script", "storyboard"):
        waiting = run_once(client, settings, child_id)
        assert waiting["status"] == "WAITING_REVIEW", (waiting, errors, ordinary_errors)
        assert waiting["pending_review"]["resource_type"] == kind
        approve(client, waiting)
    assert run_once(client, settings, child_id)["status"] == "SUCCEEDED"
    assert not errors and not ordinary_errors
    with client.app.state.session_factory.begin() as db:
        board = db.scalar(select(Storyboard).where(Storyboard.run_id == child_id))
        assert mark_stale(db, db.get(AssetVersion, board.id))
    before = accounting(client, run_id)
    refused = dispatch(client, settings, delegation_call(run_id, original))
    assert refused.status == "FAILED" and refused.error.code == "PRODUCER_OUTPUT_STALE"
    assert accounting(client, run_id)["calls"] == before["calls"]
    observed = observation(client, settings, run_id, actor, original)
    assert observed.affected_task_keys == ["write"]
    assert observed.tasks[0].status == "SUCCEEDED" and observed.tasks[0].replaceable
    sent, errors = install_replan_model(
        model_service, tasks=[writer_task(story="根据失效分镜重新整理角色行动及场景节奏。")]
    )
    _, current = replanned(client, settings, run_id, actor, observed)
    replacement = dispatched(client, settings, delegation_call(run_id, current))
    assert replacement["child_run_id"] != first["child_run_id"]
    assert len(sent) == 2 and not errors
