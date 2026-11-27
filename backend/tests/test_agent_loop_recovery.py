"""Persistent Pi sessions, journals and gateway tested through the real HTTP boundary."""

import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from langgraph.checkpoint.base import empty_checkpoint
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from test_agent_model import FinalResult, call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, rows_for, seed_historical_control, setup_loop, tool_feedback
from test_agent_tools import create_entry
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres

from arclight.agent_skills import freeze_agent_skills
from arclight.application.agent_memory import read_agent_memory
from arclight.application.agent_runtime import recovery as recovery
from arclight.application.agent_runtime import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_runtime.store import (
    AgentLoopStore,
    checked_loop,
    claim_loop,
    keep_loop_alive,
    submit_agent_decision,
)
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.audit import audit
from arclight.application.context_evidence import read_trace
from arclight.application.gateway_accounting import reconcile
from arclight.application.generation_leases import LeasePolicy
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.model_gateway import GatewayUsage
from arclight.domain.versions import digest
from arclight.main import create_app
from arclight.persistence.agent_loops import (
    AgentLoop,
    AgentLoopAction,
    AgentLoopDecision,
    AgentLoopQuestion,
    AgentLoopStamp,
)
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import AgentToolGrant, AuditEvent, ProjectMember, User, WorkflowRun
from arclight.workflows.checkpoints import open_checkpointer


def start(client, settings, seed, registry, *, limits=None, **kwargs):
    if limits is not None:
        seed_historical_control(client, seed.run_id, limits)
    return run_persistent_agent_loop(
        client.app.state.session_factory, settings, seed, registry, FinalResult, **kwargs
    )


def resume(client, settings, loop_id, actor, registry, **kwargs):
    return resume_agent_loop(
        client.app.state.session_factory, settings, loop_id, actor, registry, FinalResult, **kwargs
    )


def answer(waiting, *, choice="补充", **patch):
    return AgentHumanDecision.model_validate(
        {
            "question_id": waiting.question_id,
            "input_sha256": waiting.input_sha256,
            "decision": "ANSWER",
            "answer": choice,
            **patch,
        }
    )


def loop_for(client, run_id):
    with client.app.state.session_factory() as db:
        return db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id)).id


@pytest.mark.parametrize("with_skills", [True, False], ids=["with-skills", "historical-seed"])
def test_human_question_survives_app_restart_and_answer_continues_same_thread(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
    with_skills,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        responded = any("human_decision" in item.get("content", "") for item in payload["messages"])
        return httpx.Response(200, json=final() if responded else final("HUMAN_REVIEW"))

    model_service(respond)
    with monkeypatch.context() as before_skill_loading:
        if not with_skills:
            before_skill_loading.setattr(recovery, "freeze_agent_skills", lambda request: request)
        waiting = start(client, settings, seed, registry)
    assert waiting.status == "HUMAN_REVIEW" and waiting.question_id is not None
    assert waiting.input_sha256 and waiting.loop_id
    with client.app.state.session_factory() as db:
        loop = db.get(AgentLoop, waiting.loop_id)
        assert loop.status == "WAITING_HUMAN" and loop.lease_token is None
        assert loop.thread_id == f"agent-loop:{loop.id}"
        question = db.get(AgentLoopQuestion, waiting.question_id)
        assert question.question_json == waiting.human_request.model_dump(mode="json")
        original_input = db.get(WorkflowRun, run_id).input_json
        frozen_seed = loop.seed_json
        assert loop.request_sha256 == digest(frozen_seed)
        if with_skills:
            assert [item["name"] for item in frozen_seed["skills"]] == [
                "approved-version-evidence",
                "screenwriter-revision",
            ]
            for skill in frozen_seed["skills"]:
                assert skill["instructions"] in sent[0]["messages"][0]["content"]
        else:
            assert frozen_seed == seed.model_dump(mode="json")
    monkeypatch.setattr(
        recovery, "freeze_agent_skills", lambda _: pytest.fail("Recovery must reuse frozen Skills")
    )
    # A new application, engine and official saver read only persistent state.
    with TestClient(create_app(settings)) as restarted:
        assert start(restarted, settings, seed, registry) == waiting
        still_waiting = resume(restarted, settings, waiting.loop_id, actor, registry)
        assert still_waiting == waiting and len(sent) == 1
        completed = resume(
            restarted, settings, waiting.loop_id, actor, registry, decision=answer(waiting)
        )
        assert completed.status == "COMPLETED" and completed.model_turns == 2
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == completed
    assert len(sent) == 2
    assert sent[0]["messages"][0] == sent[1]["messages"][0]
    assert "decision_id" in json.dumps(sent[-1], ensure_ascii=False)
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(AgentLoopDecision)))) == 1
        assert db.get(AgentLoop, waiting.loop_id).seed_json == frozen_seed
        assert db.get(WorkflowRun, run_id).input_json == original_input
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert db.get(WorkflowRun, run_id).pending_review is None
    assert len(rows_for(client, run_id)["actions"]) == 2


