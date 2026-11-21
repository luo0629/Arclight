"""Dispatch durable Producer intents through existing loops and registered tools."""

import logging
from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import execute_agent_tool
from arclight.application.agent_workflows import WorkflowExecutionDeferred
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_interaction import (
    command_receipt,
    command_rows,
    record_receipt,
    require_producer,
)
from arclight.application.producer_observation import observe_producer
from arclight.application.producer_planning import (
    plan_producer,
    producer_registry,
    resume_producer_plan,
)
from arclight.application.producer_replanning import replan_producer, resume_producer_replan
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_state import terminate_agent_state
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.producer_observation import ProducerObservationRequest
from arclight.domain.producer_planning import DELEGATE_TOOL
from arclight.domain.producer_series import REPLAN_VERSIONS
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, WorkflowRun

logger = logging.getLogger(__name__)
DEFERRED_CODES = frozenset(
    {
        "AGENT_LOOP_BUSY",
        "PRODUCER_OBSERVATION_BUSY",
        "AGENT_EXECUTION_LOST",
    }
)


def _invoke(factory: sessionmaker[Session], settings: Settings, command_id: UUID, intent: dict):
    run_id, actor, payload = UUID(intent["run_id"]), intent["actor_id"], intent["payload"]
    action = payload["action"]
    # Every delivery rechecks authority, including replay of an already completed loop.
    with factory.begin() as db:
        lock_gate(db)
        require_producer(db, run_id, actor)
        if action == "ANSWER":
            loop = db.get(AgentLoop, UUID(payload["loop_id"]))
            if loop is None or loop.run_id != run_id or loop.actor_id != actor:
                raise DomainError("PRODUCER_SCOPE_INVALID", "生产恢复循环作用范围无效", 409)
            replan = loop.seed_json.get("result_schema_version") in REPLAN_VERSIONS
        if action == "DELEGATE":
            feedback = execute_agent_tool(
                db,
                producer_registry(settings),
                "producer",
                AgentToolCall(
                    run_id=run_id,
                    tool_name=DELEGATE_TOOL,
                    idempotency_key="producer-command:" + str(command_id),
                    arguments={key: payload[key] for key in ("plan_id", "plan_sha256", "task_key")},
                ),
                step_key="producer-command:" + str(command_id),
            )
            if feedback.error is not None and feedback.error.code in DEFERRED_CODES:
                raise WorkflowExecutionDeferred("Producer tool requires its original delivery")
            if (
                feedback.error is not None
                and feedback.error.code == "GATEWAY_RECONCILIATION_REQUIRED"
            ):
                return None, None
            return (
                ("COMMITTED", None)
                if feedback.status == "SUCCEEDED"
                else (
                    "FAILED",
                    feedback.error.code if feedback.error else "PRODUCER_COMMAND_FAILED",
                )
            )
    outcome: AgentLoopResult
    if action == "PLAN":
        outcome = plan_producer(factory, settings, run_id, actor)
    elif action == "REPLAN":
        observation = observe_producer(
            factory,
            settings,
            run_id,
            actor,
            str(command_id),
            ProducerObservationRequest.model_validate(payload["observation"]),
        )
        outcome = replan_producer(factory, settings, run_id, actor, observation.id)
    elif action == "ANSWER":
        resume = resume_producer_replan if replan else resume_producer_plan
        outcome = resume(
            factory,
            settings,
            UUID(payload["loop_id"]),
            actor,
            decision=AgentHumanDecision.model_validate(payload["decision"]),
        )
    else:
        raise DomainError("PRODUCER_COMMAND_INVALID", "生产命令类型无效", 409)
    if outcome.stop_code in DEFERRED_CODES:
        raise WorkflowExecutionDeferred("Producer loop requires its original delivery")
    if outcome.status == "COMPLETED":
        return "COMMITTED", None
    if outcome.status == "HUMAN_REVIEW":
        return "WAITING_HUMAN", None
    if (
        outcome.status == "WAITING_EXTERNAL"
        or outcome.stop_code == "GATEWAY_RECONCILIATION_REQUIRED"
    ):
        # Keep the original intent pending; explicit recovery republishes its original revision.
        return None, None
    return "STOPPED", outcome.stop_code


def execute_producer(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> bool:
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, run_id)
        if run is None or run.workflow_type != "producer":
            return False
        # A stale/cancelled Producer delivery cannot run a command.
        if run.status != "RUNNING" or run.revision != revision:
            return True
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        command = next(
            (
                row
                for row in command_rows(db, run, episode.project_id)
                if row.snapshot_json["revision"] == revision
            ),
            None,
        )
        if command is None or command_receipt(db, episode.project_id, command.resource_id):
            return True
        identity, intent = command.resource_id, command.snapshot_json
    try:
        status, code = _invoke(factory, settings, identity, intent)
    except DomainError as exc:
        if exc.code in DEFERRED_CODES:
            # A fenced worker has no authority to close the intent of its successor.
            raise WorkflowExecutionDeferred(
                "Producer operation requires its original delivery"
            ) from exc
        if exc.code == "GATEWAY_RECONCILIATION_REQUIRED":
            # Unknown external effects await reconciliation and an explicit RECOVER.
            return True
        status, code = "FAILED", exc.code
    except WorkflowExecutionDeferred:
        raise
    except Exception as exc:
        logger.exception("Producer command failed", extra={"run_id": str(run_id)})
        # A transport/checkpoint/database failure can occur after an effect committed.
        # Replay the immutable intent and original action IDs instead of closing it.
        raise WorkflowExecutionDeferred("Producer command requires durable recovery") from exc
    if status is None:
        return True
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        command = next(
            row for row in command_rows(db, run, episode.project_id) if row.resource_id == identity
        )
        if command_receipt(db, episode.project_id, identity):
            return True
        record_receipt(db, run, episode.project_id, command, status, code)
        if code == "AGENT_HUMAN_STOPPED" and run.status == "RUNNING":
            assert run.agent_state_json is not None
            run.agent_state_json = terminate_agent_state(
                run.agent_state_json, scope_for_run(run, episode.project_id), reason="CANCELLED"
            ).model_dump(mode="json")
            run.status, run.error_code = "CANCELLED", None
    return True
