"""Read immutable plan lineage and resolve tasks to their original delegation."""

from dataclasses import dataclass
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.production_versions import find_binding
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest
from arclight.domain.producer_planning import (
    DELEGATE_TOOL,
    DelegationRequest,
    DelegationResult,
    ProducerPlan,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import AgentToolInvocation, WorkflowRun
from arclight.persistence.versions import VersionBinding


@dataclass(frozen=True)
class ProducerPlanRecord:
    root: WorkflowRun
    plan_id: UUID
    plan_sha256: str
    version_no: int
    plan: ProducerPlan
    carried_tasks: dict[str, DelegationRequest]
    previous_plan_id: UUID | None


def canonical_task(record: ProducerPlanRecord, task_key: str) -> DelegationRequest:
    if not any(item.task_key == task_key for item in record.plan.tasks):
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划中不存在指定任务", 409)
    return record.carried_tasks.get(task_key) or DelegationRequest(
        plan_id=record.plan_id, plan_sha256=record.plan_sha256, task_key=task_key
    )


def _read(
    db: Session, project_id: UUID, plan_id: UUID, actor: str, visited: set[UUID]
) -> ProducerPlanRecord:
    from arclight.application.producer_delegation import require_producer_parent
    from arclight.application.producer_planning import require_completed_plan

    if plan_id in visited:
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划历史出现循环", 409)
    visited.add(plan_id)
    binding = find_binding(db, project_id, "PRODUCER_PLAN", plan_id)
    if binding is None:
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划版本不存在", 409)
    value = binding.snapshot_json
    try:
        root = db.get(WorkflowRun, UUID(value["run_id"]), populate_existing=True)
        if root is None:
            raise ValueError("Missing root")
        require_producer_parent(db, root.id, root.episode_id, actor)
        # Reading known history must remain possible when external cost is unknown.
        # Authority/lifecycle/deadline are checked above; only a new operation reserves cost.
        loop = db.get(AgentLoop, plan_id, populate_existing=True)
        if loop is None:
            raise ValueError("Missing plan loop")
        seed = AgentModelRequest.model_validate(loop.seed_json)
        if (
            loop.actor_id != actor
            or seed.run_id != root.id
            or seed.agent_name != "producer"
            or seed.stage != "producer"
            or digest(loop.seed_json) != loop.request_sha256
            or scope_for_run(root, project_id).input_sha256 != loop.input_sha256
        ):
            raise ValueError("Plan loop authority changed")
        plan = ProducerPlan.model_validate(value["plan"])
        version_no = value["version_no"]
        checksum = digest(plan.model_dump(mode="json"))
        if (
            loop.run_id != root.id
            or loop.project_id != project_id
            or type(version_no) is not int
            or version_no < 1
            or value["goal_sha256"] != root.input_json["goal_sha256"]
            or plan.goal_sha256 != value["goal_sha256"]
            or value["plan_sha256"] != checksum
        ):
            raise ValueError("Plan scope changed")
        carried: dict[str, DelegationRequest] = {}
        previous_id = None
        if value["schema_version"] == 1 and version_no == 1:
            if (
                loop.result_schema_sha256 != digest(ProducerPlan.model_json_schema())
                or require_completed_plan(loop).result != plan
            ):
                raise ValueError("Original plan outcome changed")
        elif value["schema_version"] == 2 and version_no > 1:
            from arclight.application.producer_observation import read_observation
            from arclight.application.producer_replanning import (
                carry_tasks,
                require_completed_replan,
            )

            outcome = require_completed_replan(db, loop)
            proposal = outcome.result
            assert proposal is not None
            previous_id = UUID(value["previous_plan_id"])
            predecessor = _read(db, project_id, previous_id, actor, visited)
            observation = read_observation(db, root.id, proposal.observation_id, actor)
            if (
                predecessor.root.id != root.id
                or predecessor.version_no + 1 != version_no
                or proposal.plan != plan
                or proposal.base_plan_id != previous_id
                or proposal.base_plan_sha256 != predecessor.plan_sha256
                or value["previous_plan_sha256"] != predecessor.plan_sha256
                or value["replan"] != proposal.model_dump(mode="json")
                or value["observation_id"] != str(proposal.observation_id)
                or value["observation_sha256"] != proposal.observation_sha256
                or value["reason"] != proposal.reason
            ):
                raise ValueError("Replan predecessor changed")
            carried = carry_tasks(predecessor, plan, observation)
            if value["carried_tasks"] != {
                key: request.model_dump(mode="json") for key, request in carried.items()
            }:
                raise ValueError("Task origins changed")
        else:
            raise ValueError("Unsupported plan binding")
    except (KeyError, ValidationError, ValueError, TypeError, AssertionError) as exc:
        raise DomainError(
            "PRODUCER_PLAN_INVALID", "生产计划须绑定原完成循环和不可变前驱历史", 409
        ) from exc
    return ProducerPlanRecord(root, plan_id, checksum, version_no, plan, carried, previous_id)


def read_plan_record(
    db: Session,
    project_id: UUID,
    plan_id: UUID,
    actor: str,
    *,
    require_current: bool = False,
) -> ProducerPlanRecord:
    record = _read(db, project_id, plan_id, actor, set())
    if require_current and read_current_plan(db, record.root, actor).plan_id != plan_id:
        raise DomainError("PRODUCER_PLAN_SUPERSEDED", "生产计划已由新版替代", 409)
    return record


def read_current_plan(db: Session, root: WorkflowRun, actor: str) -> ProducerPlanRecord:
    from arclight.application.commands import require_episode

    episode = require_episode(db, root.episode_id, actor, write=True)
    rows = list(
        db.scalars(
            select(VersionBinding).where(
                VersionBinding.project_id == episode.project_id,
                VersionBinding.resource_type == "PRODUCER_PLAN",
            )
        )
    )
    candidates = [row for row in rows if row.snapshot_json.get("run_id") == str(root.id)]
    try:
        candidates.sort(key=lambda row: row.snapshot_json["version_no"])
        if not candidates or [row.snapshot_json["version_no"] for row in candidates] != list(
            range(1, len(candidates) + 1)
        ):
            raise ValueError("Plan versions are not a single chain")
        for previous, current in zip(candidates, candidates[1:], strict=False):
            if current.snapshot_json.get("previous_plan_id") != str(previous.resource_id):
                raise ValueError("Competing plan successors")
    except (KeyError, TypeError, ValueError) as exc:
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划历史不连续", 409) from exc
    return read_plan_record(db, episode.project_id, candidates[-1].resource_id, actor)


def find_task_delegation(
    db: Session, root_run_id: UUID, request: DelegationRequest
) -> DelegationResult | None:
    rows = db.scalars(
        select(AgentToolInvocation).where(
            AgentToolInvocation.run_id == root_run_id,
            AgentToolInvocation.tool_name == DELEGATE_TOOL,
            AgentToolInvocation.status == "SUCCEEDED",
        )
    )
    matches: list[DelegationResult] = []
    try:
        for row in rows:
            result = DelegationResult.model_validate((row.feedback_json or {})["result"])
            origin = DelegationRequest.model_validate(
                result.model_dump(include={"plan_id", "plan_sha256", "task_key"})
            )
            binding = find_binding(db, row.project_id, "PRODUCER_PLAN", result.plan_id)
            if binding is None or binding.snapshot_json.get("plan_sha256") != result.plan_sha256:
                raise ValueError("Delegation plan binding changed")
            inherited = binding.snapshot_json.get("carried_tasks", {}).get(result.task_key)
            if inherited is not None:
                origin = DelegationRequest.model_validate(inherited)
            if origin.plan_id == request.plan_id and origin.task_key == request.task_key:
                if origin.plan_sha256 != request.plan_sha256:
                    raise ValueError("Delegation hash changed")
                matches.append(result.model_copy(update=origin.model_dump()))
    except (KeyError, ValidationError, ValueError) as exc:
        raise DomainError("PRODUCER_DELEGATION_CONFLICT", "生产委派历史合同无效", 409) from exc
    if not matches:
        return None
    if any(item != matches[0] for item in matches):
        raise DomainError("PRODUCER_DELEGATION_CONFLICT", "同一生产任务委派不一致", 409)
    return matches[0]
