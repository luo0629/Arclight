"""Local revisions share the original Producer root and durable Agent runtime."""

from typing import cast
from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import checked_loop, submit_agent_decision
from arclight.application.agent_tools import AgentToolRegistry, available_agent_tools
from arclight.application.commands import emit, require_run
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.producer_plan_history import (
    ProducerPlanRecord,
    canonical_task,
    read_current_plan,
    read_plan_record,
)
from arclight.application.producer_planning import producer_registry
from arclight.application.producer_series import has_series_scope, task_context
from arclight.application.production_versions import bind, component, find_binding, text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.producer_observation import ProducerObservation
from arclight.domain.producer_planning import DelegationRequest, ProducerPlan
from arclight.domain.producer_replanning import (
    REPLAN_VERSION,
    ProducerReplan,
    ProducerReplanSeed,
)
from arclight.domain.producer_research import RESEARCH_TOOL
from arclight.domain.producer_series import (
    SeriesProducerReplan,
    SeriesProducerReplanSeed,
    replan_model,
    replan_version,
    replanning_instruction,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import AgentToolInvocation, AuditEvent, WorkflowRun
from arclight.persistence.versions import ProductionVersion


def operation_key(observation_id: UUID) -> str:
    return REPLAN_VERSION + ":" + str(observation_id)


def _authorize_replan(db: Session, root: WorkflowRun, actor: str, settings: Settings) -> None:
    tools = available_agent_tools(
        db, producer_registry(settings), root.id, "producer", actor, "producer"
    )
    if RESEARCH_TOOL not in {tool.name for tool in tools}:
        raise DomainError("AGENT_TOOL_GRANT_FORBIDDEN", "当前用户未授权生产重规划调查工具", 403)


def _operation(run_id: UUID, observation_id: UUID) -> str:
    return digest(
        {"run": str(run_id), "stage": "producer", "operation": operation_key(observation_id)}
    )


def _loop(db: Session, run_id: UUID, observation_id: UUID) -> AgentLoop | None:
    return db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id,
            AgentLoop.operation_sha256 == _operation(run_id, observation_id),
        )
    )


def _definitions(version: str = "producer/1") -> list[tuple[str, dict]]:
    return [
        ("AGENT", {"workflow_version": version, "operation_version": replan_version(version)}),
        ("PROMPT", {"instruction": replanning_instruction(version)}),
        ("SCHEMA", replan_model(version).model_json_schema()),
    ]


def _versions(
    db: Session, root: WorkflowRun, observation: ProducerObservation, *, capture: bool = False
) -> None:
    version = replan_version(root.workflow_version)
    name = "producer-series-replan" if has_series_scope(root) else "producer-replan"
    identity = uuid5(root.id, "producer-replan-spec:" + str(observation.id))
    binding = find_binding(db, observation.project_id, "PRODUCER_REPLAN_SPEC", identity)
    workflow = find_binding(db, observation.project_id, "WORKFLOW", root.id)
    if workflow is None:
        raise DomainError("PRODUCER_REPLAN_INVALID", "重规划缺少原工作流版本", 409)
    if binding is None and capture:
        entries = [
            component(
                db,
                observation.project_id,
                "TEXT",
                kind,
                name,
                version,
                definition,
            )
            for kind, definition in _definitions(root.workflow_version)
        ]
        binding = bind(
            db,
            observation.project_id,
            "PRODUCER_REPLAN_SPEC",
            identity,
            {
                "schema_version": 1,
                "run_id": str(root.id),
                "observation_id": str(observation.id),
                "workflow_sha256": digest(workflow.snapshot_json),
                "components": entries,
            },
        )
    try:
        value = binding.snapshot_json if binding is not None else {}
        if (
            value["schema_version"] != 1
            or value["run_id"] != str(root.id)
            or value["observation_id"] != str(observation.id)
            or value["workflow_sha256"] != digest(workflow.snapshot_json)
            or len(value["components"]) != len(_definitions(root.workflow_version))
        ):
            raise ValueError("Replan component scope changed")
        for entry, (kind, definition) in zip(
            value["components"], _definitions(root.workflow_version), strict=True
        ):
            row = db.get(ProductionVersion, UUID(entry["id"]))
            if (
                row is None
                or row.project_id != observation.project_id
                or row.family != "TEXT"
                or row.kind != kind
                or row.name != name
                or row.version != version
                or row.definition_json != definition
                or row.digest != digest(definition)
                or entry
                != {
                    "id": str(row.id),
                    "family": row.family,
                    "kind": row.kind,
                    "name": row.name,
                    "version": row.version,
                    "digest": row.digest,
                }
            ):
                raise ValueError("Replan definitions changed")
    except (KeyError, ValueError, TypeError) as exc:
        raise DomainError("PRODUCER_REPLAN_INVALID", "重规划须使用原冻结提示词和合同", 409) from exc


