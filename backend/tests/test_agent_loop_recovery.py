"""ARUN-05 exercises the real persistent graph, journals and gateway, with HTTP Fakes."""

import json
import subprocess
import sys
import time
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from io import StringIO
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select, text, update
from sqlalchemy.exc import DBAPIError
from test_agent_model import FinalResult, call_response, model_request
from test_agent_model import model_service as model_service
from test_agent_runtime import final, rows_for, setup_loop, tool_feedback
from test_agent_tools import create_entry
from test_collaboration import create_episode, create_project, register
from test_concurrency import require_postgres
from test_generation_budget import set_policy

from arclight.application import agent_loop_recovery as recovery
from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_loop_store import (
    AgentLoopStore,
    checked_loop,
    claim_loop,
    keep_loop_alive,
    submit_agent_decision,
)
from arclight.application.agent_memory import read_agent_memory
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.audit import audit
from arclight.application.context_evidence import read_trace
from arclight.application.gateway_accounting import reconcile
from arclight.application.generation_leases import LeasePolicy
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.model_gateway import GatewayUsage
from arclight.main import create_app
from arclight.persistence.agent_loops import (
    AgentLoop,
    AgentLoopAction,
    AgentLoopDecision,
    AgentLoopQuestion,
    AgentLoopStamp,
)
from arclight.persistence.base import utcnow
from arclight.persistence.model_gateway import GatewayCall, GatewayReceipt
from arclight.persistence.models import AgentToolGrant, AuditEvent, ProjectMember, User, WorkflowRun
from arclight.workflows.checkpoints import open_checkpointer


def start(client, settings, seed, registry, **kwargs):
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


