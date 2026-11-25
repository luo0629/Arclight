"""Delegate frozen Producer tasks through existing professional workflow services."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.agents.contracts import read_script_content
from arclight.application.agent_limits import (
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
from arclight.application.producer_series import (
    contains_episode,
    delegated_context,
    has_series_scope,
    require_episode_prerequisites,
    task_context,
    validate_series_plan,
    validate_task_context,
)
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.continuity_reviews import ReviewTarget
from arclight.domain.continuity_workflow import CandidateReviewCreate
from arclight.domain.producer_planning import (
    DELEGATE_TOOL,
    ArtTask,
    DelegationRequest,
    DelegationResult,
    DirectorTask,
    ProducerPlan,
    ProducerTask,
)
from arclight.domain.producer_series import (
    PRODUCER_VERSIONS,
    SeriesTaskContext,
)
from arclight.domain.screenwriter_creation import AUTOMATIC_REVISION_ROUNDS
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import Script, VersionBinding, WorkflowRun


def require_producer_parent(
    db: Session, parent_id: UUID, episode_id: UUID, actor: str
) -> AgentRunLimit:
    """Only a live, server-configured Producer can own a same-episode child."""
    parent = db.get(WorkflowRun, parent_id, populate_existing=True)
    if (
        parent is None
        or parent.workflow_type != "producer"
        or parent.workflow_version not in PRODUCER_VERSIONS
        or not contains_episode(db, parent, episode_id)
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
    db: Session,
    root: WorkflowRun,
    planned: ProducerTask,
    actor: str,
    settings: Settings,
    series_context: SeriesTaskContext | None = None,
    child: WorkflowRun | None = None,
) -> None:
    episode = validate_task_context(db, root, planned, actor, series_context, child)
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
        content = read_script_content(script.content_json)
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
        from arclight.persistence.models import GenerationJob

        job = db.get(GenerationJob, candidate.job_id)
        if job is not None and job.target_type == "VIDEO":
            from arclight.application.video_review_work import review_inputs

            inputs = review_inputs(db, episode.project_id, candidate.id)
            if (
                inputs.job.episode_id != episode.id
                or inputs.target.storyboard_id != task.storyboard_id
                or inputs.target.shot_id != task.shot_id
            ):
                raise DomainError("REVIEW_TASK_CONFLICT", "视频审查须使用原集精确镜头", 409)
            return
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
    if spec := root.input_json.get("short_drama"):
        art, director = (item.task for item in plan.tasks) if len(plan.tasks) == 2 else (None, None)
        if (
            not isinstance(art, ArtTask)
            or not isinstance(director, DirectorTask)
            or str(art.script_version_id) != spec["script_version_id"]
            or director.script_version_id != art.script_version_id
            or director.art_collaboration
            or art.concept_plan is not None
            or art.task.target_asset_ids
            or art.task.minimum_reference_images
            or plan.tasks[0].depends_on
            or plan.tasks[1].depends_on != [plan.tasks[0].task_key]
        ):
            raise DomainError(
                "SHORT_DRAMA_PLAN_SCOPE", "制作计划必须依次使用原剧本进行美术及分镜", 422
            )
    validate_series_plan(db, root, plan)
    from arclight.application.producer_plan_history import read_current_plan

    existing = (
        db.scalar(
            select(VersionBinding.id).where(
                VersionBinding.resource_type == "PRODUCER_PLAN",
                VersionBinding.snapshot_json["run_id"].as_string() == str(root.id),
            )
        )
        if has_series_scope(root)
        else None
    )
    current = read_current_plan(db, root, actor) if existing else None
    for planned in plan.tasks:
        old = (
            next((item for item in current.plan.tasks if item == planned), None)
            if current
            else None
        )
        child = None
        if (
            current is not None
            and old is not None
            and task_context(root, current.plan, old.task_key)
            == task_context(root, plan, planned.task_key)
        ):
            receipt = find_task_delegation(db, root.id, canonical_task(current, old.task_key))
            child = db.get(WorkflowRun, receipt.child_run_id) if receipt else None
        validate_producer_task(
            db, root, planned, actor, settings, task_context(root, plan, planned.task_key), child
        )


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
    series_context: SeriesTaskContext | None = None,
) -> None:

    child = context.db.get(WorkflowRun, previous.child_run_id, populate_existing=True)
    child_control = context.db.get(AgentRunLimit, previous.child_run_id)
    original = read_plan_record(context.db, context.project_id, previous.plan_id, context.actor_id)
    origin = canonical_task(original, previous.task_key)
    identity = uuid5(root.id, "producer-delegation:" + str(origin.plan_id) + ":" + origin.task_key)
    expected_type = {
        "screenwriter": "script_storyboard",
        "director": "storyboard",
        "art_director": "art_proposal",
        "continuity_reviewer": "continuity_review",
    }[planned.task.kind]
    dependency_scripts = []
    if series_context is not None:
        dependency_scripts = require_episode_prerequisites(
            context.db, root, original.plan, planned, series_context, context.actor_id
        )
    if (
        child is None
        or child.workflow_type != expected_type
        or child.episode_id != (series_context.episode_id if series_context else root.episode_id)
        or child.input_json.get("producer_series_context")
        != delegated_context(root, series_context, dependency_scripts)
        or execution_actor(context.db, child) != context.actor_id
        or child_control is None
        or child_control.parent_run_id != root.id
        or child_control.root_run_id != root.id
        or child_control.actor_id != context.actor_id
        or child_control.project_id != context.project_id
        or child_control.depth != control.depth + 1
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
    root, plan, planned = _plan(context, request)
    selected = task_context(root, plan, planned.task_key)
    control = require_producer_parent(context.db, root.id, root.episode_id, context.actor_id)
    previous = _saved(context, request)
    child = context.db.get(WorkflowRun, previous.child_run_id) if previous else None
    validate_producer_task(context.db, root, planned, context.actor_id, settings, selected, child)
    if previous is not None:
        validate_saved_child(context, root, planned, previous, control, selected)
        _validate_reusable_output(context, previous)


def _delegate(context: AgentToolContext, request: DelegationRequest, settings: Settings) -> dict:
    db = context.db
    root, plan, planned = _plan(context, request)
    selected = task_context(root, plan, planned.task_key)
    control = require_producer_parent(db, root.id, root.episode_id, context.actor_id)
    previous = _saved(context, request)
    child = db.get(WorkflowRun, previous.child_run_id) if previous else None
    validate_producer_task(db, root, planned, context.actor_id, settings, selected, child)
    if previous is not None:
        validate_saved_child(context, root, planned, previous, control, selected)
        _validate_reusable_output(context, previous)
        return previous.model_copy(update=request.model_dump()).model_dump(mode="json")
    for prerequisite in planned.depends_on:
        saved = _saved(context, request.model_copy(update={"task_key": prerequisite}))
        child = db.get(WorkflowRun, saved.child_run_id) if saved else None
        if saved is not None:
            prerequisite_task = next(item for item in plan.tasks if item.task_key == prerequisite)
            prerequisite_context = task_context(root, plan, prerequisite)
            validate_saved_child(
                context, root, prerequisite_task, saved, control, prerequisite_context
            )
            _validate_reusable_output(context, saved)
            validate_producer_task(
                db, root, prerequisite_task, context.actor_id, settings, prerequisite_context, child
            )
        if child is None or child.status != "SUCCEEDED":
            raise DomainError("PRODUCER_PREREQUISITE_PENDING", "前置生产任务尚未成功", 409)
    dependency_scripts = require_episode_prerequisites(
        db, root, plan, planned, selected, context.actor_id
    )
    record = read_plan_record(db, context.project_id, request.plan_id, context.actor_id)
    origin = canonical_task(record, planned.task_key)
    identity = uuid5(root.id, "producer-delegation:" + str(origin.plan_id) + ":" + origin.task_key)
    task = planned.task
    episode_id = selected.episode_id if selected else root.episode_id
    shared_context = delegated_context(root, selected, dependency_scripts)
    if task.kind == "screenwriter":
        child = start_run(
            db,
            episode_id,
            context.actor_id,
            task.story,
            str(identity),
            root.trace_id,
            professional=True,
            revision_rounds=AUTOMATIC_REVISION_ROUNDS,
            settings=settings,
            producer_parent_run_id=root.id,
            narrative=selected.narrative if selected else None,
            producer_context=shared_context,
        )
    elif task.kind == "director":
        source = db.get(Script, task.script_version_id)
        performance = has_series_scope(root) or bool(
            root.input_json.get("short_drama")
            and source is not None
            and source.content_json.get("schema_version") == "3.0"
        )
        child = start_run(
            db,
            episode_id,
            context.actor_id,
            "",
            str(identity),
            root.trace_id,
            script_version_id=task.script_version_id,
            professional_director=True,
            director_revision_rounds=AUTOMATIC_REVISION_ROUNDS,
            director_art_collaboration=task.art_collaboration,
            settings=settings,
            producer_parent_run_id=root.id,
            performance=performance,
            producer_context=shared_context,
        )
    elif task.kind == "art_director":
        child = start_run(
            db,
            episode_id,
            context.actor_id,
            "",
            str(identity),
            root.trace_id,
            script_version_id=task.script_version_id,
            art_task=task.task,
            art_concept_plan=task.concept_plan,
            settings=settings,
            producer_parent_run_id=root.id,
            producer_context=shared_context,
        )
    else:
        from arclight.application.continuity_workflow import start_candidate_review

        child = start_candidate_review(
            db,
            settings,
            context.project_id,
            task.candidate_id,
            context.actor_id,
            CandidateReviewCreate(storyboard_id=task.storyboard_id, shot_id=task.shot_id),
            producer_parent_run_id=root.id,
            producer_context=shared_context,
        )
    result = DelegationResult(
        **request.model_dump(),
        delegation_id=identity,
        child_run_id=child.id,
        root_run_id=root.id,
        task_kind=task.kind,
    )
    validate_saved_child(context, root, planned, result, control, selected)
    return result.model_dump(mode="json")


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
