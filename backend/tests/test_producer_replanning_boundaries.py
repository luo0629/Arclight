"""Replanning must preserve authority, actual work and the original billing root."""

import json
from copy import deepcopy
from decimal import Decimal
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import func, select
from test_agent_loop_recovery import answer
from test_agent_model import model_service as model_service
from test_collaboration import create_episode, create_project, register
from test_producer_delegation import delegation_call, dispatch
from test_producer_planning import completed_plan, writer_task
from test_producer_replanning import install_replan_model, observation, replanned
from test_screenwriter_creation import run_once

from arclight.application import producer_replanning as replanning
from arclight.application.agent_limits import action_usage
from arclight.application.agent_tools import grant_agent_tools
from arclight.application.gateway_accounting import latest_receipt
from arclight.application.producer_plan_history import read_current_plan
from arclight.application.producer_planning import producer_registry
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.model_gateway import GatewayPolicy
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import OutboxEvent, Project, User, WorkflowRun
from arclight.persistence.versions import VersionBinding


def patched_replan_model(model_service, patch):
    def install(respond):
        def respond_with_patch(request):
            response = respond(request)
            value = response.json()
            message = value["choices"][0]["message"]
            content = message.get("content")
            if content:
                proposal = json.loads(content)
                if proposal.get("decision") == "COMPLETE":
                    patch(proposal["result"])
                    message["content"] = json.dumps(proposal, ensure_ascii=False)
            return httpx.Response(response.status_code, json=value)

        model_service(respond_with_patch)

    return install_replan_model(install)