def test_human_question_survives_app_restart_and_answer_continues_same_thread(
    client,
    episode,
    settings,
    model_service,
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
    # A new application, engine and official saver read only persistent state.
    with TestClient(create_app(settings)) as restarted:
        still_waiting = resume(restarted, settings, waiting.loop_id, actor, registry)
        assert still_waiting == waiting and len(sent) == 1
        completed = resume(
            restarted, settings, waiting.loop_id, actor, registry, decision=answer(waiting)
        )
        assert completed.status == "COMPLETED" and completed.model_turns == 2
        assert resume(restarted, settings, waiting.loop_id, actor, registry) == completed
    assert len(sent) == 2
    assert "decision_id" in json.dumps(sent[-1], ensure_ascii=False)
    with client.app.state.session_factory() as db:
        assert len(list(db.scalars(select(AgentLoopDecision)))) == 1
        assert db.get(WorkflowRun, run_id).input_json == original_input
        assert db.get(WorkflowRun, run_id).status == "RUNNING"
        assert db.get(WorkflowRun, run_id).pending_review is None
    assert len(rows_for(client, run_id)["actions"]) == 2


@pytest.mark.parametrize(
    "decision, expected",
    [
        ("STOP", "AGENT_HUMAN_STOPPED"),
        ("ANSWER", "AGENT_MODEL_CALL_LIMIT"),
    ],
)
def test_human_stop_and_resume_do_not_reset_original_limits(
    client,
    episode,
    settings,
    model_service,
    decision,
    expected,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    sent = []
    model_service(
        lambda request: sent.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = start(
        client, settings, model_request(run_id), registry, limits=AgentRunLimits(max_model_calls=1)
    )
    request = answer(waiting, decision=decision, answer=None if decision == "STOP" else "补充")
    stopped = resume(client, settings, waiting.loop_id, actor, registry, decision=request)
    assert stopped.stop_code == expected and len(sent) == 1
    assert len(rows_for(client, run_id)["actions"]) == 1


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


@pytest.mark.parametrize("node", ["intent", "decide", "tools"])
def test_reentry_after_crash_reuses_logical_model_and_tool_actions(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
    node,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    entry = create_entry(client, episode["project_id"])
    original_tool = registry.get("read_story_entry")

    def record_effect(context, parameters):
        audit(context.db, context.project_id, context.actor_id, "fixture.agent_effect", run_id)
        return original_tool.handler(context, parameters)

    registry = AgentToolRegistry([replace(original_tool, handler=record_effect)])
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


@pytest.mark.parametrize("evidence", [False, True])
def test_unknown_model_result_requires_original_ledger_reconciliation_before_retry(
    client,
    episode,
    settings,
    model_service,
    evidence,
):
    run_id, actor, registry = setup_loop(client, episode, settings, attempts=2 if evidence else 1)
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
        ("deadline", "AGENT_DEADLINE_EXCEEDED"),
        ("cancel", "AGENT_CANCELLED"),
        ("stale", "RUN_STATE_INVALID"),
    ],
)
def test_resume_rechecks_current_authority_frozen_state_and_deadline(
    client,
    episode,
    settings,
    model_service,
    mutation,
    code,
    monkeypatch,
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
    if mutation == "deadline":
        from arclight.persistence.agent_limits import AgentRunLimit

        with client.app.state.session_factory() as db:
            deadline = db.get(AgentRunLimit, run_id).deadline_at
        monkeypatch.setattr(
            "arclight.application.agent_limits.utcnow",
            lambda: deadline.replace(tzinfo=utcnow().tzinfo) + timedelta(seconds=1),
        )
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
        graph = recovery.build_agent_graph(recovery._PersistentRuntime, checkpointer=saver)
        # Bind no executable driver: update_state only changes persisted channels.
        config = {"configurable": {"thread_id": f"agent-loop:{waiting.loop_id}"}}
        saved = graph.get_state(config)
        forged = saved.values["request"] | {"system_instruction": "forged authority"}
        graph.update_state(config, {"request": forged})
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
    with pytest.raises(RuntimeError, match="loop history exists"):
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
                "settings": settings.model_dump(mode="json"),
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
from arclight.application import agent_loop_recovery as r
from arclight.application.agent_tools import build_agent_tool_registry
from arclight.application.generation_leases import LeasePolicy
from arclight.domain.agent_model import AgentModelRequest
from arclight.providers import agent_model
from pydantic import BaseModel, ConfigDict, Field
class FinalResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(min_length=1, max_length=1000)
    requires_human_review: bool
data = json.load(open(sys.argv[1], encoding="utf-8"))
settings = Settings(_env_file=None, **data["settings"])
agent_model.PublicHTTPTransport = lambda *a, **kw: httpx.MockTransport(
    lambda req: httpx.Response(200, json=data["response"]))
original = r._PersistentRuntime.decide
def crash(self, state):
    original(self, state)
    os._exit(51)
r._PersistentRuntime.decide = crash
r.run_persistent_agent_loop(session_factory(build_engine(settings), settings), settings,
    AgentModelRequest.model_validate(data["request"]), build_agent_tool_registry(settings=settings),
    FinalResult, lease_policy=LeasePolicy(duration_seconds=3, heartbeat_seconds=.2))
""",
        encoding="utf-8",
    )
    process = subprocess.run(
        [sys.executable, str(script), str(payload)], capture_output=True, text=True, timeout=45
    )
    assert process.returncode == 51, process.stderr
    time.sleep(3.2)
    model_service(lambda request: pytest.fail("a committed model result must never be resent"))
    result = resume(client, settings, loop_for(client, run_id), actor, registry)
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


def test_resume_preserves_pending_cost_and_original_deadline(
    client, episode, settings, model_service
):
    from arclight.persistence.agent_limits import AgentRunLimit

    set_policy(client, episode["project_id"], "100")
    run_id, actor, registry = setup_loop(client, episode, settings, quote="1")
    sent = []
    model_service(
        lambda request: sent.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = start(
        client, settings, model_request(run_id), registry, limits=AgentRunLimits(maximum_cost="1")
    )
    with client.app.state.session_factory() as db:
        deadline = db.get(AgentRunLimit, run_id).deadline_at
    stopped = resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
    assert stopped.stop_code == "AGENT_BUDGET_EXCEEDED" and len(sent) == 1
    assert rows_for(client, run_id)["pending"] == 1
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


def test_human_answer_context_overflow_stops_without_discarding_frozen_evidence(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, registry = setup_loop(client, episode, settings)
    with client.app.state.session_factory() as db:
        memory = read_agent_memory(db, db.get(WorkflowRun, run_id), "script", actor)
    seed = model_request(run_id, messages=[AgentUserMessage(data={"padding": ""})])
    observed = AgentModelRequest.model_validate(
        seed.model_dump(mode="json")
        | {
            "messages": [item.model_dump(mode="json") for item in seed.messages]
            + [AgentUserMessage(data=memory).model_dump(mode="json")]
        }
    )
    size = len(json.dumps(observed.model_dump(mode="json"), ensure_ascii=False).encode())
    seed = seed.model_copy(
        update={"messages": [AgentUserMessage(data={"padding": "x" * (262144 - size - 100)})]}
    )
    sent = []
    model_service(
        lambda request: sent.append(request) or httpx.Response(200, json=final("HUMAN_REVIEW"))
    )
    waiting = start(client, settings, seed, registry)
    assert waiting.status == "HUMAN_REVIEW"
    stopped = resume(client, settings, waiting.loop_id, actor, registry, decision=answer(waiting))
    assert stopped.stop_code == "AGENT_CONTEXT_LIMIT" and len(sent) == 1
    with client.app.state.session_factory() as db:
        assert db.get(AgentLoop, waiting.loop_id).seed_json == seed.model_dump(mode="json")
        assert read_trace(db, db.get(WorkflowRun, run_id), "script", actor) == memory["observation"]
