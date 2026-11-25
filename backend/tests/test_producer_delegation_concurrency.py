"""Real PostgreSQL serialization protects delegation identities and cross-Agent limits."""

from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
from threading import Barrier
from uuid import UUID

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_concurrency import require_postgres
from test_model_gateway import request, route
from test_producer_delegation import delegation_call, dispatch, dispatched
from test_producer_planning import completed_plan, writer_task

from arclight.application.agent_limits import action_usage
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.model_gateway import invoke
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentExecutionContext, AgentRunLimits
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.model_gateway import GatewayCapabilities, GatewayPolicy
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.models import OutboxEvent, WorkflowRun


@pytest.mark.parametrize("same_transport_key", [True, False])
def test_postgres_parallel_duplicate_task_has_one_child_and_one_dispatch(
    client, episode, settings, model_service, same_transport_key
):
    require_postgres(settings)
    run_id, _, _, reference, _ = completed_plan(client, episode, settings, model_service)
    gate = Barrier(2)

    def attempt(number):
        call = delegation_call(
            run_id, reference, key="same" if same_transport_key else f"duplicate-{number}"
        )
        gate.wait(timeout=20)
        return dispatch(client, settings, call)

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, (1, 2)))
    assert [item.status for item in responses] == ["SUCCEEDED", "SUCCEEDED"], responses
    assert responses[0].result == responses[1].result
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(WorkflowRun)) == 2
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


def test_postgres_parallel_distinct_tasks_still_enforce_episode_exclusivity(
    client, episode, settings, model_service
):
    require_postgres(settings)
    limits = AgentRunLimits(max_delegations=1, maximum_cost=Decimal("0"), max_duration_seconds=3600)
    tasks = [writer_task("first"), writer_task("second")]
    run_id, _, _, reference, _ = completed_plan(
        client, episode, settings, model_service, tasks=tasks, limits=limits
    )
    gate = Barrier(2)

    def attempt(task_key):
        gate.wait(timeout=20)
        return dispatch(client, settings, delegation_call(run_id, reference, task_key=task_key))

    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(attempt, ("first", "second")))
    assert sorted(item.status for item in responses) == ["FAILED", "SUCCEEDED"]
    refused = next(item for item in responses if item.status == "FAILED")
    assert refused.error.code == "EPISODE_BUSY"
    with client.app.state.session_factory() as db:
        assert db.scalar(select(func.count()).select_from(AgentRunLimit)) == 2
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1


@pytest.mark.parametrize(
    "policy,quote",
    [
        ({"maximum_cost": "1"}, Decimal("1")),
        ({"maximum_cost": "0", "max_calls": 5}, Decimal("0")),
    ],
)
def test_postgres_producer_and_actual_child_record_both_concurrent_calls(
    client, episode, settings, model_service, policy, quote
):
    require_postgres(settings)
    limits = AgentRunLimits(max_duration_seconds=3600, **policy)
    run_id, actor, _, reference, _ = completed_plan(
        client, episode, settings, model_service, limits=limits
    )
    receipt = dispatched(client, settings, delegation_call(run_id, reference))
    child_id = UUID(receipt["child_run_id"])
    factory = client.app.state.session_factory
    with factory.begin() as db:
        child = db.get(WorkflowRun, child_id)
        state = child.agent_state_json
        child.agent_state_json = transition_agent_step(
            state,
            scope_for_run(child, UUID(episode["project_id"])),
            expected_revision=state["revision"],
            step_id="script",
            target="RUNNING",
        ).model_dump(mode="json")
        child.status = "RUNNING"
    selected = route(
        maximum_cost=quote,
        capabilities=GatewayCapabilities(
            version="producer-budget-test/1",
            tasks=frozenset({"producer", "script"}),
            parameters=frozenset({"seed"}),
        ),
    )
    gate, adapter_calls = Barrier(2), []

    def attempt(values):
        identity, stage, agent = values
        gateway_request = request(
            episode,
            selected,
            workflow_id=identity,
            actor=actor,
            task=stage,
            execution=AgentExecutionContext(
                run_id=identity, agent_name=agent, stage=stage, step_key="shared-concurrent-turn"
            ),
        )
        gate.wait(timeout=20)
        try:
            invoke(
                factory,
                gateway_request,
                selected,
                lambda: adapter_calls.append(identity),
                policy=GatewayPolicy(),
            )
            return "SUCCEEDED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(
            pool.map(
                attempt, [(run_id, "producer", "producer"), (child_id, "script", "screenwriter")]
            )
        )
    assert outcomes == ["SUCCEEDED", "SUCCEEDED"]
    assert set(adapter_calls) == {run_id, child_id}
    with factory() as db:
        usage = action_usage(db, run_id)
        assert len(usage["actions"]) == 6
        assert usage["pending_estimate"] == quote * 2
        assert db.get(AgentRunLimit, child_id).root_run_id == run_id
