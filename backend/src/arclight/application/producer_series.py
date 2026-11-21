"""Frozen multi-episode scope and exact inputs for the existing Producer services."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import require_episode
from arclight.application.dependencies import exact_version, input_usable
from arclight.application.human_reviews import require_review_actor
from arclight.application.production_versions import bind, find_binding
from arclight.application.writing_context import (
    capture_narrative_context,
    validate_narrative_context,
)
from arclight.core.errors import DomainError
from arclight.domain.producer_planning import ProducerPlan, ProducerTask
from arclight.domain.producer_series import (
    SERIES_PRODUCER_VERSION,
    SeriesProducerPlan,
    SeriesProducerScope,
    SeriesTaskContext,
)
from arclight.domain.script_narrative import NarrativeRequest
from arclight.domain.series_planning import SeriesOutlineContent
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, Script, StoryOutline, WorkflowRun

SCOPE_KIND = "PRODUCER_SERIES_SCOPE"


def has_series_scope(run: WorkflowRun) -> bool:
    """The admitted project scope determines the operation; the version verifies it."""
    series = "series_scope" in run.input_json
    if series != (run.workflow_version == SERIES_PRODUCER_VERSION):
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集运行与冻结范围不符", 409)
    return series


def require_available_episode(db: Session, episode: Episode, parent_id: UUID | None = None) -> None:
    roots = db.scalars(
        select(WorkflowRun)
        .join(Episode)
        .where(
            Episode.project_id == episode.project_id,
            WorkflowRun.workflow_type == "producer",
            WorkflowRun.workflow_version == SERIES_PRODUCER_VERSION,
            WorkflowRun.status.in_(["QUEUED", "RUNNING", "WAITING_REVIEW"]),
        )
    )
    if any(root.id != parent_id and contains_episode(db, root, episode.id) for root in roots):
        raise DomainError("EPISODE_BUSY", "本集已属于活动多集制作范围", 409)


def require_known_costs(db: Session, run: WorkflowRun, actor: str) -> None:
    if not has_series_scope(run):
        return
    from arclight.application.costs import cost_facts, read_costs

    scope = read_scope(db, run)
    project_id = UUID(scope["project_id"])
    facts = cost_facts(project_id)
    unresolved = db.scalar(
        select(facts.c.id)
        .where(
            facts.c.status.in_(["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"]),
            facts.c.actual_cost.is_(None),
        )
        .limit(1)
    )
    if unresolved or read_costs(db, project_id, actor).summary.unpriced_count:
        raise DomainError("PRODUCER_COST_UNKNOWN", "项目存在未知费用，请先对账再继续制作", 409)


def capture_scope(db: Session, anchor: Episode, actor: str, request: SeriesProducerScope) -> dict:
    require_review_actor(db, anchor.project_id, actor, owner=True)
    if anchor.id != request.episodes[0].episode_id:
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集运行须以明确生产顺序的首集为锚点", 422)
    baseline = capture_narrative_context(
        db,
        anchor,
        actor,
        NarrativeRequest(
            series_plan_version_id=request.series_plan_version_id,
            ledger_version_id=request.ledger_version_id,
            expected_ledger_head_revision=request.expected_ledger_head_revision,
            target_story_time=0,
        ),
    )
    plan = db.get(StoryOutline, request.series_plan_version_id)
    assert plan is not None
    content = SeriesOutlineContent.model_validate(plan.content_json)
    episodes = []
    for item in request.episodes:
        episode = require_episode(db, item.episode_id, actor, write=True)
        if episode.project_id != anchor.project_id:
            raise DomainError("NOT_FOUND", "同项目剧集不存在", 404)
        if episode.status == "ARCHIVED" or episode.episode_no > content.target_episode_count:
            raise DomainError("PRODUCER_SERIES_SCOPE", "制作范围必须属于批准规划且未归档", 409)
        episodes.append(
            item.model_dump(mode="json")
            | {"episode_no": episode.episode_no, "target_duration_ms": episode.target_duration_ms}
        )
    selected_ids = {row["episode_id"] for row in episodes}
    active = db.scalars(
        select(WorkflowRun)
        .join(Episode)
        .where(
            Episode.project_id == anchor.project_id,
            WorkflowRun.status.in_(["QUEUED", "RUNNING", "WAITING_REVIEW"]),
            WorkflowRun.workflow_type != "continuity_review",
        )
    )
    for run in active:
        occupied = {str(run.episode_id)}
        if has_series_scope(run):
            occupied = {row["episode_id"] for row in read_scope(db, run)["episodes"]}
        if occupied & selected_ids:
            raise DomainError("EPISODE_BUSY", "制作范围与已有活动运行重叠", 409)
    value = {
        "schema_version": SERIES_PRODUCER_VERSION,
        "project_id": str(anchor.project_id),
        "request": request.model_dump(mode="json"),
        "baseline": baseline,
        "target_episode_count": content.target_episode_count,
        "episodes": episodes,
        "plan": content.series_plan.model_dump(mode="json"),
    }
    from arclight.domain.context import ContextLimits, check_size

    check_size(value, ContextLimits())
    return value


def save_scope(db: Session, project_id: UUID, run: WorkflowRun) -> None:
    if has_series_scope(run):
        bind(db, project_id, SCOPE_KIND, run.id, run.input_json["series_scope"])


def read_scope(db: Session, run: WorkflowRun) -> dict:
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    binding = find_binding(db, episode.project_id, SCOPE_KIND, run.id)
    if (
        run.workflow_version != SERIES_PRODUCER_VERSION
        or binding is None
        or binding.snapshot_json != run.input_json.get("series_scope")
        or binding.snapshot_json.get("project_id") != str(episode.project_id)
    ):
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集运行须绑定原不可变范围", 409)
    return binding.snapshot_json


def contains_episode(db: Session, run: WorkflowRun, episode_id: UUID | None) -> bool:
    if episode_id is None:
        return False
    if not has_series_scope(run):
        return run.episode_id == episode_id
    return str(episode_id) in {item["episode_id"] for item in read_scope(db, run)["episodes"]}


def task_context(root: WorkflowRun, plan: ProducerPlan, task_key: str) -> SeriesTaskContext | None:
    if not has_series_scope(root):
        if isinstance(plan, SeriesProducerPlan):
            raise DomainError("PRODUCER_SERIES_SCOPE", "单集运行不能接受多集计划", 409)
        return None
    if not isinstance(plan, SeriesProducerPlan):
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集任务需要明确目标集和输入合同", 409)
    return next(item for item in plan.task_contexts if item.task_key == task_key)


def delegated_context(
    root: WorkflowRun,
    context: SeriesTaskContext | None,
    dependency_script_ids: list[UUID] | None = None,
) -> dict | None:
    if context is None:
        return None
    return {
        "parent_run_id": str(root.id),
        "scope_sha256": digest(root.input_json["series_scope"]),
        "task_context": context.model_dump(mode="json"),
        "dependency_script_version_ids": sorted(
            str(identity) for identity in dependency_script_ids or []
        ),
    }


def validate_task_context(
    db: Session,
    root: WorkflowRun,
    planned: ProducerTask,
    actor: str,
    context: SeriesTaskContext | None,
    child: WorkflowRun | None = None,
) -> Episode:
    if not has_series_scope(root):
        return require_episode(db, root.episode_id, actor, write=True)
    scope = read_scope(db, root)
    if context is None or context.task_key != planned.task_key:
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集任务缺少精确目标集", 409)
    selected = next(
        (item for item in scope["episodes"] if item["episode_id"] == str(context.episode_id)), None
    )
    if selected is None:
        raise DomainError("PRODUCER_SERIES_SCOPE", "不能委派冻结范围以外的剧集", 409)
    episode = require_episode(db, context.episode_id, actor, write=True)
    if (
        str(episode.project_id) != scope["project_id"]
        or episode.status == "ARCHIVED"
        or episode.episode_no != selected["episode_no"]
        or episode.target_duration_ms != selected["target_duration_ms"]
    ):
        raise DomainError("PRODUCER_SERIES_SCOPE", "目标集身份／规格已变化", 409)
    if planned.task.kind == "screenwriter":
        if (
            context.narrative is None
            or str(context.narrative.series_plan_version_id)
            != scope["request"]["series_plan_version_id"]
        ):
            raise DomainError("PRODUCER_SERIES_INPUT", "编剧须实际使用冻结规划和精确剧情状态", 409)
        if child is None:
            capture_narrative_context(db, episode, actor, context.narrative)
        else:
            frozen = child.input_json.get("narrative_context")
            if not isinstance(frozen, dict) or any(
                frozen.get(key) != value
                for key, value in context.narrative.model_dump(mode="json").items()
                if key != "character_ids" or value
            ):
                raise DomainError("PRODUCER_SERIES_INPUT", "已委派任务不能更换原叙事输入", 409)
            validate_narrative_context(db, episode, actor, frozen)
    elif context.narrative is not None:
        raise DomainError("PRODUCER_SERIES_INPUT", "非编剧任务从其精确批准输入继承剧情来源", 422)
    from arclight.application.art_research_assets import allowed_for_episode

    for identity in context.shared_version_ids:
        version, asset = exact_version(db, episode.project_id, identity)
        if not input_usable(db, version, asset, "TASK_CONTEXT") or not allowed_for_episode(
            asset, episode.id
        ):
            raise DomainError("PRODUCER_SERIES_INPUT", "共享精确版本不可用或不属于本集", 409)
    seen = set()
    for identity in context.prerequisite_script_version_ids:
        script = db.get(Script, identity, populate_existing=True)
        source_episode = db.get(Episode, script.episode_id) if script else None
        if (
            script is None
            or source_episode is None
            or source_episode.project_id != episode.project_id
            or source_episode.status == "ARCHIVED"
            or script.status != "APPROVED"
        ):
            raise DomainError("PRODUCER_PREREQUISITE_PENDING", "前集精确剧本须先独立批准", 409)
        version, asset = exact_version(db, episode.project_id, identity)
        if not input_usable(db, version, asset, "TASK_CONTEXT") or script.episode_id in seen:
            raise DomainError("PRODUCER_SERIES_INPUT", "前集来源失效或重复", 409)
        seen.add(script.episode_id)
    return episode


def validate_series_plan(db: Session, root: WorkflowRun, plan: ProducerPlan) -> None:
    if not has_series_scope(root):
        return
    scope = read_scope(db, root)
    if not isinstance(plan, SeriesProducerPlan) or plan.series_scope_sha256 != digest(scope):
        raise DomainError("PRODUCER_SERIES_SCOPE", "多集计划不能更换冻结范围或来源", 409)
    contexts = {item.task_key: item for item in plan.task_contexts}
    tasks = {item.task_key: item for item in plan.tasks}
    for item in plan.tasks:
        context = contexts[item.task_key]
        selected = next(
            (row for row in scope["episodes"] if row["episode_id"] == str(context.episode_id)), None
        )
        if selected is None:
            raise DomainError("PRODUCER_SERIES_SCOPE", "计划包含范围以外剧集", 409)
        explicit = {
            str(script.episode_id)
            for identity in context.prerequisite_script_version_ids
            if (script := db.get(Script, identity)) is not None
        }
        pending = {
            str(contexts[key].episode_id)
            for key in item.depends_on
            if tasks[key].task.kind == "screenwriter"
        }
        if not set(selected["requires_episode_ids"]) <= explicit | pending:
            raise DomainError(
                "PRODUCER_PREREQUISITE_PENDING", "后集须明确依赖前集批准稿或编剧任务", 409
            )


def require_episode_prerequisites(
    db: Session,
    root: WorkflowRun,
    plan: ProducerPlan,
    planned: ProducerTask,
    context: SeriesTaskContext | None,
    actor: str,
) -> list[UUID]:
    if context is None:
        return []
    from arclight.application.producer_plan_history import (
        canonical_task,
        find_task_delegation,
        read_current_plan,
    )

    scope = read_scope(db, root)
    selected = next(
        row for row in scope["episodes"] if row["episode_id"] == str(context.episode_id)
    )
    approved = {
        str(script.episode_id)
        for identity in context.prerequisite_script_version_ids
        if (script := db.get(Script, identity)) is not None and script.status == "APPROVED"
    }
    record = read_current_plan(db, root, actor)
    sources = set(context.prerequisite_script_version_ids)
    for key in planned.depends_on:
        receipt = find_task_delegation(db, root.id, canonical_task(record, key))
        if receipt is None:
            continue
        child = db.get(WorkflowRun, receipt.child_run_id)
        if child is None or child.status != "SUCCEEDED":
            continue
        scripts = db.scalars(
            select(Script).where(Script.run_id == child.id, Script.status == "APPROVED")
        )
        for script in scripts:
            version, asset = exact_version(db, UUID(scope["project_id"]), script.id)
            if input_usable(db, version, asset, "TASK_CONTEXT"):
                approved.add(str(script.episode_id))
                sources.add(script.id)
    if not set(selected["requires_episode_ids"]) <= approved:
        raise DomainError("PRODUCER_PREREQUISITE_PENDING", "前集未确认，不能继续后集制作", 409)
    return sorted(sources, key=str)
