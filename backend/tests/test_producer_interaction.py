"""Producer UI commands use the real durable runtime and isolated SQLite fixtures."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from test_agent_model import model_service as model_service
from test_producer_planning import install_plan_model, writer_task
from test_producer_replanning import install_replan_model

from arclight.application import producer_execution as execution
from arclight.application.agent_workflows import WorkflowExecutionDeferred
from arclight.application.producer_interaction import (
    command_receipt,
    command_rows,
    queue_answer,
    queue_command,
    start_interactive_producer,
)
from arclight.application.producer_progress import read_progress
from arclight.application.runtime import execute_run
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.model_gateway import GatewayPolicy
from arclight.domain.producer_interaction import (
    ProducerCancelCommand,
    ProducerDelegateCommand,
    ProducerRecoverCommand,
    ProducerReplanCommand,
    ProducerRunCreate,
)
from arclight.domain.producer_planning import ProducerPlan
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.base import utcnow
from arclight.persistence.models import Episode, OutboxEvent, Project, WorkflowRun


def begin(client, episode, settings, *, key="start-producer", **payload):
    settings.model_gateway_policy = GatewayPolicy(text_maximum_cost="0", pricing_version="prod04:1")
    factory = client.app.state.session_factory
    with factory.begin() as db:
        actor = db.get(Project, UUID(episode["project_id"])).owner_id
        run = start_interactive_producer(
            db,
            settings,
            UUID(episode["id"]),
            actor,
            ProducerRunCreate(goal="制作雨夜纸船故事", **payload),
            key,
            "prod04-test",
        )
        return run.id, actor, run.revision


def progress(client, run_id, actor, settings):
    with client.app.state.session_factory() as db:
        return read_progress(db, run_id, actor, settings)


def planned(client, episode, settings, model_service, *, tasks=None):
    sent, errors = install_plan_model(model_service, tasks=tasks)
    run_id, actor, revision = begin(client, episode, settings)
    execute_run(client.app.state.session_factory, settings, run_id, revision)
    state = progress(client, run_id, actor, settings)
    assert not errors and len(state["plans"]) == 1, state
    return run_id, actor, state, sent


def delegate(plan, revision, task_key="write"):
    return ProducerDelegateCommand(
        action="DELEGATE",
        expected_revision=revision,
        plan_id=plan["plan_id"],
        plan_sha256=plan["plan_sha256"],
        task_key=task_key,
    )


def test_start_queues_exactly_one_plan_and_freezes_original_inputs(client, episode, settings):
    run_id, actor, revision = begin(client, episode, settings)
    assert revision == 1
    with client.app.state.session_factory() as db:
        original = digest(db.get(WorkflowRun, run_id).input_json)
    again, again_actor, again_revision = begin(client, episode, settings)
    assert (again, again_actor, again_revision) == (run_id, actor, revision)
    with client.app.state.session_factory() as db:
        run = db.get(WorkflowRun, run_id)
        rows = command_rows(db, run, UUID(episode["project_id"]))
        assert len(rows) == 1 and rows[0].snapshot_json["action"] == "PLAN"
        assert run.status == "RUNNING" and digest(run.input_json) == original
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
        control = db.get(AgentRunLimit, run_id)
        assert control.limits_json == {} and control.deadline_at is None


def test_runtime_plan_replay_does_not_repeat_model_or_billing(
    client, episode, settings, model_service
):
    run_id, actor, state, sent = planned(client, episode, settings, model_service)
    usage = state["budget"]
    execute_run(client.app.state.session_factory, settings, run_id, state["revision"])
    replay = progress(client, run_id, actor, settings)
    assert replay["budget"] == usage and len(sent) == 2
    assert replay["commands"][0]["status"] == "COMMITTED"
    assert replay["pending_command"] is None and replay["plans"][0]["tasks"][0]["can_delegate"]


def test_pending_plan_blocks_conflicting_commands(client, episode, settings):
    run_id, actor, revision = begin(client, episode, settings)
    with client.app.state.session_factory.begin() as db:
        with pytest.raises(DomainError) as rejected:
            queue_command(
                db,
                settings,
                run_id,
                actor,
                delegate({"plan_id": uuid4(), "plan_sha256": "a" * 64}, revision),
                "next",
            )
        # Invalid plan authority must not bypass an existing queued command or publish a child.
        assert rejected.value.code in {"PRODUCER_PLAN_INVALID", "PRODUCER_COMMAND_PENDING"}
        assert db.get(WorkflowRun, run_id).revision == revision


def test_explicit_recovery_requeues_original_revision_and_is_idempotent(
    client,
    episode,
    settings,
    model_service,
):
    sent, errors = install_plan_model(model_service)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        outbox = db.scalar(select(OutboxEvent).where(OutboxEvent.run_id == run_id))
        outbox_id = outbox.id
        outbox.published_at = utcnow()
    pending = progress(client, run_id, actor, settings)["pending_command"]
    request = ProducerRecoverCommand(
        action="RECOVER", expected_revision=revision, command_id=pending["command_id"]
    )
    with factory.begin() as db:
        queue_command(db, settings, run_id, actor, request, "recover-original")
        assert db.get(WorkflowRun, run_id).revision == revision
        assert db.get(OutboxEvent, outbox_id).published_at is None
    execute_run(factory, settings, run_id, revision)
    with factory.begin() as db:
        queue_command(db, settings, run_id, actor, request, "recover-original")
        assert db.scalar(select(func.count()).select_from(OutboxEvent)) == 1
    assert not errors and len(sent) == 2
    assert progress(client, run_id, actor, settings)["commands"][0]["status"] == "COMMITTED"


def test_crash_after_plan_commit_replays_without_new_calls(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
):
    sent, errors = install_plan_model(model_service)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    record = execution.record_receipt

    def lost_receipt(*args, **kwargs):
        raise RuntimeError("simulated worker loss before command receipt")

    monkeypatch.setattr(execution, "record_receipt", lost_receipt)
    with pytest.raises(RuntimeError, match="simulated worker loss"):
        execute_run(factory, settings, run_id, revision)
    assert len(sent) == 2
    state = progress(client, run_id, actor, settings)
    assert len(state["plans"]) == 1 and state["pending_command"]
    monkeypatch.setattr(execution, "record_receipt", record)
    execute_run(factory, settings, run_id, revision)
    assert not errors and len(sent) == 2
    assert progress(client, run_id, actor, settings)["pending_command"] is None


def test_concurrent_revision_commands_queue_only_one_child_intent(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, state, _ = planned(
        client,
        episode,
        settings,
        model_service,
        tasks=[writer_task("first"), writer_task("second")],
    )
    factory = client.app.state.session_factory

    def enqueue(task_key):
        try:
            with factory.begin() as db:
                queue_command(
                    db,
                    settings,
                    run_id,
                    actor,
                    delegate(state["plans"][0], state["revision"], task_key),
                    task_key,
                )
            return "QUEUED"
        except DomainError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(enqueue, ("first", "second")))
    assert sorted(results) == ["QUEUED", "REVISION_CONFLICT"]
    current = progress(client, run_id, actor, settings)
    assert len(current["commands"]) == 2 and current["pending_command"]["action"] == "DELEGATE"
    execute_run(factory, settings, run_id, current["revision"])
    replay = progress(client, run_id, actor, settings)
    assert sum(task["child_run_id"] is not None for task in replay["plans"][0]["tasks"]) == 1
    execute_run(factory, settings, run_id, current["revision"])
    assert progress(client, run_id, actor, settings)["budget"]["delegations"] == 1


@pytest.mark.parametrize("stop", [False, True])
def test_exact_human_answer_resumes_or_permanently_stops_producer(
    client,
    episode,
    settings,
    model_service,
    stop,
):
    sent, errors = install_plan_model(model_service, human=True)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    execute_run(factory, settings, run_id, revision)
    waiting = progress(client, run_id, actor, settings)
    operation = waiting["operations"][0]
    assert waiting["commands"][0]["status"] == "WAITING_HUMAN" and operation["can_answer"]
    decision = AgentHumanDecision(
        question_id=operation["question_id"],
        input_sha256=operation["input_sha256"],
        decision="STOP" if stop else "ANSWER",
        answer=None if stop else operation["human_request"]["options"][0],
    )
    with factory.begin() as db:
        queued = queue_answer(db, run_id, UUID(operation["loop_id"]), actor, decision)
        assert queued.status == "RUNNING"
        answer_revision = queued.revision
    with factory.begin() as db:
        assert queue_answer(db, run_id, UUID(operation["loop_id"]), actor, decision).revision == (
            answer_revision
        )
    execute_run(factory, settings, run_id, answer_revision)
    completed = progress(client, run_id, actor, settings)
    assert not errors
    assert len(sent) == (2 if stop else 3)
    assert completed["commands"][-1]["status"] == ("STOPPED" if stop else "COMMITTED")
    assert completed["status"] == ("CANCELLED" if stop else "RUNNING")
    assert completed["can_cancel"] is not stop
    execute_run(factory, settings, run_id, answer_revision)
    assert len(sent) == (2 if stop else 3)


def test_delegation_requires_successful_dependencies_and_cancel_preserves_child(
    client,
    episode,
    settings,
    model_service,
):
    run_id, actor, state, _ = planned(
        client,
        episode,
        settings,
        model_service,
        tasks=[writer_task("first"), writer_task("second", depends_on=("first",))],
    )
    tasks = state["plans"][0]["tasks"]
    assert tasks[0]["can_delegate"] and not tasks[1]["can_delegate"]
    assert tasks[1]["error_code"] == "PRODUCER_PREREQUISITE_PENDING"
    factory = client.app.state.session_factory
    with factory.begin() as db:
        run = queue_command(
            db,
            settings,
            run_id,
            actor,
            delegate(state["plans"][0], state["revision"], "first"),
            "delegate",
        )
        delegated_revision = run.revision
    execute_run(factory, settings, run_id, delegated_revision)
    current = progress(client, run_id, actor, settings)
    child_id = UUID(current["plans"][0]["tasks"][0]["child_run_id"])
    with factory.begin() as db:
        child_status = db.get(WorkflowRun, child_id).status
        cancelled = queue_command(
            db,
            settings,
            run_id,
            actor,
            ProducerCancelCommand(action="CANCEL", expected_revision=current["revision"]),
            "stop-root",
        )
        assert cancelled.status == "CANCELLED"
        assert db.get(WorkflowRun, child_id).status == child_status
    stopped = progress(client, run_id, actor, settings)
    assert not stopped["can_operate"] and not stopped["can_cancel"]
    assert stopped["plans"][0]["tasks"][0]["child_run_id"] == str(child_id)
    # Late deliveries cannot enter the legacy workflow graph or create another child.
    execute_run(factory, settings, run_id, delegated_revision)
    assert progress(client, run_id, actor, settings)["budget"]["delegations"] == 1


def test_archive_between_queue_and_delivery_records_safe_failure_without_provider(
    client,
    episode,
    settings,
    model_service,
):
    sent, errors = install_plan_model(model_service)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    with factory.begin() as db:
        db.get(Episode, UUID(episode["id"])).status = "ARCHIVED"
    execute_run(factory, settings, run_id, revision)
    state = progress(client, run_id, actor, settings)
    assert not errors and not sent
    assert state["commands"][0]["status"] == "FAILED"
    assert state["commands"][0]["error_code"] == "EPISODE_ARCHIVED"
    assert not state["can_operate"] and state["pending_command"] is None
    assert "seed_json" not in str(state) and "private-agent-transport-key" not in str(state)


@pytest.mark.parametrize(
    "failure", ["lost_exception", "lost_outcome", "technical", "unknown_outcome"]
)
def test_recoverable_execution_failure_preserves_original_intent(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
    failure,
):
    sent, errors = install_plan_model(model_service)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    initial = progress(client, run_id, actor, settings)
    command_id = UUID(initial["pending_command"]["command_id"])
    original = execution.plan_producer

    def interrupted(*args, **kwargs):
        if failure == "lost_exception":
            raise DomainError("AGENT_EXECUTION_LOST", "原执行器已失去租约", 409)
        if failure == "technical":
            raise RuntimeError("simulated checkpoint connection interruption")
        return AgentLoopResult[ProducerPlan](
            run_id=run_id,
            project_id=UUID(episode["project_id"]),
            agent_name="producer",
            stage="producer",
            status="STOPPED",
            model_turns=0,
            stop_code="GATEWAY_RECONCILIATION_REQUIRED"
            if failure == "unknown_outcome"
            else "AGENT_EXECUTION_LOST",
        )

    monkeypatch.setattr(execution, "plan_producer", interrupted)
    if failure == "unknown_outcome":
        execute_run(factory, settings, run_id, revision)
    else:
        with pytest.raises(WorkflowExecutionDeferred):
            execute_run(factory, settings, run_id, revision)
    with factory() as db:
        assert command_receipt(db, UUID(episode["project_id"]), command_id) is None
        assert db.get(WorkflowRun, run_id).revision == revision
    assert not sent
    with factory.begin() as db:
        queue_command(
            db,
            settings,
            run_id,
            actor,
            ProducerRecoverCommand(
                action="RECOVER", expected_revision=revision, command_id=command_id
            ),
            "resume-original",
        )
    monkeypatch.setattr(execution, "plan_producer", original)
    execute_run(factory, settings, run_id, revision)
    assert not errors and len(sent) == 2
    completed = progress(client, run_id, actor, settings)
    assert completed["commands"][0]["command_id"] == str(command_id)
    assert completed["commands"][0]["status"] == "COMMITTED"
    assert completed["revision"] == revision


def test_lost_worker_cannot_close_command_while_successor_holds_real_loop_lease(
    client,
    episode,
    settings,
    model_service,
    monkeypatch,
):
    old_entered, successor_entered, release_successor = Event(), Event(), Event()

    def provider_entered(payload):
        successor_entered.set()
        assert release_successor.wait(15), "Successor provider was not released"

    sent, errors = install_plan_model(model_service, on_http=provider_entered)
    run_id, actor, revision = begin(client, episode, settings)
    factory = client.app.state.session_factory
    initial = progress(client, run_id, actor, settings)
    command_id = UUID(initial["pending_command"]["command_id"])
    original = execution.plan_producer

    def worker_delivery(*args, **kwargs):
        if not old_entered.is_set():
            old_entered.set()
            assert successor_entered.wait(15), "Successor did not acquire its real loop"
            raise DomainError("AGENT_EXECUTION_LOST", "旧执行器已失权", 409)
        return original(*args, **kwargs)

    monkeypatch.setattr(execution, "plan_producer", worker_delivery)
    with ThreadPoolExecutor(max_workers=2) as pool:
        old = pool.submit(execute_run, factory, settings, run_id, revision)
        assert old_entered.wait(15), "Old delivery did not enter"
        successor = pool.submit(execute_run, factory, settings, run_id, revision)
        try:
            with pytest.raises(WorkflowExecutionDeferred):
                old.result(timeout=15)
            with factory() as db:
                assert command_receipt(db, UUID(episode["project_id"]), command_id) is None
                from arclight.persistence.agent_loops import AgentLoop

                loop = db.scalar(select(AgentLoop).where(AgentLoop.run_id == run_id))
                assert loop is not None and loop.lease_token is not None
        finally:
            release_successor.set()
        successor.result(timeout=15)
    assert not errors and len(sent) == 2
    completed = progress(client, run_id, actor, settings)
    assert completed["commands"][0]["status"] == "COMMITTED"
    assert completed["budget"]["model_calls"] == 2


@pytest.mark.parametrize("resolution", ["ANSWER", "CANCEL"])
def test_real_replan_question_blocks_api_and_application_new_work_until_resolved(
    client,
    episode,
    settings,
    model_service,
    resolution,
):
    run_id, actor, state, _ = planned(client, episode, settings, model_service)
    factory = client.app.state.session_factory
    sent, errors = install_replan_model(model_service, human=True)
    plan = state["plans"][0]
    request = ProducerReplanCommand(
        action="REPLAN",
        expected_revision=state["revision"],
        observation={
            "base_plan_id": plan["plan_id"],
            "base_plan_sha256": plan["plan_sha256"],
            "trigger": "RECHECK",
            "reason": "先确认当前制作方向再继续任务",
        },
    )
    with factory.begin() as db:
        queued = queue_command(db, settings, run_id, actor, request, "ask-replan")
        revision = queued.revision
    execute_run(factory, settings, run_id, revision)
    waiting = progress(client, run_id, actor, settings)
    question = next(item for item in waiting["operations"] if item["can_answer"])
    assert question["kind"] == "replan" and waiting["pending_command"] is None
    assert not waiting["can_replan"]
    assert all(not task["can_delegate"] for plan in waiting["plans"] for task in plan["tasks"])
    assert len(sent) == 2 and not errors
    for blocked in (
        delegate(plan, revision),
        request.model_copy(update={"expected_revision": revision}),
    ):
        with factory.begin() as db:
            with pytest.raises(DomainError) as rejected:
                queue_command(db, settings, run_id, actor, blocked, "application-blocked")
            assert rejected.value.code == "PRODUCER_QUESTION_PENDING"
        response = client.post(
            f"/api/v1/runs/{run_id}/producer/commands",
            json=blocked.model_dump(mode="json"),
            headers={"Idempotency-Key": "api-blocked-" + blocked.action.lower()},
        )
        assert response.status_code == 409, response.text
        assert response.json()["error"]["code"] == "PRODUCER_QUESTION_PENDING"
    with factory.begin() as db:
        assert len(command_rows(db, db.get(WorkflowRun, run_id), UUID(episode["project_id"]))) == 2
        if resolution == "CANCEL":
            completed = queue_command(
                db,
                settings,
                run_id,
                actor,
                ProducerCancelCommand(action="CANCEL", expected_revision=revision),
                "cancel-question",
            )
            assert completed.status == "CANCELLED"
        else:
            decision = AgentHumanDecision(
                question_id=question["question_id"],
                input_sha256=question["input_sha256"],
                decision="ANSWER",
                answer=question["human_request"]["options"][0],
            )
            revision = queue_answer(db, run_id, UUID(question["loop_id"]), actor, decision).revision
    if resolution == "ANSWER":
        execute_run(factory, settings, run_id, revision)
        completed = progress(client, run_id, actor, settings)
        assert len(completed["plans"]) == 2 and completed["commands"][-1]["status"] == "COMMITTED"
        assert not errors and len(sent) == 3