def _same_context(base: ProducerPlanRecord, plan: ProducerPlan, old_key: str, new_key: str) -> bool:
    old = task_context(base.root, base.plan, old_key)
    new = task_context(base.root, plan, new_key)
    return old.model_copy(update={"task_key": new_key}) == new if old else new is None


def carry_tasks(
    base: ProducerPlanRecord, plan: ProducerPlan, observation: ProducerObservation
) -> dict[str, DelegationRequest]:
    """Retain ultimate origins, including work not delegated until a later revision."""
    if (
        observation.run_id != base.root.id
        or observation.base_plan_id != base.plan_id
        or observation.base_plan_sha256 != base.plan_sha256
    ):
        raise DomainError("PRODUCER_REPLAN_SCOPE", "观察与前驱计划不一致", 409)
    old = {item.task_key: item for item in base.plan.tasks}
    observed = {item.task_key: item for item in observation.tasks}
    if set(observed) != set(old) or any(
        item.origin != canonical_task(base, key) for key, item in observed.items()
    ):
        raise DomainError("PRODUCER_REPLAN_SCOPE", "观察任务来源不一致", 409)
    carried: dict[str, DelegationRequest] = {}
    for task in plan.tasks:
        previous = old.get(task.task_key)
        if (
            previous is not None
            and previous.task == task.task
            and task_context(base.root, base.plan, task.task_key)
            == task_context(base.root, plan, task.task_key)
            and set(previous.depends_on) == set(task.depends_on)
            and all(key in carried for key in task.depends_on)
        ):
            carried[task.task_key] = canonical_task(base, task.task_key)
        elif previous is None and any(
            other.task == task.task and _same_context(base, plan, other.task_key, task.task_key)
            for other in base.plan.tasks
        ):
            raise DomainError("PRODUCER_REPLAN_REUSE", "既有任务不能改名或复制以重复委派", 409)
    for key in old:
        if key in carried:
            continue
        observed_task = observed[key]
        if (
            key not in observation.affected_task_keys
            or not observed_task.replaceable
            or observed_task.unknown_external_result
            or observed_task.status
            in {
                "PENDING",
                "RUNNING",
                "QUEUED",
                "WAITING",
                "WAITING_HUMAN",
                "WAITING_EXTERNAL",
                "CANCEL_REQUESTED",
            }
        ):
            raise DomainError("PRODUCER_REPLAN_REUSE", "只能替换观察确认可替换的受影响任务", 409)
    return carried


def _seed_observation(db: Session, loop: AgentLoop, actor: str) -> ProducerObservation:
    from arclight.application.producer_observation import read_observation

    try:
        request = AgentModelRequest.model_validate(loop.seed_json)
        first = request.messages[0]
        if not isinstance(first, AgentUserMessage):
            raise ValueError("Replan seed must start with user data")
        root = db.get(WorkflowRun, loop.run_id)
        assert root is not None
        version = replan_version(root.workflow_version)
        seed_model = SeriesProducerReplanSeed if has_series_scope(root) else ProducerReplanSeed
        frozen = seed_model.model_validate(first.data)
        observation = read_observation(db, loop.run_id, frozen.observation.id, actor)
        base = frozen.base_plan
        binding = find_binding(db, loop.project_id, "PRODUCER_PLAN", base.plan_id)
        if (
            frozen.observation != observation
            or frozen.project_id != loop.project_id
            or frozen.episode_id != observation.episode_id
            or base.plan_id != observation.base_plan_id
            or base.plan_sha256 != observation.base_plan_sha256
            or digest(base.plan.model_dump(mode="json")) != observation.base_plan_sha256
            or binding is None
            or binding.snapshot_json["version_no"] != base.version_no
            or binding.snapshot_json["plan"] != base.plan.model_dump(mode="json")
            or binding.snapshot_json["run_id"] != str(loop.run_id)
            or request.agent_name != "producer"
            or request.stage != "producer"
            or request.agent_version != version
            or request.operation_key != operation_key(observation.id)
            or request.prompt_version != version
            or request.result_schema_version != version
            or request.system_instruction != replanning_instruction(root.workflow_version)
            or loop.operation_sha256 != _operation(loop.run_id, observation.id)
            or loop.result_schema_sha256
            != digest(replan_model(root.workflow_version).model_json_schema())
        ):
            raise ValueError("Replan immutable seed changed")
        root = db.get(WorkflowRun, loop.run_id)
        assert root is not None
        if (
            frozen.goal != root.input_json["goal"]
            or frozen.goal_sha256 != root.input_json["goal_sha256"]
        ):
            raise ValueError("Replan goal changed")
        _versions(db, root, observation)
    except (KeyError, ValidationError, ValueError, TypeError, AttributeError) as exc:
        raise DomainError("PRODUCER_REPLAN_INVALID", "重规划循环作用范围无效", 409) from exc
    return observation