def test_turn_limit_survives_repeated_human_waits_and_restart(
    client, episode, settings, model_service, monkeypatch
):
    monkeypatch.setattr("arclight.application.agent_runtime.runtime.MAX_AGENT_LOOP_TURNS", 2)
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=final("HUMAN_REVIEW"))

    model_service(respond)
    waiting = start(client, settings, model_request(run_id), registry)
    assert waiting.status == "HUMAN_REVIEW" and waiting.model_turns == 1
    with TestClient(create_app(settings)) as restarted:
        next_wait = resume(
            restarted, settings, waiting.loop_id, actor, registry, decision=answer(waiting)
        )
        assert next_wait.status == "HUMAN_REVIEW" and next_wait.model_turns == 2
        assert next_wait.question_id != waiting.question_id
        stopped = resume(
            restarted, settings, waiting.loop_id, actor, registry, decision=answer(next_wait)
        )
        assert stopped.status == "STOPPED" and stopped.stop_code == "AGENT_MODEL_CALL_LIMIT"
        assert stopped.model_turns == len(sent) == 2
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == stopped
    assert len(rows_for(client, run_id)["receipts"]) == 2


@pytest.mark.parametrize(
    "decision, expected",
    [
        ("STOP", "AGENT_HUMAN_STOPPED"),
        ("ANSWER", None),
    ],
)
def test_human_stop_and_resume_keep_the_original_action_history(
    client,
    episode,
    settings,
    model_service,
    decision,
    expected,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []

    def answer_then_finish(request):
        sent.append(request)
        return httpx.Response(200, json=final("HUMAN_REVIEW" if len(sent) == 1 else "COMPLETE"))

    model_service(answer_then_finish)
    waiting = start(
        client, settings, model_request(run_id), registry, limits=AgentRunLimits(max_model_calls=1)
    )
    request = answer(waiting, decision=decision, answer=None if decision == "STOP" else "补充")
    result = resume(client, settings, waiting.loop_id, actor, registry, decision=request)
    assert result.stop_code == expected
    assert result.status == ("STOPPED" if decision == "STOP" else "COMPLETED")
    assert len(rows_for(client, run_id)["actions"]) == len(sent) == (1 if decision == "STOP" else 2)


def test_duplicate_decision_is_idempotent_but_conflicting_or_stale_answer_is_rejected(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    model_service(lambda request: httpx.Response(200, json=final("HUMAN_REVIEW")))
    waiting = start(client, settings, model_request(run_id), registry)
    with client.app.state.session_factory.begin() as db:
        first = submit_agent_decision(db, waiting.loop_id, actor, answer(waiting))
        repeated = submit_agent_decision(db, waiting.loop_id, actor, answer(waiting))
        assert repeated.id == first.id
    for patch, code in [
        ({"answer": "停止"}, "AGENT_DECISION_CONFLICT"),
        ({"input_sha256": "a" * 64}, "AGENT_STALE_DECISION"),
        ({"question_id": uuid4()}, "AGENT_STALE_DECISION"),
    ]:
        with pytest.raises(DomainError) as caught, client.app.state.session_factory.begin() as db:
            submit_agent_decision(db, waiting.loop_id, actor, answer(waiting, **patch))
        assert caught.value.code == code


@pytest.mark.parametrize(
    "protocol,node",
    [
        ("openai_chat", "intent"),
        ("openai_chat", "invoke_model"),
        ("openai_chat", "execute_tool"),
        ("gemini", "execute_tool"),
    ],
)
def test_reentry_after_crash_reuses_logical_model_and_tool_actions(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
    protocol,
    node,
):
    settings.llm_protocol = protocol
    run_id, actor, registry = setup_loop(client, episode, settings)
    entry = create_entry(client, episode["project_id"])
    original_tool = registry.get("read_story_entry")

    def record_effect(context, parameters):
        audit(context.db, context.project_id, context.actor_id, "fixture.agent_effect", run_id)
        return original_tool.handler(context, parameters)

    registry = AgentToolRegistry([replace(original_tool, handler=record_effect)])
    sent = []
    native_call = {
        "id": "native-read",
        "name": "read_story_entry",
        "args": {"entry_id": str(entry)},
    }
    signature = "opaque-native-signature"

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        if protocol == "gemini":
            if len(sent) == 1:
                parts = [{"functionCall": native_call, "thoughtSignature": signature}]
            else:
                history = [part for row in payload["contents"] for part in row["parts"]]
                assert {"functionCall": native_call, "thoughtSignature": signature} in history
                (feedback,) = (
                    part["functionResponse"] for part in history if "functionResponse" in part
                )
                assert (
                    feedback["id"] == native_call["id"] and feedback["name"] == native_call["name"]
                )
                assert feedback["response"]["result"]["status"] == "SUCCEEDED"
                parts = [{"text": final()["choices"][0]["message"]["content"]}]
            return httpx.Response(
                200, json={"candidates": [{"finishReason": "STOP", "content": {"parts": parts}}]}
            )
        return httpx.Response(
            200,
            json=final()
            if tool_feedback(payload)
            else call_response(("read", "read_story_entry", {"entry_id": str(entry)})),
        )

    model_service(respond)
    seed = model_request(run_id)
    if node == "intent":
        target, name = AgentLoopStore, "plan"
    else:
        target, name = recovery._PersistentRuntime, node
    original = getattr(target, name)
    crashed = False

    def crash_once(*args, **kwargs):
        nonlocal crashed
        value = original(*args, **kwargs)
        if not crashed:
            crashed = True
            raise RuntimeError("simulated process interruption")
        return value

    monkeypatch.setattr(target, name, crash_once)
    with pytest.raises(RuntimeError, match="simulated"):
        start(client, settings, seed, registry)
    if node == "intent":
        assert not sent
    if protocol == "gemini":
        with TestClient(create_app(settings)) as recovered:
            finished = resume(recovered, settings, loop_for(client, run_id), actor, registry)
    else:
        finished = resume(client, settings, loop_for(client, run_id), actor, registry)
    assert finished.status == "COMPLETED" and len(sent) == 2
    assert start(client, settings, seed, registry) == finished
    assert len(rows_for(client, run_id)["invocations"]) == 1
    assert len(rows_for(client, run_id)["actions"]) == 3
    with client.app.state.session_factory() as db:
        assert (
            len(
                list(
                    db.scalars(
                        select(AuditEvent).where(AuditEvent.action == "fixture.agent_effect")
                    )
                )
            )
            == 1
        )
        actions = list(db.scalars(select(AgentLoopAction)))
        assert len(actions) == 3 and all(row.result_json is not None for row in actions)
        assert all(row.request_sha256 for row in actions)


def test_resume_imports_stamped_legacy_batch_with_pending_checkpoint_writes(
    client, episode, settings, model_service, monkeypatch
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    entry = create_entry(client, episode["project_id"])
    sent = []

    def respond(request):
        payload = json.loads(request.content)
        sent.append(payload)
        return httpx.Response(
            200,
            json=final()
            if tool_feedback(payload)
            else call_response(("read", "read_story_entry", {"entry_id": str(entry)})),
        )

    model_service(respond)

    def write_historical_checkpoint(session, state):
        if state["phase"] != "tools":
            return
        initial = {
            "request": state["request"] | {"operation_key": seed.operation_key},
            "observed_round": state["observed_round"],
            "model_turns": 0,
        }
        changes = {"model_turns": 1, "response": state["response"]}
        session.store.stamp(initial, changes)
        config = {"configurable": {"thread_id": session.thread, "checkpoint_ns": ""}}
        snapshot = empty_checkpoint()
        snapshot["channel_values"] = initial | {"branch:to:decide": None}
        versions = {
            key: session.saver.get_next_version(None, None) for key in snapshot["channel_values"]
        }
        snapshot["channel_versions"] = versions
        written = session.saver.put(config, snapshot, {"source": "loop", "step": 1}, versions)
        session.saver.put_writes(
            written, [*changes.items(), ("branch:to:tools", None)], str(uuid4())
        )
        raise RuntimeError("historical interruption")

    with monkeypatch.context() as patch:
        patch.setattr(recovery._SessionCheckpoint, "save", write_historical_checkpoint)
        with pytest.raises(RuntimeError, match="historical interruption"):
            start(client, settings, seed, registry)
    assert len(sent) == 1
    loop_id = loop_for(client, run_id)
    result = resume(client, settings, loop_id, actor, registry)
    assert result.status == "COMPLETED" and result.model_turns == 2
    assert len(sent) == 2 and len(rows_for(client, run_id)["invocations"]) == 1
    assert len(rows_for(client, run_id)["actions"]) == 3
    with open_checkpointer(settings) as saver:
        config = {"configurable": {"thread_id": f"agent-loop:{loop_id}"}}
        assert saver.get_tuple(config) is not None
        config["configurable"]["checkpoint_ns"] = recovery._SESSION_NAMESPACE
        assert saver.get_tuple(config) is not None


@pytest.mark.parametrize("evidence", [False, True])
def test_unknown_model_result_requires_original_ledger_reconciliation_before_retry(
    client,
    episode,
    settings,
    model_service,
    evidence,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []

    def fail(request):
        sent.append(request)
        raise httpx.ReadTimeout("no trusted response")

    model_service(fail)
    stopped = start(client, settings, model_request(run_id), registry)
    assert stopped.stop_code == "GATEWAY_RECONCILIATION_REQUIRED" and len(sent) == 1
    with client.app.state.session_factory() as db:
        loop = db.get(AgentLoop, stopped.loop_id)
        assert loop.status == "RECOVERY_REQUIRED"
        call = db.scalar(select(GatewayCall).where(GatewayCall.workflow_id == run_id))
        call_id = call.id
    assert resume(client, settings, stopped.loop_id, actor, registry).stop_code == stopped.stop_code
    assert len(sent) == 1
    if evidence:
        with client.app.state.session_factory.begin() as db:
            reconcile(
                db,
                db.get(GatewayCall, call_id),
                actor,
                "NOT_ACCEPTED",
                GatewayUsage(actual_cost=Decimal("0")),
                "synthetic fixture: provider confirms no acceptance",
            )
        model_service(lambda request: sent.append(request) or httpx.Response(200, json=final()))
        recovered = resume(client, settings, stopped.loop_id, actor, registry)
        assert recovered.status == "COMPLETED", recovered.model_dump(mode="json")
        assert len(sent) == 2
        with client.app.state.session_factory() as db:
            assert len(list(db.scalars(select(AgentLoopAction)))) == 1
            assert len(list(db.scalars(select(GatewayCall)))) == 2


@pytest.mark.parametrize(
    "mutation, code",
    [
        ("disable", "FORBIDDEN"),
        ("grant", "USER_NOT_AUTHORIZED"),
        ("cancel", "AGENT_CANCELLED"),
        ("stale", "RUN_STATE_INVALID"),
    ],
)
def test_resume_rechecks_current_authority_and_frozen_state(
    client,
    episode,
    settings,
    model_service,
    mutation,
    code,
):

    run_id, actor, registry = setup_loop(client, episode, settings)
    entry = create_entry(client, episode["project_id"])
    sent = []
    model_service(
        lambda request: sent.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = start(
        client,
        settings,
        model_request(run_id),
        registry,
        limits=AgentRunLimits(max_duration_seconds=300),
    )
    with client.app.state.session_factory.begin() as db:
        if mutation == "disable":
            user = User(
                email=f"{uuid4().hex}@example.com",
                display_name="fixture",
                password_hash="fixture",
                disabled=True,
            )
            db.add(user)
            db.flush()
            actor = str(user.id)
        elif mutation == "grant":
            grant = db.scalar(select(AgentToolGrant).where(AgentToolGrant.run_id == run_id))
            grant.allowed_tools_json = []
        elif mutation == "cancel":
            db.get(WorkflowRun, run_id).status = "CANCELLED"
        elif mutation == "stale":
            run = db.get(WorkflowRun, run_id)
            run.agent_state_json = run.agent_state_json | {"input_sha256": "f" * 64}
    if mutation == "grant":
        model_service(
            lambda request: (
                sent.append(request)
                or httpx.Response(
                    200, json=call_response(("read", "read_story_entry", {"entry_id": str(entry)}))
                )
            )
        )
        result = resume(
            client, settings, waiting.loop_id, actor, registry, decision=answer(waiting)
        )
        assert result.stop_code in {code, "GATEWAY_INVALID_RESPONSE"}
        assert not rows_for(client, run_id)["invocations"]
    else:
        with pytest.raises(DomainError) as caught:
            resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
        assert caught.value.code == code
        assert len(sent) == 1


def test_operation_and_result_contract_are_frozen_and_checkpoint_cannot_inject_state(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    model_service(lambda request: httpx.Response(200, json=final("HUMAN_REVIEW")))
    waiting = start(client, settings, seed, registry)
    with pytest.raises(DomainError) as caught:
        start(client, settings, seed.model_copy(update={"prompt_version": "forged:2"}), registry)
    assert caught.value.code == "AGENT_LOOP_CONFLICT"
    with open_checkpointer(settings) as saver:
        config = {
            "configurable": {
                "thread_id": f"agent-loop:{waiting.loop_id}",
                "checkpoint_ns": recovery._SESSION_NAMESPACE,
            }
        }
        saved = saver.get_tuple(config)
        assert saved is not None
        channel = recovery._SESSION_CHANNEL
        state = saved.checkpoint["channel_values"][channel]
        forged = state["request"] | {"system_instruction": "forged authority"}
        version = saver.get_next_version(saved.checkpoint["channel_versions"][channel], None)
        checkpoint = empty_checkpoint()
        checkpoint["channel_values"] = {channel: state | {"request": forged}}
        checkpoint["channel_versions"] = {channel: version}
        saver.put(saved.config, checkpoint, saved.metadata, {channel: version})
    with pytest.raises(DomainError) as caught:
        resume(client, settings, waiting.loop_id, actor, registry)
    assert caught.value.code == "AGENT_CHECKPOINT_INVALID"


@pytest.mark.parametrize("dialect", ["sqlite:///unused.db", "postgresql+psycopg://unused/unused"])
def test_offline_migration_defines_frozen_threads_and_requires_online_downgrade(dialect):
    output = StringIO()
    config = Config(str(Path(__file__).resolve().parents[1] / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = dialect
    command.upgrade(config, "0045:0046", sql=True)
    assert "CREATE TABLE agent_loop_actions" in output.getvalue()
    assert "IMMUTABLE_HISTORY" in output.getvalue()
    with pytest.raises(RuntimeError, match="online history preservation"):
        command.downgrade(config, "0046:0045", sql=True)


@pytest.mark.usefixtures("legacy_execution_deadlines")
def test_durable_history_cannot_be_overwritten_deleted_or_downgraded(
    client,
    episode,
    settings,
    model_service,
    migrated,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    model_service(lambda request: httpx.Response(200, json=final("HUMAN_REVIEW")))
    waiting = start(client, settings, model_request(run_id), registry)
    with client.app.state.session_factory.begin() as db:
        submit_agent_decision(db, waiting.loop_id, actor, answer(waiting))
    for table in (AgentLoop, AgentLoopAction, AgentLoopQuestion, AgentLoopDecision, AgentLoopStamp):
        with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
            db.execute(table.__table__.delete())
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.execute(update(AgentLoop).values(input_sha256="b" * 64))
    with pytest.raises(DBAPIError), client.app.state.session_factory.begin() as db:
        db.execute(update(AgentLoopAction).values(result_json={"forged": True}))
    with pytest.raises(RuntimeError, match="Agent loop history exists"):
        command.downgrade(migrated, "0045")


def test_real_process_exit_after_response_replays_without_calling_provider_again(
    client,
    episode,
    settings,
    model_service,
    tmp_path,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    payload = tmp_path / "restart.json"
    # Isolated fixtures only; no environment file, account credential or user material.
    payload.write_text(
        json.dumps(
            {
                "settings": settings.model_dump(mode="json", exclude={"service_encryption_key"}),
                "request": seed.model_dump(mode="json"),
                "response": final(),
            }
        ),
        encoding="utf-8",
    )
    script = tmp_path / "crash.py"
    script.write_text(
        """import json, os, sys, httpx
from arclight.core.config import Settings
from arclight.persistence.session import build_engine, session_factory
from arclight.application.agent_runtime import recovery as r
from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.generation_leases import LeasePolicy
from arclight.domain.agent_model import AgentModelRequest
from arclight.providers import llm_transport
from pydantic import BaseModel, ConfigDict, Field
class FinalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    requires_human_review: bool
data = json.load(open(sys.argv[1], encoding="utf-8"))
settings = Settings(
    _env_file=None,
    service_encryption_key=os.environ["ARCLIGHT_TEST_AGENT_RECOVERY_KEY"],
    **data["settings"],
)
llm_transport.PublicHTTPTransport = lambda *a, **kw: httpx.MockTransport(
    lambda req: httpx.Response(200, json=data["response"]))
original = r._PersistentRuntime.invoke_model
def crash(self, request, turn):
    original(self, request, turn)
    os._exit(51)
r._PersistentRuntime.invoke_model = crash
r.run_persistent_agent_loop(session_factory(build_engine(settings), settings), settings,
    AgentModelRequest.model_validate(data["request"]), build_agent_tool_registry(settings=settings),
    FinalResult, lease_policy=LeasePolicy(duration_seconds=3, heartbeat_seconds=.2))
""",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(script), str(payload)],
        capture_output=True,
        text=True,
        timeout=45,
        env={
            **os.environ,
            "ARCLIGHT_TEST_AGENT_RECOVERY_KEY": settings.service_encryption_key.get_secret_value(),
        },
    )
    assert process.returncode == 51, process.stderr
    loop_id = loop_for(client, run_id)
    with client.app.state.session_factory() as db:
        action = db.scalars(
            select(AgentLoopAction).where(
                AgentLoopAction.loop_id == loop_id, AgentLoopAction.kind == "MODEL"
            )
        ).one()
        assert action.result_json is not None, "child did not commit a model result"
        assert action.result_json["final"]["decision"] == "COMPLETE"
        receipt = db.scalars(
            select(GatewayReceipt)
            .join(GatewayCall)
            .where(GatewayCall.id == action.gateway_call_id, GatewayCall.workflow_id == run_id)
        ).one()
        assert receipt.status == "SUCCEEDED"
    time.sleep(3.2)
    model_service(lambda request: pytest.fail("a committed model result must never be resent"))
    result = resume(client, settings, loop_id, actor, registry)
    assert result.status == "COMPLETED" and result.model_turns == 1
    assert len(rows_for(client, run_id)["actions"]) == 1
    with client.app.state.session_factory() as db:
        assert db.scalar(select(GatewayReceipt)).status == "SUCCEEDED"


def test_postgres_parallel_resume_is_fenced_and_model_io_holds_no_business_lock(
    client,
    episode,
    settings,
    model_service,
):
    require_postgres(settings)
    run_id, actor, registry = setup_loop(client, episode, settings)
    seed = model_request(run_id)
    model_service(lambda request: httpx.Response(200, json=final("HUMAN_REVIEW")))
    waiting = start(client, settings, seed, registry)
    sent = []

    def respond(request):
        sent.append(request)
        with client.app.state.session_factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '1500ms'"))
            # An independent real transaction can lock and update the owning run mid-I/O.
            run = db.scalar(select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update())
            run.revision += 1
        with pytest.raises(DomainError) as caught:
            resume(client, settings, waiting.loop_id, actor, registry)
        assert caught.value.code == "AGENT_LOOP_BUSY"
        return httpx.Response(200, json=final())

    model_service(respond)
    completed = resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
    assert completed.status == "COMPLETED" and len(sent) == 1


def test_execution_heartbeat_and_expired_owner_are_checked(
    client, episode, settings, model_service
):
    run_id, _, registry = setup_loop(client, episode, settings)
    model_service(lambda request: httpx.Response(200, json=final("HUMAN_REVIEW")))
    waiting = start(client, settings, model_request(run_id), registry)
    factory = client.app.state.session_factory
    policy = LeasePolicy(duration_seconds=1, heartbeat_seconds=0.1)
    with factory.begin() as db:
        loop = checked_loop(db, waiting.loop_id)
        old = claim_loop(db, loop, policy)
    store = AgentLoopStore(factory, waiting.loop_id, old)
    with keep_loop_alive(store, policy):
        time.sleep(1.2)
        with factory.begin() as db:
            assert store.guard(db).lease_expires_at is not None
    with factory.begin() as db:
        loop = checked_loop(db, waiting.loop_id)
        replacement = claim_loop(db, loop)
    assert replacement != old
    with pytest.raises(DomainError) as caught, factory.begin() as db:
        store.guard(db)
    assert caught.value.code == "AGENT_EXECUTION_LOST"


def test_response_without_committed_receipt_stays_unknown_and_is_not_resent(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []
    model_service(lambda request: sent.append(request) or httpx.Response(200, json=final()))

    def crash(*args):
        raise RuntimeError("crash before response transaction commit")

    monkeypatch.setattr(AgentLoopStore, "save_model", crash)
    with pytest.raises(RuntimeError, match="before response transaction"):
        start(client, settings, model_request(run_id), registry)
    with client.app.state.session_factory() as db:
        assert db.scalar(select(GatewayCall)) is not None
        assert db.scalar(select(GatewayReceipt)) is None
        assert db.scalar(select(AgentLoopAction)).result_json is None
    stopped = resume(client, settings, loop_for(client, run_id), actor, registry)
    assert stopped.stop_code == "GATEWAY_RECONCILIATION_REQUIRED" and len(sent) == 1


def test_resume_beyond_old_budget_preserves_pending_cost_and_historical_control(
    client, episode, settings, model_service
):
    from arclight.persistence.agent_limits import AgentRunLimit

    run_id, actor, registry = setup_loop(client, episode, settings, quote="1")
    sent = []

    def respond(request):
        sent.append(request)
        return httpx.Response(200, json=final("HUMAN_REVIEW" if len(sent) == 1 else "COMPLETE"))

    model_service(respond)
    waiting = start(
        client, settings, model_request(run_id), registry, limits=AgentRunLimits(maximum_cost="1")
    )
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    result = resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
    assert result.status == "COMPLETED" and len(sent) == 2
    assert rows_for(client, run_id)["pending"] == 2
    with client.app.state.session_factory() as db:
        assert db.get(AgentRunLimit, run_id).deadline_at == deadline


@pytest.mark.parametrize("revocation", ["disable_initiator", "remove_membership"])
def test_current_initiator_authority_is_required_even_for_another_resume_actor(
    client,
    settings,
    model_service,
    revocation,
):
    owner = register(client)
    project = create_project(client)
    episode = create_episode(client, project)
    editor = register(client, "editor-recovery@example.com")
    actor_id = UUID(editor["user"]["id"])
    with client.app.state.session_factory.begin() as db:
        db.add(ProjectMember(project_id=UUID(project["id"]), user_id=actor_id, role="editor"))
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []
    model_service(
        lambda request: sent.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = start(client, settings, model_request(run_id), registry)
    assert waiting.status == "HUMAN_REVIEW"
    with client.app.state.session_factory.begin() as db:
        if revocation == "disable_initiator":
            db.get(User, actor_id).disabled = True
        else:
            db.execute(ProjectMember.__table__.delete().where(ProjectMember.user_id == actor_id))
    with pytest.raises(DomainError) as caught:
        resume(
            client,
            settings,
            waiting.loop_id,
            owner["user"]["id"],
            registry,
            decision=answer(waiting),
        )
    assert caught.value.code == ("FORBIDDEN" if revocation == "disable_initiator" else "NOT_FOUND")
    assert len(sent) == 1


def test_human_answer_keeps_large_context_and_frozen_evidence(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    with client.app.state.session_factory() as db:
        memory = read_agent_memory(db, db.get(WorkflowRun, run_id), "script", actor)
    source = {"padding": "x" * 262144}
    seed = model_request(run_id, messages=[AgentUserMessage(data=source)])
    sent = []

    def respond(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=final("HUMAN_REVIEW" if len(sent) == 1 else "COMPLETE"))

    model_service(respond)
    waiting = start(client, settings, seed, registry)
    assert waiting.status == "HUMAN_REVIEW"
    completed = resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
    assert completed.status == "COMPLETED" and completed.model_turns == 2 and len(sent) == 2
    delivered_memory = []
    for request in sent:
        user_data = [
            json.loads(item["content"])["data"]
            for item in request["messages"]
            if item["role"] == "user"
        ]
        assert source in user_data
        projected = next(item for item in user_data if "observation" in item)
        assert projected["observation"] == memory["observation"]
        assert {key: value for key, value in projected["memory"].items() if key != "working"} == {
            key: value for key, value in memory["memory"].items() if key != "working"
        }
        delivered_memory.append(projected)
    assert delivered_memory[0] == delivered_memory[1]
    with client.app.state.session_factory() as db:
        assert db.get(AgentLoop, waiting.loop_id).seed_json == freeze_agent_skills(seed).model_dump(
            mode="json"
        )
        assert read_trace(db, db.get(WorkflowRun, run_id), "script", actor) == memory["observation"]