@pytest.mark.parametrize(
    "tamper",
    ["base_id", "base_hash", "observation_id", "observation_hash", "reason", "goal", "evidence"],
)
def test_forged_replan_scope_or_old_loop_evidence_never_publishes(
    client, episode, settings, model_service, tamper
):
    run_id, actor, first, reference, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, reference)
    factory = client.app.state.session_factory
    with factory() as db:
        original = deepcopy(
            db.scalar(
                select(VersionBinding).where(VersionBinding.resource_type == "PRODUCER_PLAN")
            ).snapshot_json
        )

    def patch(result):
        if tamper == "base_id":
            result["base_plan_id"] = str(uuid4())
        elif tamper == "base_hash":
            result["base_plan_sha256"] = "f" * 64
        elif tamper == "observation_id":
            result["observation_id"] = str(uuid4())
        elif tamper == "observation_hash":
            result["observation_sha256"] = "f" * 64
        elif tamper == "reason":
            result["reason"] = "替换已冻结的人工说明"
        elif tamper == "goal":
            result["plan"]["goal_sha256"] = "f" * 64
        else:
            result["plan"]["evidence"] = [
                row.model_dump(mode="json") for row in first.result.evidence
            ]

    sent, errors = patched_replan_model(model_service, patch)
    result = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert result.status == "STOPPED", (result.model_dump_json(), errors)
    assert len(sent) == 2 and not errors
    with factory() as db:
        bindings = list(
            db.scalars(
                select(VersionBinding).where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
        )
        assert len(bindings) == 1 and bindings[0].snapshot_json == original
        assert (
            read_current_plan(db, db.get(WorkflowRun, run_id), actor).plan_id == reference.plan_id
        )
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_replan_carries_active_child_and_rejects_work_from_superseded_plan(
    client, episode, settings, model_service
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    first = dispatch(client, settings, delegation_call(run_id, original, key="original-work"))
    assert first.status == "SUCCEEDED"
    observed = observation(client, settings, run_id, actor, original)
    assert str(observed.tasks[0].child_run_id) == first.result["child_run_id"]
    assert not observed.tasks[0].replaceable
    sent, errors = install_replan_model(model_service)
    result, current = replanned(client, settings, run_id, actor, observed)
    second = dispatch(client, settings, delegation_call(run_id, current, key="current-work"))
    repeated = dispatch(
        client, settings, delegation_call(run_id, current, key="current-work-again")
    )
    assert second.status == repeated.status == "SUCCEEDED"
    assert second.result == repeated.result
    assert second.result["plan_id"] == str(result.loop_id)
    assert second.result["delegation_id"] == first.result["delegation_id"]
    assert second.result["child_run_id"] == first.result["child_run_id"]
    stale = dispatch(client, settings, delegation_call(run_id, original, key="old-plan-again"))
    assert stale.status == "FAILED" and stale.error.code == "PRODUCER_PLAN_SUPERSEDED"
    assert len(sent) == 2 and not errors
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.parametrize("mutation", ["change", "rename", "duplicate"])
def test_active_child_cannot_be_replaced_renamed_or_duplicated(
    client, episode, settings, model_service, mutation
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    existing = dispatch(client, settings, delegation_call(run_id, original))
    assert existing.status == "SUCCEEDED"
    observed = observation(client, settings, run_id, actor, original)
    if mutation == "change":
        proposed = [writer_task(story="改写任务不能绕过活动子任务。")]
    elif mutation == "rename":
        proposed = [writer_task("renamed")]
    else:
        proposed = [writer_task(), writer_task("duplicate")]
    sent, errors = install_replan_model(model_service, tasks=proposed)
    result = replanning.replan_producer(
        client.app.state.session_factory, settings, run_id, actor, observed.id
    )
    assert result.status == "STOPPED", (result.model_dump_json(), errors)
    assert len(sent) == 2 and not errors
    with client.app.state.session_factory() as db:
        assert read_current_plan(db, db.get(WorkflowRun, run_id), actor).plan_id == original.plan_id
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_durable_replan_output_recovers_publication_without_repeat_model_or_history(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, original)
    factory = client.app.state.session_factory
    sent, errors = install_replan_model(model_service)
    save = replanning._save

    def crash(*args):
        raise RuntimeError("crash after durable replan output")

    monkeypatch.setattr(replanning, "_save", crash)
    with pytest.raises(RuntimeError, match="durable replan output"):
        replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    with factory() as db:
        loops = list(db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)))
        assert len(loops) == 2 and all(row.status == "COMPLETED" for row in loops)
        assert read_current_plan(db, db.get(WorkflowRun, run_id), actor).plan_id == original.plan_id
        thread_ids = {row.id: row.thread_id for row in loops}
    monkeypatch.setattr(replanning, "_save", save)
    recovered, current = replanned(client, settings, run_id, actor, observed)
    assert replanning.replan_producer(factory, settings, run_id, actor, observed.id) == recovered
    assert replanning.resume_producer_replan(factory, settings, current.plan_id, actor) == recovered
    assert len(sent) == 2 and not errors
    with factory() as db:
        assert read_current_plan(db, db.get(WorkflowRun, run_id), actor).version_no == 2
        assert {row.id: row.thread_id for row in db.scalars(select(AgentLoop))} == thread_ids
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 2
        )


def effect_counts(db):
    return tuple(
        db.scalar(select(func.count()).select_from(model))
        for model in (
            WorkflowRun,
            AgentLoop,
            AgentLoopDecision,
            VersionBinding,
            GatewayCall,
            GatewayReceipt,
            OutboxEvent,
        )
    )


def editor_episode(client):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    member = register(client, "replan-editor@example.com")
    client.headers["Authorization"] = "Bearer " + owner["access_token"]
    response = client.post(
        f"/api/v1/projects/{project['id']}/members",
        json={"email": member["user"]["email"], "role": "editor"},
    )
    assert response.status_code == 201, response.text
    client.headers["Authorization"] = "Bearer " + member["access_token"]
    return episode, owner, member


