"""Versioned Producer plans use the existing durable Agent runtime and authority gates."""

from typing import cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_limits import configure_agent_run, validate_agent_execution
from arclight.application.agent_runtime.store import checked_loop
from arclight.application.agent_runtime_state import initialize_run_state, scope_for_run
from arclight.application.agent_tools import (
    AgentToolRegistry,
    build_agent_tool_registry,
)
from arclight.application.commands import emit, require_episode, require_run
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import require_text_snapshot
from arclight.application.producer_series import has_series_scope
from arclight.application.production_versions import (
    bind,
    capture_text,
    find_binding,
    producer_prompt,
    text_settings,
)
from arclight.application.shared_agent_reads import grant_professional_agent_tools
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_state import transition_agent_step
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.producer_planning import (
    DELEGATE_TOOL,
    PLAN_VERSION,
    WORKFLOW_VERSION,
    ProducerGoal,
    ProducerPlan,
    ProducerPlanReference,
)
from arclight.domain.producer_research import RESEARCH_TOOL
from arclight.domain.producer_series import (
    PRODUCER_VERSIONS,
    SERIES_PLAN_VERSION,
    SERIES_PRODUCER_VERSION,
    SeriesProducerPlan,
    SeriesProducerScope,
    plan_model,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import AgentToolInvocation, AuditEvent, Episode, WorkflowRun


def producer_registry(settings: Settings) -> AgentToolRegistry:
    catalog = build_agent_tool_registry(settings=settings)
    return AgentToolRegistry(
        item for name in (RESEARCH_TOOL, DELEGATE_TOOL) if (item := catalog.get(name)) is not None
    )


def start_producer(
    db: Session,
    settings: Settings,
    episode_id: UUID,
    actor: str,
    goal: str,
    key: str,
    request_id: str,
    *,
    series: SeriesProducerScope | None = None,
    short_drama: dict | None = None,
) -> WorkflowRun:
    """Trusted application entry; UI and automatic observation belong to PROD-03/04.

    The root stays active while its children use their original review state machines.
    Starting this coordinator does not invoke a provider or dispatch another workflow.
    """
    value = ProducerGoal(goal=goal, key=key)
    lock_gate(db)
    episode = require_episode(db, episode_id, actor, write=True)
    db.refresh(episode, with_for_update=True)
    version = SERIES_PRODUCER_VERSION if series else WORKFLOW_VERSION
    fingerprint = digest(
        {"workflow": version, "goal": value.goal}
        | ({"series": series.model_dump(mode="json")} if series else {})
        | ({"short_drama": short_drama} if short_drama is not None else {})
    )
    previous = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode_id, WorkflowRun.idempotency_key == value.key
        )
    )
    if previous is not None:
        if previous.request_fingerprint != fingerprint or execution_actor(db, previous) != actor:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能更换生产目标或调用者", 409)
        return previous
    from arclight.application.producer_series import require_available_episode

    require_available_episode(db, episode)
    if db.scalar(
        select(WorkflowRun.id).where(
            WorkflowRun.episode_id == episode_id,
            WorkflowRun.status.in_(["QUEUED", "RUNNING", "WAITING_REVIEW"]),
            WorkflowRun.workflow_type != "continuity_review",
        )
    ):
        raise DomainError("EPISODE_BUSY", "该剧集已有未完成工作流", 409)
    inputs = {
        "created_by": actor,
        "goal": value.goal,
        "goal_sha256": digest({"goal": value.goal}),
        "target_duration_ms": episode.target_duration_ms,
    }
    if series is not None:
        from arclight.application.producer_series import capture_scope

        inputs["series_scope"] = capture_scope(db, episode, actor, series)
    if short_drama is not None:
        inputs["short_drama"] = short_drama
    service = require_text_snapshot(db, actor, settings)
    if service:
        inputs["_text_service"] = service
    run = WorkflowRun(
        episode_id=episode.id,
        workflow_type="producer",
        workflow_version=version,
        status="RUNNING",
        trace_id=request_id,
        idempotency_key=value.key,
        request_fingerprint=fingerprint,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    if series is not None:
        from arclight.application.producer_series import save_scope

        save_scope(db, episode.project_id, run)
    state = initialize_run_state(run, episode.project_id)
    state = transition_agent_step(
        state,
        scope_for_run(run, episode.project_id),
        expected_revision=state.revision,
        step_id="producer",
        target="RUNNING",
    )
    run.agent_state_json = state.model_dump(mode="json")
    capture_text(db, episode.project_id, run, settings, actor=actor)
    configure_agent_run(db, run.id, actor)
    grant_professional_agent_tools(
        db,
        AgentToolGrantRequest(
            run_id=run.id, agent_name="producer", allowed_tools=[RESEARCH_TOOL, DELEGATE_TOOL]
        ),
        actor,
        producer_registry(settings),
    )
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="workflow.started",
            resource_id=run.id,
            details_json={"workflow_type": "producer", "goal_sha256": inputs["goal_sha256"]},
        )
    )
    emit(db, run, "workflow.started", {"workflow_type": "producer"})
    return run


