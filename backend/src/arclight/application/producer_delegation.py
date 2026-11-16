"""Delegate frozen Producer tasks through existing professional workflow services."""

from uuid import UUID, uuid5

from sqlalchemy.orm import Session

from arclight.agents.contracts import ScriptContent
from arclight.application.agent_limits import (
    action_usage,
    delegation_count,
    validate_agent_execution,
)
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.commands import require_episode, start_run
from arclight.application.content_duration import validate_duration
from arclight.application.content_references import validate_script_assets
from arclight.application.gateway_text import execution_actor
from arclight.application.producer_plan_history import (
    canonical_task,
    find_task_delegation,
    read_plan_record,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_workflow import CandidateReviewCreate
from arclight.domain.producer_planning import (
    DELEGATE_TOOL,
    WORKFLOW_VERSION,
    ArtTask,
    DelegationRequest,
    DelegationResult,
    DirectorTask,
    ProducerPlan,
    ProducerTask,
)
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import Script, WorkflowRun


def require_producer_parent(
    db: Session, parent_id: UUID, episode_id: UUID, actor: str
) -> AgentRunLimit:
    """Only a live, server-configured Producer can own a same-episode child."""
    parent = db.get(WorkflowRun, parent_id, populate_existing=True)
    if (
        parent is None
        or parent.workflow_type != "producer"
        or parent.workflow_version != WORKFLOW_VERSION
        or parent.episode_id != episode_id
        or execution_actor(db, parent) != actor
    ):
        raise DomainError("PRODUCER_PARENT_INVALID", "生产父运行作用范围无效", 409)
    require_episode(db, episode_id, actor, write=True)
    validate_agent_execution(db, parent.id, "producer")
    control = db.get(AgentRunLimit, parent.id)
    if (
        control is None
        or control.parent_run_id is not None
        or control.root_run_id != parent.id
        or control.actor_id != actor
    ):
        raise DomainError("PRODUCER_PARENT_INVALID", "生产父运行须使用原根运行限制", 409)
    return control


def validate_producer_task(
    db: Session, root: WorkflowRun, planned: ProducerTask, actor: str, settings: Settings
) -> None:
    episode = require_episode(db, root.episode_id, actor, write=True)
    task = planned.task
    if task.kind == "screenwriter":
        if len(task.story) > min(20000, settings.max_story_chars):
            raise DomainError("STORY_TOO_LARGE", "故事超过服务长度限制", 422)
    elif isinstance(task, DirectorTask | ArtTask):
        if isinstance(task, ArtTask):
            from arclight.application.art_research_assets import (
                DIRECTORY_KINDS,
                allowed_for_episode,
                scoped_directory,
                usable_directory,
            )

            for target_id in task.task.target_asset_ids:
                target = scoped_directory(db, episode.project_id, target_id)
                if (
                    target is None
                    or target.asset_type not in DIRECTORY_KINDS
                    or not usable_directory(target)
                    or not allowed_for_episode(target, episode.id)
                ):
                    raise DomainError("ART_TARGET_INVALID", "美术目标资产类型或使用范围无效", 409)
        script = db.get(Script, task.script_version_id, populate_existing=True)
        if script is None or script.episode_id != episode.id:
            raise DomainError("NOT_FOUND", "指定剧本版本不存在", 404)
        if script.status != "APPROVED":
            raise DomainError("SCRIPT_NOT_APPROVED", "委派须引用已批准的精确剧本", 409)
        content = ScriptContent.model_validate(script.content_json)
        validate_script_assets(db, episode.project_id, content)
        validate_duration(
            content, episode.target_duration_ms, settings.text_duration_tolerance_percent
        )
        if task.kind == "art_director" and task.concept_plan is not None:
            from arclight.application.art_concepts import freeze_concept_plan

            freeze_concept_plan(db, episode.project_id, episode.id, task.concept_plan, actor=actor)
    else:
        from arclight.application.candidate_checks import require_candidate_media_check
        from arclight.application.continuity_reviews import require_review_target

        candidate = db.get(GenerationCandidate, task.candidate_id, populate_existing=True)
        if candidate is None or candidate.project_id != episode.project_id:
            raise DomainError("NOT_FOUND", "生成候选不存在", 404)
        checked = require_candidate_media_check(db, episode.project_id, candidate.id)
        from arclight.persistence.models import AssetVersion

        version = db.get(AssetVersion, candidate.asset_version_id, populate_existing=True)
        if version is None:
            raise DomainError("REVIEW_TARGET_INVALID", "候选精确资产版本不存在", 409)
        require_review_target(
            db,
            episode.project_id,
            episode.id,
            ReviewTarget(
                candidate_id=candidate.id,
                asset_id=version.asset_id,
                asset_version_id=version.id,
                input_manifest_id=candidate.input_manifest_id,
                media_check_sha256=digest(checked.model_dump(mode="json")),
                storyboard_id=task.storyboard_id,
                shot_id=task.shot_id,
            ),
            actor,
        )


def validate_plan_tasks(
    db: Session, root: WorkflowRun, plan: ProducerPlan, actor: str, settings: Settings
) -> None:
    """Validate existing exact inputs without creating or running a professional task."""
    require_producer_parent(db, root.id, root.episode_id, actor)
    for planned in plan.tasks:
        validate_producer_task(db, root, planned, actor, settings)


def _plan(
    context: AgentToolContext, request: DelegationRequest
) -> tuple[WorkflowRun, ProducerPlan, ProducerTask]:
    record = read_plan_record(
        context.db, context.project_id, request.plan_id, context.actor_id, require_current=True
    )
    if record.root.id != context.run_id or record.plan_sha256 != request.plan_sha256:
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划作用范围或精确哈希无效", 409)
    planned = next((item for item in record.plan.tasks if item.task_key == request.task_key), None)
    if planned is None:
        raise DomainError("PRODUCER_PLAN_INVALID", "生产计划中不存在指定任务", 409)
    return record.root, record.plan, planned


def _saved(context: AgentToolContext, request: DelegationRequest) -> DelegationResult | None:
    record = read_plan_record(context.db, context.project_id, request.plan_id, context.actor_id)
    origin = canonical_task(record, request.task_key)
    return find_task_delegation(context.db, context.run_id, origin)


def validate_saved_child(
    context: AgentToolContext,
    root: WorkflowRun,
    planned: ProducerTask,
    previous: DelegationResult,
    control: AgentRunLimit,
) -> None:
    from arclight.application.model_gateway import utc

    child = context.db.get(WorkflowRun, previous.child_run_id, populate_existing=True)
    child_control = context.db.get(AgentRunLimit, previous.child_run_id)
    identity = uuid5(
        root.id, "producer-delegation:" + str(previous.plan_id) + ":" + previous.task_key
    )
    expected_type = {
        "screenwriter": "script_storyboard",
        "director": "storyboard",
        "art_director": "art_proposal",
        "continuity_reviewer": "continuity_review",
    }[planned.task.kind]
    if (
        child is None
        or child.workflow_type != expected_type
        or child.episode_id != root.episode_id
        or execution_actor(context.db, child) != context.actor_id
        or child_control is None
        or child_control.parent_run_id != root.id
        or child_control.root_run_id != root.id
        or child_control.actor_id != context.actor_id
        or child_control.project_id != context.project_id
        or child_control.depth != control.depth + 1
        or child_control.limits_json != control.limits_json
        or utc(child_control.deadline_at) != utc(control.deadline_at)
        or child_control.input_sha256 != scope_for_run(child, context.project_id).input_sha256
        or previous.root_run_id != root.id
        or previous.task_kind != planned.task.kind
        or previous.delegation_id != identity
    ):
        raise DomainError("PRODUCER_DELEGATION_CONFLICT", "原生产子任务作用范围无效", 409)


def _validate_reusable_output(context: AgentToolContext, previous: DelegationResult) -> None:
    from arclight.application.producer_observation import validate_child_outputs

    child = context.db.get(WorkflowRun, previous.child_run_id, populate_existing=True)
    assert child is not None
    if child.status == "SUCCEEDED":
        validate_child_outputs(context.db, child)


def validate_delegation_scope(
    context: AgentToolContext, request: DelegationRequest, settings: Settings
) -> None:
    """Revalidate exact authority and child history, including successful tool replay."""
    root, _, planned = _plan(context, request)
    validate_producer_task(context.db, root, planned, context.actor_id, settings)
    control = require_producer_parent(context.db, root.id, root.episode_id, context.actor_id)
    previous = _saved(context, request)
    if previous is not None:
        validate_saved_child(context, root, planned, previous, control)
        _validate_reusable_output(context, previous)


def _delegate(context: AgentToolContext, request: DelegationRequest, settings: Settings) -> dict:
    db = context.db
    root, plan, planned = _plan(context, request)
    validate_producer_task(db, root, planned, context.actor_id, settings)
    control = require_producer_parent(db, root.id, root.episode_id, context.actor_id)
    previous = _saved(context, request)
    if previous is not None:
        validate_saved_child(context, root, planned, previous, control)
        _validate_reusable_output(context, previous)
        return previous.model_copy(update=request.model_dump()).model_dump(mode="json")
    for prerequisite in planned.depends_on:
        saved = _saved(context, request.model_copy(update={"task_key": prerequisite}))
        child = db.get(WorkflowRun, saved.child_run_id) if saved else None
        if saved is not None:
            prerequisite_task = next(item for item in plan.tasks if item.task_key == prerequisite)
            validate_saved_child(context, root, prerequisite_task, saved, control)
            _validate_reusable_output(context, saved)
            validate_producer_task(db, root, prerequisite_task, context.actor_id, settings)
        if child is None or child.status != "SUCCEEDED":
            raise DomainError("PRODUCER_PREREQUISITE_PENDING", "前置生产任务尚未成功", 409)
    limits = AgentRunLimits.model_validate(control.limits_json)
    if control.depth + 1 > limits.max_delegation_depth:
        raise DomainError("AGENT_DELEGATION_DEPTH", "Agent 委派深度已达上限", 409)
    if delegation_count(db, control.root_run_id) >= limits.max_delegations:
        raise DomainError("AGENT_DELEGATION_LIMIT", "Agent 委派次数已达上限", 409)
    usage = action_usage(db, control.root_run_id)
    if limits.maximum_cost is not None and (
        usage["unknown_cost"]
        or usage["actual_cost"] + usage["pending_estimate"] > limits.maximum_cost
    ):
        raise DomainError("AGENT_COST_LIMIT", "已有用量不允许新生产委派", 409)
    record = read_plan_record(db, context.project_id, request.plan_id, context.actor_id)
    origin = canonical_task(record, planned.task_key)
    identity = uuid5(root.id, "producer-delegation:" + str(origin.plan_id) + ":" + origin.task_key)
    task = planned.task
    if task.kind == "screenwriter":
        child = start_run(
            db,
            root.episode_id,
            context.actor_id,
            task.story,
            str(identity),
            root.trace_id,
            professional=True,
            revision_rounds=task.revision_rounds,
            settings=settings,
            agent_limits=limits,
            producer_parent_run_id=root.id,
        )
    elif task.kind == "director":
        child = start_run(
            db,
            root.episode_id,
            context.actor_id,
            "",
            str(identity),
            root.trace_id,
            script_version_id=task.script_version_id,
            professional_director=True,
            director_revision_rounds=task.director_revision_rounds,
            director_art_collaboration=task.art_collaboration,
            settings=settings,
            agent_limits=limits,
            producer_parent_run_id=root.id,
        )
    elif task.kind == "art_director":
        child = start_run(
            db,
            root.episode_id,
            context.actor_id,
            "",
            str(identity),
            root.trace_id,
            script_version_id=task.script_version_id,
            art_task=task.task,
            art_concept_plan=task.concept_plan,
            settings=settings,
            agent_limits=limits,
            producer_parent_run_id=root.id,
        )
    else:
        from arclight.application.continuity_workflow import start_candidate_review

        child = start_candidate_review(
            db,
            settings,
            context.project_id,
            task.candidate_id,
            context.actor_id,
            CandidateReviewCreate(
                storyboard_id=task.storyboard_id, shot_id=task.shot_id, agent_limits=limits
            ),
            producer_parent_run_id=root.id,
        )
    return DelegationResult(
        **request.model_dump(),
        delegation_id=identity,
        child_run_id=child.id,
        root_run_id=root.id,
        task_kind=task.kind,
    ).model_dump(mode="json")


def delegation_tools(settings: Settings) -> list[AgentToolDefinition]:
    def scope(context: AgentToolContext, parameters: AgentToolContract) -> None:
        assert isinstance(parameters, DelegationRequest)
        validate_delegation_scope(context, parameters, settings)

    def delegate(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, DelegationRequest)
        return _delegate(context, parameters, settings)

    return [
        AgentToolDefinition(
            name=DELEGATE_TOOL,
            allowed_agents=frozenset({"producer"}),
            allowed_steps=frozenset({"producer"}),
            parameters_model=DelegationRequest,
            result_model=DelegationResult,
            scope_validator=scope,
            replay_validator=scope,
            handler=delegate,
            requires_write=True,
        )
    ]
