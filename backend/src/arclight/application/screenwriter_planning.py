"""Version-bound planning through the shared persistent Runtime and investigation tool."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import read_script_content
from arclight.agents.script_rewrite import RewriteRequest
from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_loop_store import checked_loop
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolRegistry, build_agent_tool_registry
from arclight.application.commands import require_run
from arclight.application.content_duration import duration_bounds
from arclight.application.context_evidence import read_trace
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.rewrite_content import content_hash, selected_scenes
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_state import AgentRunState, state_for_scope
from arclight.domain.screenwriter_planning import (
    PLAN_VERSION,
    PLANNING_PROMPT,
    SERIES_PLAN_VERSION,
    SERIES_PLANNING_PROMPT,
    EpisodeConstraints,
    EpisodePlan,
    PlanEvidence,
    PlanningConstraints,
    PlanningSeed,
    RewriteConstraints,
    RewritePlan,
    ScreenwriterPlan,
    SeasonConstraints,
    SeasonPlan,
    SeriesPlanEvidence,
    SeriesPlanningSeed,
    SeriesScreenwriterPlan,
)
from arclight.domain.screenwriter_research import (
    SERIES_RESEARCH_TOOL,
    InvestigateSeriesStoryResult,
    InvestigateStoryResult,
)
from arclight.domain.screenwriter_revision import SERIES_WORKFLOWS
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import WorkflowRun

_STAGES = {"story_outline": "outline", "script_storyboard": "script", "script_rewrite": "rewrite"}


def _registry(settings: Settings, *, series: bool = False) -> AgentToolRegistry:
    tool = build_agent_tool_registry(settings=settings).get(
        SERIES_RESEARCH_TOOL if series else "investigate_story"
    )
    assert tool is not None
    return AgentToolRegistry([tool])


def _stage(run: WorkflowRun) -> str:
    stage = _STAGES.get(run.workflow_type)
    if stage is None:
        raise DomainError("SCREENWRITER_TASK_INVALID", "此任务没有编剧规划模式", 422)
    return stage


def _plan_key(attempt: int = 0, *, series: bool = False) -> str:
    version = SERIES_PLAN_VERSION if series else PLAN_VERSION
    return version if attempt == 0 else f"{version}:attempt:{attempt}"


def _operation(run_id: UUID, stage: str, attempt: int = 0, *, series: bool = False) -> str:
    return digest(
        {
            "run": str(run_id),
            "stage": stage,
            "operation": _plan_key(attempt, series=series),
        }
    )


def _loop(
    db: Session, run_id: UUID, stage: str, attempt: int = 0, *, series: bool = False
) -> AgentLoop | None:
    return db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id,
            AgentLoop.operation_sha256 == _operation(run_id, stage, attempt, series=series),
        )
    )


def _seed(run: WorkflowRun, settings: Settings) -> AgentModelRequest:
    stage = _stage(run)
    inputs = run.input_json
    constraints: PlanningConstraints
    if stage == "outline":
        snapshot = inputs["source_snapshot"]
        constraints = SeasonConstraints(
            mode="season",
            target_episode_count=snapshot["target_episode_count"],
            episode_duration_ms=snapshot["episode_duration_ms"],
        )
    elif stage == "rewrite":
        rewrite = inputs["rewrite_input"]
        request = RewriteRequest.model_validate(rewrite["request"])
        constraints = RewriteConstraints(
            mode="rewrite",
            target_duration_ms=inputs["target_duration_ms"],
            duration_tolerance_percent=settings.text_duration_tolerance_percent,
            base_version_id=request.base_version_id,
            source_sha256=rewrite["source_sha256"],
            target=request.target,
        )
    else:
        constraints = EpisodeConstraints(
            mode="episode",
            target_duration_ms=inputs["target_duration_ms"],
            duration_tolerance_percent=settings.text_duration_tolerance_percent,
        )
    series = run.workflow_version in SERIES_WORKFLOWS
    seed = (
        SeriesPlanningSeed(constraints=constraints, narrative_context=inputs["narrative_context"])
        if series
        else PlanningSeed(constraints=constraints)
    )
    return AgentModelRequest(
        run_id=run.id,
        agent_name="screenwriter",
        stage=stage,
        operation_key=_plan_key(series=series),
        agent_version="screenwriter-planning/2" if series else "screenwriter-planning/1",
        prompt_version="screenwriter-planning/2" if series else "screenwriter-planning/1",
        result_schema_version=SERIES_PLAN_VERSION if series else PLAN_VERSION,
        system_instruction=SERIES_PLANNING_PROMPT if series else PLANNING_PROMPT,
        messages=[AgentUserMessage(data=seed.model_dump(mode="json"))],
        max_tokens=16000,
    )


def _validate(db: Session, run_id: UUID, plan: ScreenwriterPlan, attempt: int = 0) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    stage = _stage(run)
    series = run.workflow_version in SERIES_WORKFLOWS
    loop = _loop(db, run_id, stage, attempt, series=series)
    if loop is None:
        raise DomainError("SCREENWRITER_PLAN_INVALID", "规划缺少持久运行", 409)
    seed = AgentModelRequest.model_validate(loop.seed_json)
    first = seed.messages[0]
    assert isinstance(first, AgentUserMessage)
    seed_model = SeriesPlanningSeed if series else PlanningSeed
    task = seed_model.model_validate(first.data).constraints
    if series:
        if (
            not isinstance(plan, SeriesScreenwriterPlan)
            or plan.narrative_context.model_dump(mode="json") != run.input_json["narrative_context"]
        ):
            raise DomainError("SCREENWRITER_PLAN_SCOPE", "连续剧情规划不能更换冻结来源", 422)
        allowed_characters = set(plan.narrative_context.character_ids)
        if any(not set(scene.character_ids) <= allowed_characters for scene in plan.plan.scenes):
            raise DomainError("SCREENWRITER_PLAN_SCOPE", "场景人物须属于冻结写作视角", 422)
    if plan.plan.mode != task.mode:
        raise DomainError("SCREENWRITER_PLAN_SCOPE", "规划模式与冻结任务不一致", 422)
    if any(question.blocking for question in plan.open_questions):
        raise DomainError("SCREENWRITER_CLARIFICATION_REQUIRED", "阻塞问题须先请求人工澄清", 422)
    source_scene_numbers: dict[str, int] = {}
    if isinstance(task, SeasonConstraints):
        assert isinstance(plan.plan, SeasonPlan)
        outline = plan.plan.outline
        if (outline.target_episode_count, outline.episode_duration_ms) != (
            task.target_episode_count,
            task.episode_duration_ms,
        ):
            raise DomainError("SCREENWRITER_PLAN_SCOPE", "分集数量或单集时长不符冻结目标", 422)
    else:
        assert isinstance(plan.plan, (EpisodePlan, RewritePlan))
        actual = plan.plan.estimated_duration_ms
        lower, upper = duration_bounds(task.target_duration_ms, task.duration_tolerance_percent)
        if not lower <= actual <= upper:
            raise DomainError("SCREENWRITER_PLAN_DURATION", "计划估时不在冻结单集允许范围", 422)
        if isinstance(task, EpisodeConstraints):
            assert isinstance(plan.plan, EpisodePlan)
            if plan.plan.target_duration_ms != task.target_duration_ms:
                raise DomainError("SCREENWRITER_PLAN_SCOPE", "单集计划不能更改目标时长", 422)
        else:
            assert isinstance(plan.plan, RewritePlan)
            rewrite = run.input_json["rewrite_input"]
            source = read_script_content(rewrite["source_script"])
            if (
                plan.plan.base_version_id != task.base_version_id
                or plan.plan.target != task.target
                or content_hash(rewrite["source_script"]) != task.source_sha256
                or [scene.scene_id for scene in plan.plan.scenes]
                != [scene.scene_id for scene in selected_scenes(source, task.target)]
            ):
                raise DomainError("SCREENWRITER_PLAN_SCOPE", "改写计划不能扩大或更换精确范围", 422)
            source_scene_numbers = {scene.scene_id: scene.scene_no for scene in source.scenes}
    if isinstance(plan, SeriesScreenwriterPlan):
        target_scene_no = plan.narrative_context.target_scene_no
        if isinstance(plan.plan, EpisodePlan):
            if target_scene_no > len(plan.plan.scenes):
                raise DomainError("SCREENWRITER_PLAN_SCOPE", "冻结写作场景须存在于单集规划", 422)
            anchor_times = [plan.plan.scenes[target_scene_no - 1].story_time]
        else:
            anchor_times = [
                scene.story_time
                for scene in plan.plan.scenes
                if source_scene_numbers[scene.scene_id] == target_scene_no
            ]
        if any(value != plan.narrative_context.target_story_time for value in anchor_times):
            raise DomainError("SCREENWRITER_PLAN_SCOPE", "写作目标场景须保持冻结故事时点", 422)
    # Validate the original chain, then require citations from this loop's actual
    # successful tools, excluding other operations and fabricated manifests.
    read_trace(db, run, stage, execution_actor(db, run))
    allowed = set()
    author_materials = set()
    for action in db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop.id)):
        feedback = action.result_json
        if (
            action.kind != "TOOL"
            or action.request_json.get("tool_name")
            != (SERIES_RESEARCH_TOOL if series else "investigate_story")
            or not feedback
            or feedback.get("status") != "SUCCEEDED"
        ):
            continue
        research_model = InvestigateSeriesStoryResult if series else InvestigateStoryResult
        research = research_model.model_validate(feedback["result"])
        for material in research.materials:
            allowed.add(
                (SeriesPlanEvidence if series else PlanEvidence)(
                    manifest_id=research.manifest_id,
                    **material.model_dump(exclude={"source", "content"}),
                ).model_dump_json()
            )
        if series:
            for material in research.materials:
                if material.content.get("perspective") == "AUTHOR":
                    author_materials.add(
                        SeriesPlanEvidence(
                            manifest_id=research.manifest_id,
                            **material.model_dump(exclude={"source", "content"}),
                        ).model_dump_json()
                    )
    cited = {item.model_dump_json() for item in plan.evidence}
    if series and not cited.intersection(author_materials):
        raise DomainError(
            "SCREENWRITER_PLAN_EVIDENCE", "连续剧情规划必须实际调查并引用作者时点投影", 422
        )
    if not cited <= allowed:
        raise DomainError(
            "SCREENWRITER_PLAN_EVIDENCE", "计划引用必须来自本次成功调查的精确材料", 422
        )


def _remember(factory: sessionmaker[Session], outcome: AgentLoopResult[ScreenwriterPlan]) -> None:
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, outcome.run_id, populate_existing=True, with_for_update=True)
        assert run is not None
        scope = scope_for_run(run, outcome.project_id)
        assert run.agent_state_json is not None
        state = state_for_scope(run.agent_state_json, scope)
        version = SERIES_PLAN_VERSION if run.workflow_version in SERIES_WORKFLOWS else PLAN_VERSION
        reference = {"loop_id": str(outcome.loop_id), "schema_version": version}
        previous = state.extensions.get("screenwriter.planning")
        if previous == reference:
            return
        if previous is not None:
            old = db.get(AgentLoop, UUID(str(previous["loop_id"])))
            new = db.get(AgentLoop, outcome.loop_id)
            seed = AgentModelRequest.model_validate(new.seed_json) if new else None
            if (
                old is None
                or old.status != "STOPPED"
                or seed is None
                or not seed.operation_key.startswith(version + ":attempt:")
            ):
                raise DomainError(
                    "SCREENWRITER_PLAN_CONFLICT", "本运行的有效规划引用不能被替换", 409
                )
        run.agent_state_json = AgentRunState.model_validate(
            state.model_dump(mode="json")
            | {
                "revision": state.revision + 1,
                "extensions": state.extensions | {"screenwriter.planning": reference},
            }
        ).model_dump(mode="json")


def plan_screenwriter(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    *,
    limits: AgentRunLimits | None = None,
    attempt: int = 0,
) -> AgentLoopResult[ScreenwriterPlan]:
    """Plan an already active writer step. Caller grants investigation before execution."""
    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        validate_publication(db, run.id)
        if execution_actor(db, run) != actor:
            raise DomainError("FORBIDDEN", "规划启动须使用原运行发起者", 403)
        series = run.workflow_version in SERIES_WORKFLOWS
        previous = _loop(db, run.id, _stage(run), attempt, series=series)
        try:
            request = (
                AgentModelRequest.model_validate(previous.seed_json)
                if previous
                else _seed(run, settings).model_copy(
                    update={"operation_key": _plan_key(attempt, series=series)}
                )
            )
        except (KeyError, ValidationError):
            raise DomainError(
                "SCREENWRITER_TASK_INVALID", "冻结编剧输入缺少有效规划约束", 422
            ) from None
    result = run_persistent_agent_loop(
        factory,
        settings,
        request,
        _registry(settings, series=series),
        SeriesScreenwriterPlan if series else ScreenwriterPlan,
        limits=limits,
        validate_result=lambda db, plan: _validate(db, run_id, plan, attempt),
    )
    _remember(factory, result)
    return result


def resume_screenwriter_plan(
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[ScreenwriterPlan]:
    """Resume the saved planning prompt, constraints, evidence and human question."""
    with factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        run = db.get(WorkflowRun, loop.run_id)
        assert run is not None
        seed = AgentModelRequest.model_validate(loop.seed_json)
        series = run.workflow_version in SERIES_WORKFLOWS
        version = SERIES_PLAN_VERSION if series else PLAN_VERSION
        attempt = 0
        if seed.operation_key.startswith(version + ":attempt:"):
            attempt = int(seed.operation_key.rsplit(":", 1)[1])
        if loop.operation_sha256 != _operation(run.id, _stage(run), attempt, series=series):
            raise DomainError("SCREENWRITER_PLAN_SCOPE", "此循环不是当前编剧规划操作", 409)
        run_id = run.id
    result = resume_agent_loop(
        factory,
        settings,
        loop_id,
        actor,
        _registry(settings, series=series),
        SeriesScreenwriterPlan if series else ScreenwriterPlan,
        decision=decision,
        validate_result=lambda db, plan: _validate(db, run_id, plan, attempt),
    )
    _remember(factory, result)
    return result


def read_screenwriter_plan(
    db: Session, loop_id: UUID, actor: str
) -> AgentLoopResult[ScreenwriterPlan] | AgentLoopResult[SeriesScreenwriterPlan] | None:
    """Read saved working output even after its owning workflow finishes; no execution."""
    loop = db.get(AgentLoop, loop_id)
    if loop is None:
        raise DomainError("NOT_FOUND", "编剧规划不存在", 404)
    run = require_run(db, loop.run_id, actor)
    version = SERIES_PLAN_VERSION if run.workflow_version in SERIES_WORKFLOWS else PLAN_VERSION
    seed = AgentModelRequest.model_validate(loop.seed_json)
    if not (seed.operation_key == version or seed.operation_key.startswith(version + ":attempt:")):
        raise DomainError("NOT_FOUND", "编剧规划不存在", 404)
    return (
        (
            AgentLoopResult[SeriesScreenwriterPlan]
            if run.workflow_version in SERIES_WORKFLOWS
            else AgentLoopResult[ScreenwriterPlan]
        ).model_validate(loop.outcome_json)
        if loop.outcome_json
        else None
    )