def require_completed_replan(db: Session, loop: AgentLoop) -> AgentLoopResult[ProducerReplan]:
    observation = _seed_observation(db, loop, loop.actor_id)
    try:
        root = db.get(WorkflowRun, loop.run_id)
        assert root is not None
        outcome = (
            cast(
                AgentLoopResult[ProducerReplan],
                AgentLoopResult[SeriesProducerReplan].model_validate(loop.outcome_json),
            )
            if has_series_scope(root)
            else AgentLoopResult[ProducerReplan].model_validate(loop.outcome_json)
        )
        proposal = outcome.result
        if (
            loop.status != "COMPLETED"
            or outcome.status != "COMPLETED"
            or outcome.run_id != loop.run_id
            or outcome.project_id != loop.project_id
            or outcome.loop_id != loop.id
            or outcome.input_sha256 != loop.input_sha256
            or outcome.agent_name != "producer"
            or outcome.stage != "producer"
            or proposal is None
            or proposal.base_plan_id != observation.base_plan_id
            or proposal.base_plan_sha256 != observation.base_plan_sha256
            or proposal.observation_id != observation.id
            or proposal.observation_sha256 != observation.snapshot_sha256
            or proposal.reason != observation.reason
        ):
            raise ValueError("Completed replan scope changed")
    except (ValidationError, ValueError, TypeError) as exc:
        raise DomainError("PRODUCER_REPLAN_INVALID", "重规划结果须绑定原完成循环", 409) from exc
    return outcome


def _fresh_evidence(db: Session, loop: AgentLoop, plan: ProducerPlan) -> None:
    actions = list(db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)))
    visible = {
        "loop-" + digest({"step": action.action_key, "call": call["call_id"]}): call["call_id"]
        for action in actions
        if action.kind == "MODEL" and action.result_json
        for call in action.result_json.get("tool_calls", [])
        if call["tool_name"] == RESEARCH_TOOL
    }
    evidence = set()
    investigated: set[str] = set()
    for action in actions:
        feedback = action.result_json or {}
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name") != RESEARCH_TOOL
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        invocation = db.scalar(
            select(AgentToolInvocation).where(
                AgentToolInvocation.run_id == loop.run_id,
                AgentToolInvocation.idempotency_key == feedback["idempotency_key"],
                AgentToolInvocation.agent_name == "producer",
                AgentToolInvocation.tool_name == RESEARCH_TOOL,
                AgentToolInvocation.status == "SUCCEEDED",
            )
        )
        if (
            invocation is not None
            and invocation.feedback_json == feedback
            and action.action_key in visible
        ):
            evidence.add((visible[action.action_key], feedback["result"]["evidence_sha256"]))
            if (visible[action.action_key], feedback["result"]["evidence_sha256"]) in {
                (row.tool_call_key, row.evidence_sha256) for row in plan.evidence
            }:
                investigated.add(feedback["result"]["episode_id"])
    if not {(row.tool_call_key, row.evidence_sha256) for row in plan.evidence} <= evidence:
        raise DomainError("PRODUCER_PLAN_EVIDENCE", "新版依据须来自本重规划循环的成功调查", 422)
    root = db.get(WorkflowRun, loop.run_id)
    assert root is not None
    if (
        has_series_scope(root)
        and not {item["episode_id"] for item in root.input_json["series_scope"]["episodes"]}
        <= investigated
    ):
        raise DomainError("PRODUCER_PLAN_EVIDENCE", "多集重规划须重新调查冻结范围内各集", 422)