@pytest.mark.parametrize("phase", ["begin", "completed_replay", "human_resume"])
@pytest.mark.parametrize("revocation", ["grant", "disabled", "viewer", "archived", "cancelled"])
def test_replan_reauthorizes_begin_completed_replay_and_human_resume_before_side_effects(
    client, settings, model_service, phase, revocation
):
    episode, owner, member = editor_episode(client)
    actor = member["user"]["id"]
    run_id, _, _, original, _ = completed_plan(
        client, episode, settings, model_service, actor=actor
    )
    observed = observation(client, settings, run_id, actor, original)
    sent, errors = install_replan_model(model_service, human=phase == "human_resume")
    factory = client.app.state.session_factory
    previous = None
    if phase != "begin":
        previous = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
        assert previous.status == ("HUMAN_REVIEW" if phase == "human_resume" else "COMPLETED")
    if revocation == "viewer":
        client.headers["Authorization"] = "Bearer " + owner["access_token"]
        response = client.patch(
            f"/api/v1/projects/{episode['project_id']}/members/{actor}", json={"role": "viewer"}
        )
        assert response.status_code == 200, response.text
    else:
        with factory.begin() as db:
            if revocation == "grant":
                grant_agent_tools(
                    db,
                    AgentToolGrantRequest(run_id=run_id, agent_name="producer", allowed_tools=[]),
                    actor,
                    producer_registry(settings),
                )
            elif revocation == "disabled":
                db.get(User, UUID(actor)).disabled = True
            elif revocation == "archived":
                db.get(Project, UUID(episode["project_id"])).archived = True
            else:
                db.get(WorkflowRun, run_id).status = "CANCELLED"
    with factory() as db:
        counts = effect_counts(db)
    sent_before = len(sent)
    with pytest.raises(DomainError) as refused:
        if phase == "human_resume":
            replanning.resume_producer_replan(
                factory, settings, previous.loop_id, actor, decision=answer(previous)
            )
        else:
            replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert refused.value.code in {
        "FORBIDDEN",
        "PROJECT_ARCHIVED",
        "AGENT_CANCELLED",
        "AGENT_TOOL_GRANT_FORBIDDEN",
        "AGENT_NOT_ACTIVE",
    }
    assert len(sent) == sent_before and not errors
    with factory() as db:
        assert effect_counts(db) == counts
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


def test_new_replan_continues_after_historical_call_cap_and_preserves_old_plan(
    client, episode, settings, model_service
):
    limits = AgentRunLimits(max_model_calls=3, maximum_cost=Decimal("0"), max_duration_seconds=3600)
    run_id, actor, _, original, first_sent = completed_plan(
        client, episode, settings, model_service, limits=limits
    )
    assert len(first_sent) == 2
    observed = observation(client, settings, run_id, actor, original)
    factory = client.app.state.session_factory
    with factory() as db:
        control = db.get(AgentRunLimit, run_id)
        frozen = (control.limits_json, control.deadline_at)
    sent, errors = install_replan_model(model_service)
    result = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert result.status == "COMPLETED", result.model_dump_json()
    assert len(sent) == 2 and not errors
    assert replanning.replan_producer(factory, settings, run_id, actor, observed.id) == result
    assert len(sent) == 2
    with factory() as db:
        assert len([row for row in action_usage(db, run_id)["actions"] if row.kind == "MODEL"]) == 4
        control = db.get(AgentRunLimit, run_id)
        assert (control.limits_json, control.deadline_at) == frozen
        current = read_current_plan(db, db.get(WorkflowRun, run_id), actor)
        assert current.plan_id == result.loop_id and current.previous_plan_id == original.plan_id
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0


