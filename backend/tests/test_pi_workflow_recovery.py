import copy

import pytest
from sqlalchemy import select
from test_agent_model import model_service as model_service
from test_screenwriter_creation import install, run_once, start

from arclight.application.agent_runtime.pi_bridge import PiAgent, PiRuntimeError
from arclight.application.runtime import execute_run
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.base import utcnow
from arclight.persistence.models import OutboxEvent, WorkflowRecoveryCounter, WorkflowRun
from arclight.workers.tasks import (
    MAX_WORKFLOW_RECOVERY_FAILURES,
    _record_recovery_failure,
    pending_workflow_events,
)


def test_retry_requeues_paused_pi_workflow_with_the_original_delivery_and_result(
    client, episode, settings, model_service, monkeypatch
):
    sent, errors = install(model_service, monkeypatch, settings)
    run_id = start(client, episode)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        revision = db.get(WorkflowRun, run_id).revision
        delivery = db.scalars(select(OutboxEvent).where(OutboxEvent.run_id == run_id)).one()
        delivery.published_at = utcnow()

    def lose_pi(*args):
        raise PiRuntimeError("Pi process disconnected after the provider round")

    with monkeypatch.context() as failed_pi:
        failed_pi.setattr(PiAgent, "accept_model", lose_pi)
        for attempt in range(1, MAX_WORKFLOW_RECOVERY_FAILURES + 1):
            with pytest.raises(PiRuntimeError):
                execute_run(factory, settings, run_id, revision)
            with factory.begin() as db:
                retry = _record_recovery_failure(db, run_id, revision)
                assert retry == (attempt < MAX_WORKFLOW_RECOVERY_FAILURES)
        execute_run(factory, settings, run_id, revision)

    assert len(sent) == 1, errors
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run.status == "RUNNING" and run.error_code == "WORKFLOW_RECOVERY_LIMIT"
        frozen_inputs = copy.deepcopy(run.input_json)
        agent_state = copy.deepcopy(run.agent_state_json)
        delivery = db.scalars(select(OutboxEvent).where(OutboxEvent.run_id == run_id)).one()
        event_id = delivery.id
        assert db.get(WorkflowRecoveryCounter, event_id).failure_count == (
            MAX_WORKFLOW_RECOVERY_FAILURES
        )
        assert event_id not in {event.id for event in pending_workflow_events(db)}
        loop = db.scalars(select(AgentLoop).where(AgentLoop.run_id == run_id)).one()
        loop_id = loop.id
        action = db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop_id)).one()
        action_id = action.id
        call_id = action.gateway_call_id
        settled_response = copy.deepcopy(action.result_json)
        assert call_id is not None and settled_response["status"] == "TOOL_CALLS"

    endpoint = f"/api/v1/runs/{run_id}/retry"
    accepted = client.post(endpoint)
    assert accepted.status_code == 202, accepted.text
    assert accepted.json()["status"] == "QUEUED"
    duplicate = client.post(endpoint)
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "NOT_RETRYABLE"
    execute_run(factory, settings, run_id, revision + 1)
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert (run.revision, run.error_code) == (revision, None)
        assert run.input_json == frozen_inputs and run.agent_state_json == agent_state
        assert db.get(WorkflowRecoveryCounter, event_id) is None
        assert db.get(OutboxEvent, event_id).published_at is None
        assert [(event.id, event.revision) for event in pending_workflow_events(db)] == [
            (event_id, revision)
        ]

    finished = run_once(client, settings, run_id)
    assert finished["status"] == "WAITING_REVIEW", (finished["error_code"], errors)
    assert len(sent) == 4, errors
    with factory() as db:
        loop = db.get(AgentLoop, loop_id)
        action = db.get(AgentLoopAction, action_id)
        assert loop.status == "COMPLETED"
        assert action.gateway_call_id == call_id and action.result_json == settled_response
