import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from test_agent_loop_recovery import answer, loop_for, resume, start
from test_agent_model import call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, rows_for, setup_loop, tool_feedback
from test_agent_tools import create_entry

from arclight.application.pi_agent import PiAgent, PiRuntimeError
from arclight.main import create_app
from arclight.persistence.agent_loops import AgentLoopAction, AgentLoopDecision


def test_pi_process_death_reuses_settled_turn_and_resumes_tools_and_human_answer(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    entries = [create_entry(client, episode["project_id"]) for _ in range(2)]
    provider_ids = ["pi-read-first", "pi-read-second"]
    tool_calls = tuple(
        (identity, "read_story_entry", {"entry_id": str(entry)})
        for identity, entry in zip(provider_ids, entries, strict=True)
    )
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if len(sent) == 1:
            response = call_response(*tool_calls)
        elif len(sent) == 2:
            response = final("HUMAN_REVIEW")
        elif len(sent) == 3:
            response = final()
        else:
            pytest.fail("Pi repeated a settled model request")
        return httpx.Response(200, json=response)

    model_service(respond)
    accept_model = PiAgent.accept_model
    next_tool = PiAgent.next_tool
    accepted = []
    dispatched = []
    stopped = False

    def interrupt_after_receipt(bridge, response, tools, usage):
        nonlocal stopped
        if not stopped:
            stopped = True
            child = bridge._process
            assert child is not None and child.poll() is None
            child.kill()
            child.wait(timeout=10)
        result = accept_model(bridge, response, tools, usage)
        accepted.append(response.status)
        return result

    def record_tool_dispatch(bridge, pending):
        call = next_tool(bridge, pending)
        dispatched.append(call.call_id)
        return call

    monkeypatch.setattr(PiAgent, "accept_model", interrupt_after_receipt)
    monkeypatch.setattr(PiAgent, "next_tool", record_tool_dispatch)

    with pytest.raises(PiRuntimeError):
        start(client, settings, seed, registry)
    loop_id = loop_for(client, run_id)
    before = rows_for(client, run_id)
    assert stopped and len(sent) == 1
    assert not before["invocations"]
    assert [status for status, _, _ in before["receipts"]] == ["SUCCEEDED"]
    with client.app.state.session_factory() as db:
        action = db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop_id)).one()
        assert action.kind == "MODEL" and action.result_json["status"] == "TOOL_CALLS"

    waiting = resume(client, settings, loop_id, actor, registry)
    assert waiting.status == "HUMAN_REVIEW" and waiting.loop_id == loop_id
    assert dispatched == provider_ids
    assert [item["tool_call_id"] for item in sent[1]["messages"] if item["role"] == "tool"] == (
        provider_ids
    )
    assert [item["status"] for item in tool_feedback(sent[1])] == ["SUCCEEDED", "SUCCEEDED"]

    decision = answer(waiting)
    with TestClient(create_app(settings)) as restarted:
        assert resume(restarted, settings, loop_id, actor, registry) == waiting
        assert len(sent) == 2
        completed = resume(restarted, settings, loop_id, actor, registry, decision=decision)
        assert resume(restarted, settings, loop_id, actor, registry) == completed

    assert completed.status == "COMPLETED" and completed.model_turns == 3
    assert completed.loop_id == loop_id and completed.input_sha256 == waiting.input_sha256
    assert all(
        call in sent[2]["messages"] for call in sent[1]["messages"] if call["role"] == "tool"
    )
    decision_message = sent[2]["messages"][-1]
    assert decision_message["role"] == "user"
    sent_decision = json.loads(decision_message["content"])["data"]["human_decision"]
    assert len(sent) == 3 and dispatched == provider_ids
    assert accepted == ["TOOL_CALLS", "FINAL", "FINAL"]
    after = rows_for(client, run_id)
    assert [status for status, _, _ in after["receipts"]] == ["SUCCEEDED"] * 3
    assert [item[3] for item in after["invocations"]] == ["SUCCEEDED", "SUCCEEDED"]
    with client.app.state.session_factory() as db:
        actions = list(
            db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop_id))
        )
        assert sorted(item.kind for item in actions) == ["MODEL", "MODEL", "MODEL", "TOOL", "TOOL"]
        assert all(item.result_json is not None for item in actions)
        saved_decision = db.scalars(select(AgentLoopDecision)).one()
        assert waiting.human_request is not None
        assert sent_decision == {
            "decision_id": str(saved_decision.id),
            "actor_id": actor,
            "question": waiting.human_request.model_dump(mode="json"),
            **decision.model_dump(mode="json"),
        }
        assert saved_decision.decision_json == decision.model_dump(mode="json")