def _validate(
    db: Session, run_id: UUID, observation_id: UUID, proposal: ProducerReplan, settings: Settings
) -> None:
    from arclight.application.producer_delegation import validate_plan_tasks
    from arclight.application.producer_observation import (
        read_observation,
        validate_observation_current,
    )

    root = db.get(WorkflowRun, run_id, populate_existing=True)
    assert root is not None
    actor = execution_actor(db, root)
    validate_publication(db, root.id)
    _authorize_replan(db, root, actor, settings)
    observation = read_observation(db, root.id, observation_id, actor)
    if (
        proposal.base_plan_id != observation.base_plan_id
        or proposal.base_plan_sha256 != observation.base_plan_sha256
        or proposal.observation_id != observation.id
        or proposal.observation_sha256 != observation.snapshot_sha256
        or proposal.reason != observation.reason
        or proposal.plan.goal_sha256 != root.input_json["goal_sha256"]
    ):
        raise DomainError("PRODUCER_REPLAN_SCOPE", "重规划不能更换观察、原因或冻结目标", 422)
    validate_observation_current(db, settings, root, observation, actor)
    base = read_plan_record(
        db, observation.project_id, observation.base_plan_id, actor, require_current=True
    )
    carry_tasks(base, proposal.plan, observation)
    loop = _loop(db, root.id, observation.id)
    if loop is None:
        raise DomainError("PRODUCER_REPLAN_INVALID", "重规划缺少原持久循环", 409)
    _seed_observation(db, loop, actor)
    _fresh_evidence(db, loop, proposal.plan)
    validate_plan_tasks(db, root, proposal.plan, actor, settings)


def _save(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    outcome: AgentLoopResult[ProducerReplan],
) -> None:
    if outcome.status != "COMPLETED":
        return
    assert outcome.result is not None and outcome.loop_id is not None
    with factory.begin() as db:
        lock_gate(db)
        loop = checked_loop(db, outcome.loop_id)
        root = require_run(db, run_id, loop.actor_id, lock=True)
        if loop.run_id != run_id or require_completed_replan(db, loop) != outcome:
            raise DomainError("PRODUCER_REPLAN_INVALID", "重规划结果与原循环不一致", 409)
        existing = find_binding(db, outcome.project_id, "PRODUCER_PLAN", loop.id)
        if existing is not None:
            read_plan_record(db, outcome.project_id, loop.id, loop.actor_id, require_current=True)
            return
        proposal = outcome.result
        _validate(db, run_id, proposal.observation_id, proposal, settings)
        base = read_current_plan(db, root, loop.actor_id)
        from arclight.application.producer_observation import read_observation

        observation = read_observation(db, root.id, proposal.observation_id, loop.actor_id)
        carried = carry_tasks(base, proposal.plan, observation)
        snapshot = {
            "schema_version": 2,
            "version_no": base.version_no + 1,
            "run_id": str(root.id),
            "goal_sha256": proposal.plan.goal_sha256,
            "plan_sha256": digest(proposal.plan.model_dump(mode="json")),
            "plan": proposal.plan.model_dump(mode="json"),
            "previous_plan_id": str(base.plan_id),
            "previous_plan_sha256": base.plan_sha256,
            "observation_id": str(observation.id),
            "observation_sha256": observation.snapshot_sha256,
            "reason": proposal.reason,
            "replan": proposal.model_dump(mode="json"),
            "carried_tasks": {
                key: request.model_dump(mode="json") for key, request in carried.items()
            },
        }
        bind(db, outcome.project_id, "PRODUCER_PLAN", loop.id, snapshot)
        db.add(
            AuditEvent(
                project_id=outcome.project_id,
                actor_id=loop.actor_id,
                action="producer.plan.replanned",
                resource_id=loop.id,
                details_json={
                    key: snapshot[key]
                    for key in (
                        "run_id",
                        "version_no",
                        "previous_plan_id",
                        "observation_id",
                        "reason",
                        "plan_sha256",
                    )
                },
            )
        )
        emit(
            db,
            root,
            "producer.plan.replanned",
            {"plan_id": str(loop.id), "version_no": snapshot["version_no"]},
        )