@pytest.mark.parametrize("mutation", ["change", "rename", "duplicate", "duplicate_dependency"])
def test_real_unknown_child_result_is_observable_but_never_replaced_or_resent(
    client, episode, settings, model_service, mutation
):
    anchor = writer_task("anchor", story="另一独立制作目标作为既有前置键。")
    run_id, actor, _, original, _ = completed_plan(
        client,
        episode,
        settings,
        model_service,
        limits=AgentRunLimits(maximum_cost=Decimal("1"), max_duration_seconds=3600),
        tasks=[anchor, writer_task()],
    )
    settings.model_gateway_policy = GatewayPolicy(
        text_maximum_cost=Decimal("0.1"), pricing_version="prod03-unknown-child:1"
    )
    receipt = dispatch(client, settings, delegation_call(run_id, original))
    assert receipt.status == "SUCCEEDED"
    child_id = UUID(receipt.result["child_run_id"])
    child_sent = []

    def timeout(request):
        child_sent.append(request)
        raise httpx.ReadTimeout("provider accepted request but returned no trusted outcome")

    model_service(timeout)
    stopped = run_once(client, settings, child_id)
    assert stopped["status"] == "FAILED", stopped
    assert len(child_sent) == 1
    factory = client.app.state.session_factory
    with factory() as db:
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == child_id))
        outcome = latest_receipt(db, call.id)
        assert outcome.status == "RESULT_UNKNOWN" and outcome.actual_cost is None
        preserved = (call.id, outcome.id, outcome.status, outcome.actual_cost, call.maximum_cost)
        assert call.maximum_cost == Decimal("0.1")
        assert action_usage(db, run_id)["pending_estimate"] == Decimal("0.1")
        original_outbox = {
            (row.id, row.run_id, row.production_event_id, row.event_type, row.revision)
            for row in db.scalars(select(OutboxEvent))
        }
        assert len(original_outbox) == 2
        assert {row[3] for row in original_outbox} == {"workflow.execute", "production.coordinate"}
    observed = observation(client, settings, run_id, actor, original)
    unknown_task = next(task for task in observed.tasks if task.task_key == "write")
    assert unknown_task.unknown_external_result and not unknown_task.replaceable
    if mutation == "change":
        proposed = [anchor, writer_task(story="未知结果时不能换故事盲重试。")]
    elif mutation == "rename":
        proposed = [anchor, writer_task("renamed")]
    elif mutation == "duplicate_dependency":
        proposed = [anchor, writer_task(), writer_task("duplicate", depends_on=("anchor",))]
    else:
        proposed = [anchor, writer_task(), writer_task("duplicate")]
    sent, errors = install_replan_model(model_service, tasks=proposed)
    result = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert result.status == "STOPPED", result.model_dump_json()
    assert len(sent) == 2 and not errors
    assert replanning.replan_producer(factory, settings, run_id, actor, observed.id) == result
    assert len(sent) == 2
    with factory() as db:
        calls = list(db.scalars(select(GatewayCall).where(GatewayCall.workflow_id == child_id)))
        assert len(calls) == 1
        call = calls[0]
        outcome = latest_receipt(db, call.id)
        assert (
            call.id,
            outcome.id,
            outcome.status,
            outcome.actual_cost,
            call.maximum_cost,
        ) == preserved
        assert action_usage(db, run_id)["pending_estimate"] == Decimal("0.1")
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert {
            (row.id, row.run_id, row.production_event_id, row.event_type, row.revision)
            for row in db.scalars(select(OutboxEvent))
        } == original_outbox
        assert read_current_plan(db, db.get(WorkflowRun, run_id), actor).plan_id == original.plan_id


def test_completed_replan_replay_rejects_conflicting_human_decision(
    client, episode, settings, model_service
):
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, original)
    sent, errors = install_replan_model(model_service, human=True)
    factory = client.app.state.session_factory
    waiting = replanning.replan_producer(factory, settings, run_id, actor, observed.id)
    assert waiting.status == "HUMAN_REVIEW"
    result = replanning.resume_producer_replan(
        factory, settings, waiting.loop_id, actor, decision=answer(waiting)
    )
    assert result.status == "COMPLETED"
    assert (
        replanning.resume_producer_replan(
            factory, settings, waiting.loop_id, actor, decision=answer(waiting)
        )
        == result
    )
    with pytest.raises(DomainError) as conflict:
        replanning.resume_producer_replan(
            factory, settings, waiting.loop_id, actor, decision=answer(waiting, choice="停止")
        )
    assert conflict.value.code == "AGENT_DECISION_CONFLICT"
    assert len(sent) == 3 and not errors
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentLoopDecision)) == 1
        assert read_current_plan(db, db.get(WorkflowRun, run_id), actor).plan_id == result.loop_id
