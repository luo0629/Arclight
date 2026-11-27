"""Durable external waits resume the original Pi batch without model polling."""

import json
from dataclasses import replace

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_loop_recovery import resume, start
from test_agent_model import call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, setup_loop, tool_feedback
from test_agent_tools import create_entry

from arclight.application.agent_external_waits import queue_ready_external
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.pi_agent import PiAgent, PiRuntimeError
from arclight.main import create_app
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction, AgentLoopQuestion
from arclight.persistence.models import AgentToolInvocation, OutboxEvent, WorkflowRun


@pytest.mark.parametrize("crash_on_ready", [False, True], ids=["resume", "pi-crash"])
def test_external_wait_survives_restart_and_batched_interrupt_replay(
    client, episode, settings, model_service, monkeypatch, crash_on_ready
):
    run_id, actor, original_registry = setup_loop(client, episode, settings)
    entries = [create_entry(client, episode["project_id"]) for _ in range(2)]
    ready = set()
    executed = []
    original = original_registry.get("read_story_entry")

    def handler(context, parameters):
        assert parameters.entry_id in ready
        executed.append(parameters.entry_id)
        return original.handler(context, parameters)

    registry = AgentToolRegistry(
        [replace(original, handler=handler, is_ready=lambda ctx, params: params.entry_id in ready)]
    )
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(
            200,
            json=final()
            if tool_feedback(payload)
            else call_response(
                *[
                    (f"read-{i}", "read_story_entry", {"entry_id": str(item)})
                    for i, item in enumerate(entries)
                ]
            ),
        )

    model_service(respond)
    waiting = start(client, settings, model_request(run_id), registry)
    assert waiting.status == "WAITING_EXTERNAL" and waiting.question_id is None
    assert waiting.waiting_for.arguments == {"entry_id": str(entries[0])}
    assert len(sent) == 1 and not executed
    with client.app.state.session_factory() as db:
        loop = db.get(AgentLoop, waiting.loop_id)
        assert loop.status == "WAITING_EXTERNAL" and loop.lease_token is None
        assert not list(db.scalars(select(AgentLoopQuestion)))
        assert not list(db.scalars(select(AgentToolInvocation)))
    with TestClient(create_app(settings)) as restarted:
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == waiting
        assert len(sent) == 1 and not executed
        ready.add(entries[0])
        if crash_on_ready:
            factory = restarted.app.state.session_factory
            outbox_ids = select(OutboxEvent.id).where(OutboxEvent.run_id == run_id)
            with factory() as db:
                revision = db.get(WorkflowRun, run_id).revision
                deliveries = set(db.scalars(outbox_ids))
            accept_tool = PiAgent.accept_tool

            def crash_after_tool(bridge, feedback):
                child = bridge._process
                assert child is not None and child.poll() is None
                child.kill()
                child.wait(timeout=10)
                return accept_tool(bridge, feedback)

            with monkeypatch.context() as patch:
                patch.setattr(PiAgent, "accept_tool", crash_after_tool)
                with pytest.raises(PiRuntimeError):
                    resume(restarted, settings, waiting.loop_id, actor, registry)
            assert executed == entries[:1] and len(sent) == 1
            assert queue_ready_external(factory, settings) == 0
            with factory() as db:
                loop = db.get(AgentLoop, waiting.loop_id)
                assert loop.status == "RUNNING" and loop.outcome_json is None
                assert loop.lease_token is None
                assert db.get(WorkflowRun, run_id).revision == revision
                assert set(db.scalars(outbox_ids)) == deliveries
        second = resume(restarted, settings, waiting.loop_id, actor, registry)
        assert second.status == "WAITING_EXTERNAL"
        assert second.waiting_for.arguments == {"entry_id": str(entries[1])}
        assert executed == entries[:1] and len(sent) == 1
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == second
        ready.add(entries[1])
        completed = resume(restarted, settings, waiting.loop_id, actor, registry)
        assert completed.status == "COMPLETED" and completed.model_turns == 2
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == completed
    assert executed == entries and len(sent) == 2
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(AgentToolInvocation)))) == 2
        assert (
            len(list(db.scalars(select(AgentLoopAction).where(AgentLoopAction.kind == "WAIT"))))
            == 2
        )
