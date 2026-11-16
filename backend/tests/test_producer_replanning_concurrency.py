"""Real PostgreSQL protects observations, successor publication and carried delegation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_concurrency import require_postgres
from test_producer_delegation import delegation_call, dispatch
from test_producer_planning import completed_plan
from test_producer_replanning import install_replan_model, observation, replanned

from arclight.application import producer_replanning as replanning
from arclight.application.producer_observation import observe_producer
from arclight.application.producer_plan_history import read_current_plan
from arclight.core.errors import DomainError
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import OutboxEvent, VersionBinding, WorkflowRun


def test_postgres_duplicate_observation_commits_one_immutable_snapshot(
    client, episode, settings, model_service
):
    require_postgres(settings)
    run_id, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    factory = client.app.state.session_factory
    request = ProducerObservationRequest(
        base_plan_id=reference.plan_id,
        base_plan_sha256=reference.plan_sha256,
        trigger="RECHECK",
        reason="并发重放同一权威观察请求。",
    )
    request_id, gate = uuid4().hex, Barrier(2)

    def attempt(_):
        gate.wait(timeout=20)
        return observe_producer(factory, settings, run_id, actor, request_id, request)

    with ThreadPoolExecutor(max_workers=2) as pool:
        observed = list(pool.map(attempt, (1, 2)))
    assert observed[0] == observed[1]
    with factory() as db:
        assert (
            db.scalar(
                select(func.count())
                .select_from(VersionBinding)
                .where(VersionBinding.resource_type == "PRODUCER_OBSERVATION")
            )
            == 1
        )


@pytest.mark.parametrize("same_observation", [True, False])
def test_postgres_competing_replans_cannot_publish_two_successors(
    client, episode, settings, model_service, same_observation
):
    require_postgres(settings)
    run_id, actor, _, reference, _ = completed_plan(client, episode, settings, model_service)
    first = observation(client, settings, run_id, actor, reference)
    second = first if same_observation else observation(client, settings, run_id, actor, reference)
    sent, errors = install_replan_model(model_service)
    gate, factory = Barrier(2), client.app.state.session_factory

    def attempt(observed):
        gate.wait(timeout=20)
        try:
            return replanning.replan_producer(factory, settings, run_id, actor, observed.id)
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(attempt, (first, second)))
    completed = [
        item for item in outcomes if not isinstance(item, str) and item.status == "COMPLETED"
    ]
    assert completed, outcomes
    assert not errors
    with factory() as db:
        plans = list(
            db.scalars(
                select(VersionBinding).where(VersionBinding.resource_type == "PRODUCER_PLAN")
            )
        )
        assert len(plans) == 2
        current = read_current_plan(db, db.get(WorkflowRun, run_id), actor)
        assert current.version_no == 2 and current.previous_plan_id == reference.plan_id
        assert current.plan_id == completed[0].loop_id
    if same_observation:
        recovered = replanning.replan_producer(factory, settings, run_id, actor, first.id)
        assert recovered.loop_id == completed[0].loop_id
        assert len(sent) == 2
    else:
        assert len(completed) == 1


def test_postgres_carried_task_concurrent_dispatch_still_creates_one_original_child(
    client, episode, settings, model_service
):
    require_postgres(settings)
    run_id, actor, _, original, _ = completed_plan(client, episode, settings, model_service)
    observed = observation(client, settings, run_id, actor, original)
    install_replan_model(model_service)
    _, current = replanned(client, settings, run_id, actor, observed)
    gate = Barrier(2)

    def attempt(number):
        gate.wait(timeout=20)
        return dispatch(client, settings, delegation_call(run_id, current, key=f"carried-{number}"))

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(attempt, (1, 2)))
    assert [item.status for item in results] == ["SUCCEEDED", "SUCCEEDED"], results
    assert results[0].result == results[1].result
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        child_id = UUID(results[0].result["child_run_id"])
        assert db.get(AgentRunLimit, child_id).root_run_id == run_id
