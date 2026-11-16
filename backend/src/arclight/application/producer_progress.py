"""Authorized public projection of Producer history without exposing executable seeds."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_limits import (
    action_usage,
    delegation_count,
    validate_agent_execution,
)
from arclight.application.agent_workflows import creation_loops
from arclight.application.commands import require_episode, require_run
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate, utc
from arclight.application.producer_diagnostics import public_diagnostic
from arclight.application.producer_interaction import (
    command_receipt,
    command_rows,
    has_pending_question,
    validate_delegation,
)
from arclight.application.producer_plan_history import find_task_delegation
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.producer_planning import WORKFLOW_VERSION, DelegationRequest, ProducerPlan
from arclight.domain.producer_replanning import REPLAN_VERSION
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoopDecision
from arclight.persistence.base import utcnow
from arclight.persistence.models import Episode, WorkflowRun
from arclight.persistence.versions import VersionBinding


def read_progress(db: Session, run_id: UUID, actor: str, settings: Settings) -> dict:
    # Authenticate before joining execution's gate -> run lock order. Recheck after
    # waiting: authorization and state may have changed while another worker held it.
    require_run(db, run_id, actor)
    lock_gate(db)
    db.expire_all()
    run = require_run(db, run_id, actor)
    if run.workflow_type != "producer" or run.workflow_version != WORKFLOW_VERSION:
        raise DomainError("NOT_FOUND", "生产协调运行不存在", 404)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    project_id = episode.project_id
    control = db.get(AgentRunLimit, run.id)
    reason = None
    try:
        require_episode(db, episode.id, actor, write=True)
        if execution_actor(db, run) != actor:
            raise DomainError("PRODUCER_INITIATOR_REQUIRED", "仅原发起者可操作生产协调运行", 403)
        validate_publication(db, run.id)
        if run.status != "RUNNING":
            raise DomainError("AGENT_NOT_ACTIVE", "生产协调运行当前不可操作", 409)
    except DomainError as exc:
        reason = exc.code
    can_cancel = reason is None
    if reason is None:
        try:
            validate_agent_execution(db, run.id, "producer")
        except DomainError as exc:
            reason = exc.code
    can_operate = reason is None
    commands, pending = [], None
    for row in command_rows(db, run, project_id):
        receipt = command_receipt(db, project_id, row.resource_id)
        item = {
            "command_id": str(row.resource_id),
            "revision": row.snapshot_json["revision"],
            "action": row.snapshot_json["action"],
            "status": receipt["status"] if receipt else "QUEUED",
            "error_code": public_diagnostic(receipt["error_code"]) if receipt else None,
        }
        commands.append(item)
        if receipt is None:
            pending = item
    loops = creation_loops(db, run.id, "producer")
    question_pending = has_pending_question(db, run.id)
    has_lease = any(
        loop.lease_expires_at and utc(loop.lease_expires_at) > utcnow() for loop in loops
    )
    operations = []
    for loop in loops:
        outcome = loop.outcome_json or {}
        question_id = outcome.get("question_id")
        answered = bool(
            question_id
            and db.scalar(
                select(AgentLoopDecision.id).where(
                    AgentLoopDecision.question_id == UUID(question_id)
                )
            )
        )
        operations.append(
            {
                "loop_id": str(loop.id),
                "kind": "replan"
                if loop.seed_json.get("result_schema_version") == REPLAN_VERSION
                else "plan",
                "status": loop.status,
                "stop_code": public_diagnostic(outcome.get("stop_code")),
                "model_turns": outcome.get("model_turns", 0),
                "question_id": question_id,
                "input_sha256": loop.input_sha256,
                "human_request": outcome.get("human_request"),
                "can_answer": can_operate
                and pending is None
                and not answered
                and loop.status == "WAITING_HUMAN"
                and bool(question_id),
            }
        )
    if pending and any(loop.status in {"WAITING_EXTERNAL", "RECOVERY_REQUIRED"} for loop in loops):
        pending["status"] = "WAITING_EXTERNAL"
    # Read authorization precedes the narrowly scoped immutable snapshot query. Execution
    # readers deliberately retain their stronger current-plan/creator/lifecycle guards.
    bindings = list(
        db.scalars(
            select(VersionBinding).where(
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == "PRODUCER_PLAN",
            )
        )
    )
    bindings = sorted(
        (row for row in bindings if row.snapshot_json.get("run_id") == str(run.id)),
        key=lambda row: row.snapshot_json["version_no"],
    )
    plans = []
    for index, binding in enumerate(bindings):
        snapshot = binding.snapshot_json
        plan = ProducerPlan.model_validate(snapshot["plan"])
        tasks = []
        for task in plan.tasks:
            request = DelegationRequest(
                plan_id=binding.resource_id,
                plan_sha256=snapshot["plan_sha256"],
                task_key=task.task_key,
            )
            origin = DelegationRequest.model_validate(
                snapshot.get("carried_tasks", {}).get(task.task_key) or request.model_dump()
            )
            saved = find_task_delegation(db, run.id, origin)
            child = db.get(WorkflowRun, saved.child_run_id) if saved else None
            allowed = (
                can_operate
                and pending is None
                and not question_pending
                and index == len(bindings) - 1
                and not saved
            )
            error = child.error_code if child else None
            if allowed:
                try:
                    validate_delegation(db, settings, run, episode, actor, request)
                except DomainError as exc:
                    allowed, error = False, exc.code
            unknown = False
            if child:
                from arclight.application.producer_observation import _unknown_result

                unknown = _unknown_result(db, child)
            tasks.append(
                {
                    "task_key": task.task_key,
                    "kind": task.task.kind,
                    "reason": task.reason,
                    "depends_on": task.depends_on,
                    "child_run_id": str(child.id) if child else None,
                    "status": child.status if child else "NOT_STARTED",
                    "error_code": public_diagnostic(error),
                    "unknown_external_result": unknown,
                    "can_delegate": bool(allowed),
                }
            )
        plans.append(
            {
                "plan_id": str(binding.resource_id),
                "plan_sha256": snapshot["plan_sha256"],
                "version_no": snapshot["version_no"],
                "summary": plan.summary,
                "previous_plan_id": snapshot.get("previous_plan_id"),
                "reason": snapshot.get("reason"),
                "tasks": tasks,
            }
        )
    usage = action_usage(db, run.id)
    return {
        "run_id": str(run.id),
        "episode_id": str(episode.id),
        "project_id": str(project_id),
        "status": run.status,
        "revision": run.revision,
        "goal": run.input_json["goal"],
        "can_operate": can_operate,
        "readonly_reason": reason,
        "can_recover": can_operate and pending is not None and not has_lease,
        "can_replan": can_operate and pending is None and bool(plans) and not question_pending,
        "can_cancel": can_cancel,
        "pending_command": pending,
        "commands": commands,
        "plans": plans,
        "operations": operations,
        "budget": {
            "limits": control.limits_json if control else {},
            "deadline_at": control.deadline_at.isoformat() if control else None,
            "actual_cost": str(usage["actual_cost"]),
            "pending_estimate": str(usage["pending_estimate"]),
            "unknown_cost": usage["unknown_cost"],
            "model_calls": sum(action.kind == "MODEL" for action in usage["actions"]),
            "tool_calls": sum(action.kind == "TOOL" for action in usage["actions"]),
            "delegations": delegation_count(db, run.id),
        },
    }