def _operation(run_id: UUID) -> str:
    return digest({"run": str(run_id), "stage": "producer", "operation": PLAN_VERSION})


def _planning_loop(db: Session, run_id: UUID) -> AgentLoop | None:
    return db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id, AgentLoop.operation_sha256 == _operation(run_id)
        )
    )


def _validate(db: Session, run_id: UUID, plan: ProducerPlan, settings: Settings) -> None:
    from arclight.application.producer_delegation import validate_plan_tasks

    run = db.get(WorkflowRun, run_id)
    assert run is not None
    validate_publication(db, run.id)
    validate_agent_execution(db, run.id, "producer")
    if plan.goal_sha256 != run.input_json["goal_sha256"]:
        raise DomainError("PRODUCER_PLAN_SCOPE", "生产计划不能更换冻结目标", 422)
    loop = _planning_loop(db, run.id)
    if loop is None:
        raise DomainError("PRODUCER_PLAN_EVIDENCE", "生产计划缺少本次持久调查", 422)
    actions = list(db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)))
    # Models see their transport call ID; durable tool keys are executor-generated.
    # Bind the visible citation through the committed model response to its real action.
    visible_keys = {
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
                AgentToolInvocation.run_id == run.id,
                AgentToolInvocation.idempotency_key == feedback["idempotency_key"],
                AgentToolInvocation.agent_name == "producer",
                AgentToolInvocation.tool_name == RESEARCH_TOOL,
                AgentToolInvocation.status == "SUCCEEDED",
            )
        )
        if (
            invocation is not None
            and invocation.feedback_json == feedback
            and action.action_key in visible_keys
        ):
            evidence.add((visible_keys[action.action_key], feedback["result"]["evidence_sha256"]))
            cited = (visible_keys[action.action_key], feedback["result"]["evidence_sha256"])
            if cited in {(row.tool_call_key, row.evidence_sha256) for row in plan.evidence}:
                investigated.add(feedback["result"]["episode_id"])
    if not {(row.tool_call_key, row.evidence_sha256) for row in plan.evidence} <= evidence:
        raise DomainError("PRODUCER_PLAN_EVIDENCE", "计划依据须来自本循环真实成功调查", 422)
    if (
        has_series_scope(run)
        and not {row["episode_id"] for row in run.input_json["series_scope"]["episodes"]}
        <= investigated
    ):
        raise DomainError("PRODUCER_PLAN_EVIDENCE", "多集计划须引用逐集真实调查", 422)
    validate_plan_tasks(db, run, plan, execution_actor(db, run), settings)


def require_completed_plan(loop: AgentLoop) -> AgentLoopResult[ProducerPlan]:
    """A mutable outcome must still describe its original immutable planning scope."""
    from pydantic import ValidationError

    try:
        seed = AgentModelRequest.model_validate(loop.seed_json)
        outcome = (
            cast(
                AgentLoopResult[ProducerPlan],
                AgentLoopResult[SeriesProducerPlan].model_validate(loop.outcome_json),
            )
            if seed.agent_version == SERIES_PRODUCER_VERSION
            else AgentLoopResult[ProducerPlan].model_validate(loop.outcome_json)
        )
        version = (
            SERIES_PLAN_VERSION if seed.agent_version == SERIES_PRODUCER_VERSION else PLAN_VERSION
        )
        if (
            loop.status != "COMPLETED"
            or loop.operation_sha256 != _operation(loop.run_id)
            or seed.agent_name != "producer"
            or seed.stage != "producer"
            or seed.agent_version not in PRODUCER_VERSIONS
            or seed.operation_key != PLAN_VERSION
            or seed.prompt_version != version
            or seed.result_schema_version != version
            or outcome.status != "COMPLETED"
            or outcome.run_id != loop.run_id
            or outcome.project_id != loop.project_id
            or outcome.loop_id != loop.id
            or outcome.agent_name != seed.agent_name
            or outcome.stage != seed.stage
            or outcome.input_sha256 != loop.input_sha256
        ):
            raise ValueError("Completed Producer scope changed")
    except (ValidationError, ValueError, TypeError) as exc:
        raise DomainError(
            "PRODUCER_PLAN_INVALID", "生产计划结果须绑定原完成循环和精确作用范围", 409
        ) from exc
    return outcome