def replan_producer(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    observation_id: UUID,
) -> AgentLoopResult[ProducerReplan]:
    from arclight.application.agent_loop_recovery import run_persistent_agent_loop
    from arclight.application.producer_delegation import require_producer_parent
    from arclight.application.producer_observation import (
        read_observation,
        validate_observation_current,
    )

    with factory.begin() as db:
        lock_gate(db)
        root = require_run(db, run_id, actor, lock=True)
        require_producer_parent(db, root.id, root.episode_id, actor)
        validate_publication(db, root.id)
        _authorize_replan(db, root, actor, settings)
        observation = read_observation(db, root.id, observation_id, actor)
        text_settings(db, observation.project_id, root, settings)
        previous = _loop(db, root.id, observation.id)
        if previous is not None:
            previous = checked_loop(db, previous.id, actor=actor)
            _seed_observation(db, previous, actor)
            existing = find_binding(db, observation.project_id, "PRODUCER_PLAN", previous.id)
            if existing is not None:
                read_plan_record(
                    db, observation.project_id, previous.id, actor, require_current=True
                )
                return require_completed_replan(db, previous)
        validate_observation_current(db, settings, root, observation, actor)
        base = read_plan_record(
            db, observation.project_id, observation.base_plan_id, actor, require_current=True
        )
        _versions(db, root, observation, capture=True)
        request = (
            AgentModelRequest.model_validate(previous.seed_json)
            if previous
            else AgentModelRequest(
                run_id=root.id,
                agent_name="producer",
                stage="producer",
                operation_key=operation_key(observation.id),
                agent_version=replan_version(root.workflow_version),
                prompt_version=replan_version(root.workflow_version),
                result_schema_version=replan_version(root.workflow_version),
                system_instruction=replanning_instruction(root.workflow_version),
                max_tokens=16000,
                messages=[
                    AgentUserMessage(
                        data={
                            "project_id": str(observation.project_id),
                            "episode_id": str(root.episode_id),
                            "goal": root.input_json["goal"],
                            "goal_sha256": root.input_json["goal_sha256"],
                            "base_plan": {
                                "plan_id": str(base.plan_id),
                                "plan_sha256": base.plan_sha256,
                                "version_no": base.version_no,
                                "plan": base.plan.model_dump(mode="json"),
                            },
                            "observation": observation.model_dump(mode="json"),
                        }
                    )
                ],
            )
        )
    research = producer_registry(settings).get(RESEARCH_TOOL)
    assert research is not None
    outcome = run_persistent_agent_loop(
        factory,
        settings,
        request,
        AgentToolRegistry([research]),
        replan_model(root.workflow_version),
        validate_result=lambda db, proposal: _validate(
            db, run_id, observation_id, proposal, settings
        ),
    )
    _save(factory, settings, run_id, outcome)
    return outcome


def resume_producer_replan(
    factory: sessionmaker[Session],
    settings: Settings,
    plan_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[ProducerReplan]:
    from arclight.application.agent_loop_recovery import resume_agent_loop
    from arclight.application.producer_observation import validate_observation_current

    with factory.begin() as db:
        loop = checked_loop(db, plan_id, actor=actor)
        observation = _seed_observation(db, loop, actor)
        root = require_run(db, loop.run_id, actor, lock=True)
        _authorize_replan(db, root, actor, settings)
        text_settings(db, loop.project_id, root, settings)
        existing = find_binding(db, loop.project_id, "PRODUCER_PLAN", loop.id)
        if existing is not None:
            read_plan_record(db, loop.project_id, loop.id, actor, require_current=True)
            if decision is not None:
                submit_agent_decision(db, loop.id, actor, decision)
            return require_completed_replan(db, loop)
        validate_observation_current(db, settings, root, observation, actor)
        read_plan_record(db, loop.project_id, observation.base_plan_id, actor, require_current=True)
        run_id = root.id
    research = producer_registry(settings).get(RESEARCH_TOOL)
    assert research is not None
    outcome = resume_agent_loop(
        factory,
        settings,
        plan_id,
        actor,
        AgentToolRegistry([research]),
        replan_model(root.workflow_version),
        decision=decision,
        validate_result=lambda db, proposal: _validate(
            db, run_id, observation.id, proposal, settings
        ),
    )
    _save(factory, settings, run_id, outcome)
    return outcome
