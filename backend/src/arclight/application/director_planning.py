"""Persistent director planning reuses the original Runtime, evidence and human decisions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import read_script_content
from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_loop_store import checked_loop
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolRegistry, build_agent_tool_registry
from arclight.application.commands import require_run
from arclight.application.content_duration import duration_bounds
from arclight.application.director_performance import director_contracts, require_performance_script
from arclight.application.director_research import approved_input
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.short_drama_director import director_model_input
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_state import AgentRunState, state_for_scope
from arclight.domain.director_performance import (
    PerformanceDirectorPlan,
    PerformanceDirectorSeed,
    is_performance_director,
)
from arclight.domain.director_planning import (
    PLAN_VERSION,
    RESEARCH_TOOL,
    DirectorConstraints,
    DirectorEvidence,
    DirectorPlan,
    InvestigateProductionResult,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import WorkflowRun


def _registry(settings: Settings) -> AgentToolRegistry:
    tool = build_agent_tool_registry(settings=settings).get(RESEARCH_TOOL)
    assert tool is not None
    return AgentToolRegistry([tool])


def _operation(run_id: UUID, attempt: int, version: str = PLAN_VERSION) -> str:
    return digest({"run": str(run_id), "stage": "storyboard", "operation": _key(attempt, version)})


def _key(attempt: int, version: str = PLAN_VERSION) -> str:
    if type(attempt) is not int or attempt < 0:
        raise DomainError("DIRECTOR_ATTEMPT_INVALID", "规划重试次数无效", 422)
    return version if not attempt else f"{version}:attempt:{attempt}"


def _validate(db: Session, run_id: UUID, plan: DirectorPlan, settings: Settings, attempt: int):
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    script, _ = approved_input(db, run, execution_actor(db, run), settings)
    loop = db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id,
            AgentLoop.operation_sha256
            == _operation(run_id, attempt, director_contracts(run.workflow_version).plan_version),
        )
    )
    if loop is None:
        raise DomainError("DIRECTOR_PLAN_INVALID", "导演计划缺少持久运行", 409)
    seed = AgentModelRequest.model_validate(loop.seed_json)
    first = seed.messages[0]
    assert isinstance(first, AgentUserMessage)
    task = director_contracts(run.workflow_version).plan_seed.model_validate(first.data).constraints
    if (
        plan.script_version_id != task.script_version_id
        or plan.script_version_id != script.id
        or plan.script_sha256 != task.script_sha256
        or digest(script.content_json) != task.script_sha256
        or plan.target_duration_ms != task.target_duration_ms
        or [scene.scene_id for scene in plan.scenes] != task.scene_ids
    ):
        raise DomainError("DIRECTOR_PLAN_SCOPE", "导演计划不能更换剧本、场景或冻结目标", 422)
    lower, upper = duration_bounds(task.target_duration_ms, task.duration_tolerance_percent)
    if not lower <= plan.estimated_duration_ms <= upper:
        raise DomainError("DIRECTOR_PLAN_DURATION", "场景计划估时不满足冻结目标", 422)
    if any(question.blocking for question in plan.open_questions):
        raise DomainError("DIRECTOR_CLARIFICATION_REQUIRED", "阻塞问题须先请求人工澄清", 422)
    allowed: dict[str, dict] = {}
    for action in db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)):
        feedback = action.result_json
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name") != RESEARCH_TOOL
            or not feedback
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        result = InvestigateProductionResult.model_validate(feedback["result"])
        allowed.update(
            {
                DirectorEvidence.model_validate(
                    item.model_dump(exclude={"content"})
                ).model_dump_json(): item.content
                for item in result.materials
            }
        )
    if not {item.model_dump_json() for item in plan.evidence} <= allowed.keys():
        raise DomainError("DIRECTOR_PLAN_EVIDENCE", "计划须引用本循环实际成功调查的材料", 422)
    if not {"SCRIPT", "VISUAL", "CAPABILITY"} <= {item.source_kind for item in plan.evidence}:
        raise DomainError("DIRECTOR_PLAN_EVIDENCE", "计划缺少剧本、规范或生产能力调查", 422)
    covered = {
        scene["scene_id"]
        for item in plan.evidence
        if item.source_kind == "SCRIPT"
        for scene in allowed[item.model_dump_json()]["scenes"]
    }
    if covered != set(task.scene_ids):
        raise DomainError("DIRECTOR_PLAN_EVIDENCE", "计划须调查全部精确剧本场景", 422)


def _remember(factory: sessionmaker[Session], result: AgentLoopResult[DirectorPlan]):
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, result.run_id, populate_existing=True, with_for_update=True)
        assert run is not None and run.agent_state_json is not None
        state = state_for_scope(run.agent_state_json, scope_for_run(run, result.project_id))
        reference = {
            "loop_id": str(result.loop_id),
            "schema_version": director_contracts(run.workflow_version).plan_version,
        }
        previous = state.extensions.get("director.planning")
        if previous == reference:
            return
        if previous is not None:
            old = db.get(AgentLoop, UUID(str(previous["loop_id"])))
            if old is None or old.status != "STOPPED":
                raise DomainError("DIRECTOR_PLAN_CONFLICT", "有效导演计划不能替换", 409)
        run.agent_state_json = AgentRunState.model_validate(
            state.model_dump(mode="json")
            | {
                "revision": state.revision + 1,
                "extensions": state.extensions | {"director.planning": reference},
            }
        ).model_dump(mode="json")


def _select_operation(db: Session, run: WorkflowRun, attempt: int) -> None:
    """Reserve the working operation before I/O, including concurrent distinct retries."""
    assert run.agent_state_json is not None
    operation = _operation(run.id, attempt, director_contracts(run.workflow_version).plan_version)
    previous = run.agent_state_json["extensions"].get("director.planning_operation")
    if previous == {"sha256": operation}:
        return
    if previous is not None:
        old = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id, AgentLoop.operation_sha256 == previous["sha256"]
            )
        )
        if old is None or old.status != "STOPPED":
            raise DomainError("DIRECTOR_PLAN_CONFLICT", "须恢复原导演规划，不能另开重试", 409)
    run.agent_state_json = AgentRunState.model_validate(
        run.agent_state_json
        | {
            "revision": run.agent_state_json["revision"] + 1,
            "extensions": run.agent_state_json["extensions"]
            | {"director.planning_operation": {"sha256": operation}},
        }
    ).model_dump(mode="json")


def plan_director(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    *,
    limits: AgentRunLimits | None = None,
    attempt: int = 0,
) -> AgentLoopResult[DirectorPlan]:
    """Plan an active director step; the caller must grant read-only investigation first."""
    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        validate_publication(db, run.id)
        if actor != execution_actor(db, run):
            raise DomainError("FORBIDDEN", "规划启动须使用原运行发起者", 403)
        script, _ = approved_input(db, run, actor, settings)
        contracts = director_contracts(run.workflow_version)
        source = read_script_content(script.content_json)
        constraints = DirectorConstraints(
            script_version_id=script.id,
            script_sha256=digest(script.content_json),
            scene_ids=[scene.scene_id for scene in source.scenes],
            target_duration_ms=run.input_json["target_duration_ms"],
            duration_tolerance_percent=settings.text_duration_tolerance_percent,
        )
        plan_seed = (
            PerformanceDirectorSeed(
                constraints=constraints, script=require_performance_script(source)
            )
            if is_performance_director(run.workflow_version)
            else contracts.plan_seed(constraints=constraints)
        )
        _select_operation(db, run, attempt)
        previous = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id,
                AgentLoop.operation_sha256 == _operation(run.id, attempt, contracts.plan_version),
            )
        )
        if previous:
            request = AgentModelRequest.model_validate(previous.seed_json)
        else:
            prompt, messages = director_model_input(
                db, run, contracts.plan_prompt, plan_seed.model_dump(mode="json")
            )
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="director",
                stage="storyboard",
                operation_key=_key(attempt, contracts.plan_version),
                agent_version=contracts.plan_version,
                prompt_version=contracts.plan_version,
                result_schema_version=contracts.plan_version,
                system_instruction=prompt,
                max_tokens=16000,
                messages=messages,
            )
    result = run_persistent_agent_loop(
        factory,
        settings,
        request,
        _registry(settings),
        contracts.plan,
        limits=limits,
        validate_result=lambda db, plan: _validate(db, run_id, plan, settings, attempt),
    )
    _remember(factory, result)
    return result


def resume_director_plan(
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[DirectorPlan]:
    with factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        seed = AgentModelRequest.model_validate(loop.seed_json)
        run_id = loop.run_id
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        contracts = director_contracts(run.workflow_version)
        attempt = (
            int(seed.operation_key.rsplit(":", 1)[-1])
            if seed.operation_key.startswith(contracts.plan_version + ":attempt:")
            else 0
        )
        if seed.agent_name != "director" or loop.operation_sha256 != _operation(
            loop.run_id, attempt, contracts.plan_version
        ):
            raise DomainError("DIRECTOR_PLAN_SCOPE", "此循环不是导演规划", 409)
        validate_publication(db, run_id)
        approved_input(db, run, execution_actor(db, run), settings)
    result = resume_agent_loop(
        factory,
        settings,
        loop_id,
        actor,
        _registry(settings),
        contracts.plan,
        decision=decision,
        validate_result=lambda db, plan: _validate(db, run_id, plan, settings, attempt),
    )
    _remember(factory, result)
    return result


def read_director_plan(
    db: Session, loop_id: UUID, actor: str
) -> AgentLoopResult[DirectorPlan] | AgentLoopResult[PerformanceDirectorPlan] | None:
    loop = db.get(AgentLoop, loop_id)
    if loop is None:
        raise DomainError("NOT_FOUND", "导演规划不存在", 404)
    run = require_run(db, loop.run_id, actor)
    contracts = director_contracts(run.workflow_version)
    seed = AgentModelRequest.model_validate(loop.seed_json)
    if seed.agent_name != "director" or not (
        seed.operation_key == contracts.plan_version
        or seed.operation_key.startswith(contracts.plan_version + ":attempt:")
    ):
        raise DomainError("NOT_FOUND", "导演规划不存在", 404)
    if not loop.outcome_json:
        return None
    if is_performance_director(run.workflow_version):
        return AgentLoopResult[PerformanceDirectorPlan].model_validate(loop.outcome_json)
    return AgentLoopResult[DirectorPlan].model_validate(loop.outcome_json)