def _save(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    outcome: AgentLoopResult[ProducerPlan],
) -> None:
    if outcome.status != "COMPLETED":
        return
    assert outcome.result is not None and outcome.loop_id is not None
    with factory.begin() as db:
        lock_gate(db)
        loop = _planning_loop(db, run_id)
        if loop is None:
            raise DomainError("PRODUCER_PLAN_INVALID", "生产计划缺少原持久循环", 409)
        loop = checked_loop(db, loop.id)
        if require_completed_plan(loop) != outcome:
            raise DomainError("PRODUCER_PLAN_INVALID", "生产计划结果与原循环不一致", 409)
        run = require_run(db, loop.run_id, loop.actor_id, lock=True)
        _validate(db, run.id, outcome.result, settings)
        reference = find_binding(db, outcome.project_id, "PRODUCER_PLAN", outcome.loop_id)
        snapshot = {
            "schema_version": 1,
            "version_no": 1,
            "run_id": str(run.id),
            "goal_sha256": outcome.result.goal_sha256,
            "plan_sha256": digest(outcome.result.model_dump(mode="json")),
            "plan": outcome.result.model_dump(mode="json"),
        }
        bind(db, outcome.project_id, "PRODUCER_PLAN", outcome.loop_id, snapshot)
        if reference is None:
            db.add(
                AuditEvent(
                    project_id=outcome.project_id,
                    actor_id=execution_actor(db, run),
                    action="producer.plan.created",
                    resource_id=outcome.loop_id,
                    details_json={"run_id": str(run.id), "plan_sha256": snapshot["plan_sha256"]},
                )
            )
            emit(
                db, run, "producer.plan.created", {"plan_id": str(outcome.loop_id), "version_no": 1}
            )


def plan_producer(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, actor: str
) -> AgentLoopResult[ProducerPlan]:
    from arclight.application.agent_runtime import run_persistent_agent_loop

    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        validate_publication(db, run.id)
        if (
            run.workflow_type != "producer"
            or run.workflow_version not in PRODUCER_VERSIONS
            or execution_actor(db, run) != actor
        ):
            raise DomainError("PRODUCER_SCOPE_INVALID", "此运行不是当前调用者的生产协调运行", 403)
        validate_agent_execution(db, run.id, "producer")
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        text_settings(db, episode.project_id, run, settings)
        previous = _planning_loop(db, run.id)
        request = (
            AgentModelRequest.model_validate(previous.seed_json)
            if previous is not None
            else AgentModelRequest(
                run_id=run.id,
                agent_name="producer",
                stage="producer",
                operation_key=PLAN_VERSION,
                agent_version=run.workflow_version,
                prompt_version=SERIES_PLAN_VERSION if has_series_scope(run) else PLAN_VERSION,
                result_schema_version=SERIES_PLAN_VERSION
                if has_series_scope(run)
                else PLAN_VERSION,
                system_instruction=producer_prompt(db, episode.project_id, run.id),
                messages=[
                    AgentUserMessage(
                        data={
                            "project_id": str(episode.project_id),
                            "episode_id": str(episode.id),
                            "goal": run.input_json["goal"],
                            "goal_sha256": run.input_json["goal_sha256"],
                            **(
                                {
                                    "series_scope": run.input_json["series_scope"],
                                    "series_scope_sha256": digest(run.input_json["series_scope"]),
                                }
                                if "series_scope" in run.input_json
                                else {}
                            ),
                        }
                    )
                ],
            )
        )
    # A planning model can investigate; delegation requires a separately completed plan.
    registry = producer_registry(settings)
    research = registry.get(RESEARCH_TOOL)
    assert research is not None
    outcome = run_persistent_agent_loop(
        factory,
        settings,
        request,
        AgentToolRegistry([research]),
        plan_model(run.workflow_version),
        validate_result=lambda db, plan: _validate(db, run_id, plan, settings),
    )
    _save(factory, settings, run_id, outcome)
    return outcome


def resume_producer_plan(
    factory: sessionmaker[Session],
    settings: Settings,
    plan_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[ProducerPlan]:
    from arclight.application.agent_runtime import resume_agent_loop

    with factory.begin() as db:
        loop = checked_loop(db, plan_id, actor=actor)
        if loop.operation_sha256 != _operation(loop.run_id):
            raise DomainError("PRODUCER_PLAN_SCOPE", "此循环不属于生产计划", 409)
        run_id = loop.run_id
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        result_model = plan_model(run.workflow_version)
    research = producer_registry(settings).get(RESEARCH_TOOL)
    assert research is not None
    outcome = resume_agent_loop(
        factory,
        settings,
        plan_id,
        actor,
        AgentToolRegistry([research]),
        result_model,
        decision=decision,
        validate_result=lambda db, plan: _validate(db, run_id, plan, settings),
    )
    _save(factory, settings, run_id, outcome)
    return outcome


def read_producer_plan(db: Session, plan_id: UUID, actor: str) -> ProducerPlanReference:
    loop = db.get(AgentLoop, plan_id)
    if loop is None:
        raise DomainError("NOT_FOUND", "生产计划不存在", 404)
    require_run(db, loop.run_id, actor)
    binding = find_binding(db, loop.project_id, "PRODUCER_PLAN", plan_id)
    if binding is None or loop.status != "COMPLETED":
        raise DomainError("PRODUCER_PLAN_PENDING", "生产计划尚未完成", 409)
    return ProducerPlanReference(plan_id=plan_id, plan_sha256=binding.snapshot_json["plan_sha256"])
