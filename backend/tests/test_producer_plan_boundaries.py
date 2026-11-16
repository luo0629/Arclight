"""Only the original completed Producer loop can authorize its immutable plan."""

from copy import deepcopy
from uuid import uuid4

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import DBAPIError
from test_agent_model import model_service as model_service
from test_producer_delegation import delegation_call, dispatch, dispatched
from test_producer_planning import begin_producer, completed_plan

from arclight.application import producer_planning as planning
from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import OutboxEvent, VersionBinding, WorkflowRun


def changed_outcome(outcome, field):
    replacement = outcome.model_dump(mode="json")
    if field in {"run_id", "project_id", "loop_id"}:
        replacement[field] = str(uuid4())
    elif field == "agent_name":
        replacement[field] = "screenwriter"
    elif field == "stage":
        replacement[field] = "script"
    elif field == "input_sha256":
        replacement[field] = "f" * 64
    else:
        replacement["result"]["summary"] = "循环结果不能替换已保存的计划。"
    return replacement


def test_completed_plan_cannot_be_delegated_from_another_producer_root(
    client, episode, settings, model_service
):
    run_id, _, outcome, reference, sent = completed_plan(client, episode, settings, model_service)
    response = client.post(
        f"/api/v1/projects/{episode['project_id']}/episodes",
        json={"episode_no": 2, "title": "黎明", "target_duration_ms": 30000},
    )
    assert response.status_code == 201, response.text
    other_root, _ = begin_producer(client, response.json(), settings)

    feedback = dispatch(client, settings, delegation_call(other_root, reference))

    assert feedback.status == "FAILED" and feedback.error.code == "PRODUCER_PLAN_INVALID"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 2
        assert db.get(AgentLoop, outcome.loop_id).run_id == run_id
    assert len(sent) == 2
    assert dispatched(client, settings, delegation_call(run_id, reference))["root_run_id"] == str(
        run_id
    )


@pytest.mark.parametrize("status", ["RUNNING", "HUMAN_REVIEW"])
def test_saved_plan_requires_its_original_loop_to_remain_completed(
    client, episode, settings, model_service, status
):
    run_id, _, outcome, reference, sent = completed_plan(client, episode, settings, model_service)
    with client.app.state.session_factory.begin() as db:
        db.execute(update(AgentLoop).where(AgentLoop.id == outcome.loop_id).values(status=status))

    feedback = dispatch(client, settings, delegation_call(run_id, reference))

    assert feedback.status == "FAILED" and feedback.error.code == "PRODUCER_PLAN_INVALID"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert db.get(AgentLoop, outcome.loop_id).status == status
    assert len(sent) == 2


@pytest.mark.parametrize("target", ["binding", "seed"])
def test_saved_plan_and_original_seed_reject_even_coherent_history_rewrites(
    client, episode, settings, model_service, target
):
    run_id, actor, outcome, reference, sent = completed_plan(
        client, episode, settings, model_service
    )
    factory = client.app.state.session_factory
    with factory() as db:
        loop = db.get(AgentLoop, outcome.loop_id)
        original_seed = deepcopy(loop.seed_json)
        binding = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.resource_id == outcome.loop_id,
            )
        )
        original_binding = deepcopy(binding.snapshot_json)
        if target == "binding":
            replacement = deepcopy(original_binding)
            replacement["plan"]["summary"] = "改写已经完成的历史计划。"
            replacement["plan_sha256"] = digest(replacement["plan"])
            rewrite = (
                update(VersionBinding)
                .where(VersionBinding.id == binding.id)
                .values(snapshot_json=replacement)
            )
        else:
            replacement = deepcopy(original_seed)
            replacement["operation_key"] = "producer-plan/forged"
            rewrite = (
                update(AgentLoop)
                .where(AgentLoop.id == loop.id)
                .values(seed_json=replacement, request_sha256=digest(replacement))
            )

    with pytest.raises(DBAPIError, match="IMMUTABLE_HISTORY"), factory.begin() as db:
        db.execute(rewrite)

    with factory() as db:
        assert db.get(AgentLoop, outcome.loop_id).seed_json == original_seed
        binding = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.resource_id == outcome.loop_id,
            )
        )
        assert binding.snapshot_json == original_binding
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
    assert planning.plan_producer(factory, settings, run_id, actor) == outcome
    assert len(sent) == 2
    assert dispatched(client, settings, delegation_call(run_id, reference))["plan_id"] == str(
        outcome.loop_id
    )


@pytest.mark.parametrize(
    "field", ["run_id", "project_id", "loop_id", "agent_name", "stage", "input_sha256", "result"]
)
def test_saved_plan_rejects_changed_original_loop_outcome(
    client, episode, settings, model_service, field
):
    run_id, _, outcome, reference, sent = completed_plan(client, episode, settings, model_service)
    replacement = changed_outcome(outcome, field)
    with client.app.state.session_factory.begin() as db:
        db.execute(
            update(AgentLoop)
            .where(AgentLoop.id == outcome.loop_id)
            .values(outcome_json=replacement)
        )

    feedback = dispatch(client, settings, delegation_call(run_id, reference))

    assert feedback.status == "FAILED" and feedback.error.code == "PRODUCER_PLAN_INVALID"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 1
        assert db.get(AgentLoop, outcome.loop_id).outcome_json == replacement
        binding = db.scalar(
            select(VersionBinding).where(
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.resource_id == outcome.loop_id,
            )
        )
        assert binding.snapshot_json["plan"] == outcome.result.model_dump(mode="json")
    assert len(sent) == 2


@pytest.mark.parametrize(
    "field", ["run_id", "project_id", "loop_id", "agent_name", "stage", "input_sha256"]
)
def test_completed_plan_replay_rejects_changed_outcome_identity(
    client, episode, settings, model_service, field
):
    run_id, actor, outcome, _, sent = completed_plan(client, episode, settings, model_service)
    replacement = changed_outcome(outcome, field)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.execute(
            update(AgentLoop)
            .where(AgentLoop.id == outcome.loop_id)
            .values(outcome_json=replacement)
        )

    with pytest.raises(DomainError) as error:
        planning.plan_producer(factory, settings, run_id, actor)

    assert error.value.code == "PRODUCER_PLAN_INVALID"
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 1
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 0
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
            == 1
        )
        assert db.get(AgentLoop, outcome.loop_id).outcome_json == replacement
    assert len(sent) == 2
