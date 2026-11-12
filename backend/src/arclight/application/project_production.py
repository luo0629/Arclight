"""Read-only projections over authoritative task, review and render histories."""

from datetime import UTC, datetime
from typing import Any, get_args
from uuid import UUID

from sqlalchemy import String, case, cast, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from arclight.application.commands import require_project
from arclight.application.human_reviews import review_blocker
from arclight.application.render_deliveries import delivery_read
from arclight.core.errors import DomainError
from arclight.domain.project_production import (
    PendingProjectReview,
    PendingProjectReviewPage,
    ProjectDelivery,
    ProjectDeliveryPage,
    ProjectTask,
    ProjectTaskPage,
    PublicError,
    ReviewBlocker,
    ReviewKind,
    TaskStatus,
    TaskType,
    WorkflowType,
)
from arclight.domain.render_inspections import CheckName, InspectionFailure
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    GenerationAttempt,
    GenerationJob,
    RenderArtifact,
    RenderExecution,
    RenderJob,
    RenderManifest,
    Script,
    Storyboard,
    StoryOutline,
    Timeline,
    VisualAsset,
    WorkflowRun,
)


def _scope(db: Session, project_id: UUID, actor: str, episode_id: UUID | None):
    project = require_project(db, project_id, actor)
    if (
        episode_id is not None
        and db.scalar(
            select(Episode.id).where(Episode.id == episode_id, Episode.project_id == project_id)
        )
        is None
    ):
        raise DomainError("NOT_FOUND", "本项目的剧集不存在", 404)
    return project


def _safe(column: Any, vocabulary: Any, fallback: str, *, nullable: bool = False):
    conditions = [(column.in_(get_args(vocabulary)), column)]
    if nullable:
        conditions.insert(0, (column.is_(None), literal(None, type_=String)))
    return case(*conditions, else_=fallback)


# Only exact, server-defined codes enter a public category. A recognizable
# prefix on arbitrary provider diagnostics is never sufficient authorization.
PUBLIC_ERROR_ALIASES: dict[PublicError, tuple[str, ...]] = {
    "BUDGET_EXCEEDED": (
        "ART_CONCEPT_BUDGET",
        "BUDGET_EPISODE_EXCEEDED",
        "BUDGET_TASK_EXCEEDED",
        "BUDGET_BATCH_EXCEEDED",
        "BUDGET_UNPRICED_HISTORY",
        "BUDGET_QUOTE_UNAVAILABLE",
        "BUDGET_QUOTE_CHANGED",
    ),
    "PROVIDER_UNAVAILABLE": (
        "GATEWAY_QUOTA_EXHAUSTED",
        "GATEWAY_PROVIDER_RATE_LIMITED",
        "GATEWAY_PROVIDER_AUTHENTICATION_FAILED",
        "GATEWAY_PROVIDER_ACCESS_DENIED",
        "GATEWAY_PAUSED",
        "GATEWAY_CIRCUIT_OPEN",
        "GATEWAY_RATE_LIMITED",
        "PROVIDER_QUOTA_EXHAUSTED",
        "PROVIDER_RATE_LIMITED",
        "PROVIDER_AUTHENTICATION_FAILED",
        "PROVIDER_ACCESS_DENIED",
        "SERVICE_DNS_UNAVAILABLE",
        "SERVICE_ADDRESS_BLOCKED",
        "SERVICE_REDIRECT_BLOCKED",
        "INVALID_SERVICE_URL",
    ),
    "RESULT_UNKNOWN": ("GATEWAY_RECONCILIATION_REQUIRED", "ART_CONCEPT_UNRESOLVED"),
    "INPUT_INVALID": (
        "ART_CONCEPT_INPUT_CHANGED",
        "ART_CONCEPT_REFERENCE_LOCKED",
        "ART_CONCEPT_ROUND_LIMIT",
        "ART_CONCEPT_CANDIDATE_LIMIT",
        "BUDGET_CANDIDATE_LIMIT",
        "BUDGET_BATCH_MISMATCH",
        "CONTEXT_SNAPSHOT_INVALID",
        "CONTEXT_TOO_LARGE",
        "CONTEXT_INPUT_UNSAFE",
        "CONTEXT_REFERENCE_INVALID",
        "SCRIPT_DURATION_REQUIRED",
        "SCRIPT_DURATION_OUT_OF_RANGE",
        "STORYBOARD_DURATION_OUT_OF_RANGE",
        "PRODUCTION_VERSION_INCOMPATIBLE",
        "RENDER_INPUT_CHANGED",
        "RENDER_SOURCE_RANGE_INVALID",
        "RENDER_RIGHTS_REQUIRED",
        "ASSET_NOT_USABLE",
        "GATEWAY_SCOPE_INVALID",
        "GATEWAY_CAPABILITY_REJECTED",
        "GATEWAY_ROUTE_FORBIDDEN",
        "GATEWAY_ATTEMPT_LIMIT",
    ),
    "STORAGE_FAILED": (
        "GATEWAY_STORAGE_UNAVAILABLE",
        "RENDER_STORAGE_CHANGED",
        "OBJECT_UNAVAILABLE",
        "ART_CONCEPT_MEDIA_MISSING",
    ),
    "PROVIDER_FAILED": (
        "GATEWAY_PROVIDER_FAILED",
        "GATEWAY_MODEL_TRUNCATED",
        "GATEWAY_INVALID_RESPONSE",
        "INVALID_RESPONSE",
        "PROVIDER_TRUNCATED",
    ),
    "PROVIDER_REJECTED": ("GATEWAY_MODEL_REFUSED", "GATEWAY_NOT_ACCEPTED", "PROVIDER_REFUSED"),
    "WORKFLOW_FAILED": ("WORKFLOW_EXECUTION_FAILED", "RUN_STATE_INVALID"),
    "CANCELLED": ("RENDER_CANCELLED",),
    "RENDER_INTERRUPTED": ("RENDER_WORKER_INTERRUPTED", "RENDER_RECOVERY_EXHAUSTED"),
    "RENDER_TIMEOUT": ("RENDER_TIME_LIMIT", "RENDER_MEDIA_TIME_LIMIT"),
    "RENDER_OUTPUT_INVALID": tuple(f"RENDER_OUTPUT_{name.upper()}" for name in get_args(CheckName)),
    "INSPECTION_FAILED": tuple(f"RENDER_INSPECTION_{name}" for name in get_args(InspectionFailure)),
}


def _public_error(column: Any):
    return case(
        (column.is_(None), literal(None, type_=String)),
        (column.in_(get_args(PublicError)), column),
        *((column.in_(aliases), category) for category, aliases in PUBLIC_ERROR_ALIASES.items()),
        else_="INTERNAL_ERROR",
    )


def _uuid_text(column: Any):
    # PostgreSQL UUID casts include hyphens; SQLite's UUID storage does not.
    return func.replace(cast(column, String), "-", "")


def _episode_url(project_id: UUID, episode_id: UUID | None, view: str) -> str:
    if episode_id is None:
        return f"/projects/{project_id}?section=tasks"
    return f"/projects/{project_id}/episodes/{episode_id}?view={view}"


def _human_review_url(project_id: UUID, data: dict[str, Any]) -> str:
    episode_id = data["episode_id"]
    kind = data["kind"]
    if kind in {"SCRIPT", "STORYBOARD"} and episode_id is not None:
        destination = _episode_url(project_id, episode_id, "list") + (
            f"&node={kind.lower()}:{data['version_id']}"
        )
        if data["run_id"] is not None:
            destination += f"&run={data['run_id']}"
        return destination
    if kind in {"REFERENCE", "VISUAL_SPEC"}:
        return f"/projects/{project_id}?section=assets&proposal={data['object_id']}" + (
            f"&proposal_version={data['version_id']}"
        )
    if kind == "EXCEPTIONAL_SHOT" and data["asset_id"] is not None:
        return f"/projects/{project_id}?section=assets&asset={data['asset_id']}" + (
            f"&asset_review={data['version_id']}&review={data['id']}"
        )
    if episode_id is not None and kind == "EXPORT":
        return _episode_url(project_id, episode_id, "render") + (
            f"&render_manifest={data['object_id']}&review={data['id']}"
        )
    if episode_id is not None and kind == "DELIVERY":
        return _episode_url(project_id, episode_id, "render") + (f"&delivery={data['object_id']}")
    return f"/projects/{project_id}?section=reviews&review={data['id']}"


def _task_query(project_id: UUID):
    latest_error = (
        select(GenerationAttempt.error_code)
        .where(GenerationAttempt.job_id == GenerationJob.id)
        .order_by(GenerationAttempt.attempt_no.desc())
        .limit(1)
        .correlate(GenerationJob)
        .scalar_subquery()
    )

    def columns(model, family, project, task_type, workflow, error):
        return (
            model.id.label("id"),
            literal(family).label("family"),
            project.label("project_id"),
            Episode.id.label("episode_id"),
            Episode.episode_no,
            Episode.title.label("episode_title"),
            task_type.label("task_type"),
            workflow.label("workflow_type"),
            _safe(model.status, TaskStatus, "UNKNOWN").label("status"),
            _public_error(error).label("error_code"),
            model.created_at,
            model.updated_at,
        )

    workflows = (
        select(
            *columns(
                WorkflowRun,
                "WORKFLOW",
                Episode.project_id,
                literal("TEXT"),
                _safe(WorkflowRun.workflow_type, WorkflowType, "unknown"),
                WorkflowRun.error_code,
            )
        )
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(Episode.project_id == project_id)
    )
    generation = (
        select(
            *columns(
                GenerationJob,
                "GENERATION",
                GenerationJob.project_id,
                _safe(GenerationJob.task_type, TaskType, "OTHER"),
                literal(None, type_=String),
                latest_error,
            )
        )
        .outerjoin(Episode, Episode.id == GenerationJob.episode_id)
        .where(
            GenerationJob.project_id == project_id,
            or_(GenerationJob.episode_id.is_(None), Episode.project_id == project_id),
        )
    )
    renders = (
        select(
            *columns(
                RenderExecution,
                "RENDER",
                RenderExecution.project_id,
                literal("RENDER"),
                literal(None, type_=String),
                RenderExecution.error_code,
            )
        )
        .outerjoin(RenderJob, RenderJob.id == RenderExecution.id)
        .outerjoin(
            RenderManifest,
            RenderManifest.id == RenderJob.manifest_id,
        )
        .outerjoin(Timeline, Timeline.id == RenderManifest.timeline_id)
        .outerjoin(
            Episode,
            Episode.id == Timeline.episode_id,
        )
        .where(
            RenderExecution.project_id == project_id,
            or_(Episode.id.is_(None), Episode.project_id == project_id),
        )
    )
    return union_all(workflows, generation, renders).subquery()


def project_tasks(
    db: Session,
    project_id: UUID,
    actor: str,
    *,
    episode_id: UUID | None = None,
    task_type: str | None = None,
    status: str | None = None,
    error_code: str | None = None,
    created_from: datetime | None = None,
    created_to: datetime | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ProjectTaskPage:
    _scope(db, project_id, actor, episode_id)
    for boundary in (created_from, created_to):
        if boundary is not None and boundary.utcoffset() is None:
            raise DomainError("INVALID_TIME_RANGE", "筛选时间必须包含时区", 422)
    created_from = created_from.astimezone(UTC) if created_from is not None else None
    created_to = created_to.astimezone(UTC) if created_to is not None else None
    if created_from and created_to and created_from >= created_to:
        raise DomainError("INVALID_TIME_RANGE", "结束时间必须晚于开始时间", 422)
    rows = _task_query(project_id)
    query = select(rows)
    for column, value in (
        (rows.c.episode_id, episode_id),
        (rows.c.task_type, task_type),
        (rows.c.status, status),
        (rows.c.error_code, error_code),
    ):
        if value is not None:
            query = query.where(column == value)
    if created_from is not None:
        query = query.where(rows.c.created_at >= created_from)
    if created_to is not None:
        query = query.where(rows.c.created_at < created_to)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = db.execute(
        query.order_by(
            rows.c.created_at.desc(),
            rows.c.family,
            rows.c.id,
        )
        .limit(limit)
        .offset(offset)
    ).mappings()
    items = []
    for row in page:
        parameter = {"WORKFLOW": "run", "GENERATION": "generation_job", "RENDER": "render_job"}[
            row["family"]
        ]
        detail = _episode_url(
            project_id, row["episode_id"], "render" if row["family"] == "RENDER" else "tasks"
        )
        items.append(
            ProjectTask.model_validate(
                {
                    **row,
                    "detail_url": f"{detail}&{parameter}={row['id']}",
                }
            )
        )
    return ProjectTaskPage(items=items, total=total, limit=limit, offset=offset)


def _review_query(project_id: UUID):
    request = HumanReviewRequest
    version = request.snapshot_json["version_id"].as_string()
    kind = _safe(request.snapshot_json["kind"].as_string(), ReviewKind, "WORKFLOW")

    def columns(model, source, kind, object_id, version_id, run_id):
        return (
            model.id.label("id"),
            literal(source).label("source"),
            Episode.project_id.label("project_id"),
            Episode.id.label("episode_id"),
            Episode.episode_no,
            Episode.title.label("episode_title"),
            kind.label("kind"),
            object_id.label("object_id"),
            version_id.label("version_id"),
            run_id.label("run_id"),
            model.created_at,
            Episode.status.label("episode_status"),
        )

    human = (
        select(
            request.id.label("id"),
            literal("HUMAN_REVIEW").label("source"),
            request.project_id,
            request.episode_id,
            Episode.episode_no,
            Episode.title.label("episode_title"),
            kind.label("kind"),
            request.snapshot_json["object_id"].as_string().label("object_id"),
            version.label("version_id"),
            request.run_id,
            request.created_at,
            Episode.status.label("episode_status"),
        )
        .outerjoin(Episode, Episode.id == request.episode_id)
        .where(
            request.project_id == project_id,
            or_(request.episode_id.is_(None), Episode.project_id == project_id),
            ~select(HumanReviewDecision.id)
            .where(HumanReviewDecision.request_id == request.id)
            .exists(),
            ~select(HumanReviewResume.id)
            .where(HumanReviewResume.request_id == request.id)
            .exists(),
        )
    )

    def logged(identity):
        return (
            select(request.id)
            .where(
                request.project_id == project_id,
                _uuid_text(version) == _uuid_text(identity),
            )
            .exists()
        )

    selections = [human]
    for model, name in ((Script, "SCRIPT"), (Storyboard, "STORYBOARD")):
        selections.append(
            select(
                *columns(
                    model,
                    name,
                    literal(name),
                    cast(model.id, String),
                    cast(model.id, String),
                    model.run_id,
                )
            )
            .join(Episode, Episode.id == model.episode_id)
            .where(
                Episode.project_id == project_id,
                model.run_id.is_(None),
                model.status == "READY_FOR_REVIEW",
                ~logged(model.id),
            )
        )
    pending_version = WorkflowRun.pending_review["version_id"].as_string()
    pending_request = WorkflowRun.pending_review["review_request_id"].as_string()
    exact_logged = (
        select(request.id)
        .where(
            request.project_id == project_id,
            request.run_id == WorkflowRun.id,
            or_(
                _uuid_text(version) == _uuid_text(pending_version),
                _uuid_text(request.id) == _uuid_text(pending_request),
                _uuid_text(request.snapshot_json["object_id"].as_string())
                == _uuid_text(WorkflowRun.pending_review["review_id"].as_string()),
                _uuid_text(request.snapshot_json["object_id"].as_string())
                == _uuid_text(WorkflowRun.pending_review["loop_id"].as_string()),
            ),
        )
        .exists()
    )
    selections.append(
        select(
            *columns(
                WorkflowRun,
                "WORKFLOW",
                literal("WORKFLOW"),
                cast(WorkflowRun.id, String),
                pending_version,
                WorkflowRun.id,
            )
        )
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(
            Episode.project_id == project_id,
            or_(
                WorkflowRun.status == "WAITING_REVIEW",
                (
                    (WorkflowRun.status == "RUNNING")
                    & WorkflowRun.pending_review["resource_type"].as_string().is_not(None)
                ),
            ),
            ~exact_logged,
        )
    )
    selections.append(
        select(
            *columns(
                StoryOutline,
                "OUTLINE",
                literal("OUTLINE"),
                cast(StoryOutline.id, String),
                cast(StoryOutline.id, String),
                StoryOutline.run_id,
            )
        )
        .join(Episode, Episode.id == StoryOutline.episode_id)
        .where(
            StoryOutline.project_id == project_id,
            Episode.project_id == project_id,
            StoryOutline.status == "DRAFT",
            ~logged(StoryOutline.id),
        )
    )
    return union_all(*selections).subquery()


def pending_project_reviews(
    db: Session,
    project_id: UUID,
    actor: str,
    *,
    episode_id: UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> PendingProjectReviewPage:
    project = _scope(db, project_id, actor, episode_id)
    rows = _review_query(project_id)
    query = select(rows)
    if episode_id is not None:
        query = query.where(rows.c.episode_id == episode_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    page = db.execute(
        query.order_by(
            rows.c.created_at.desc(),
            rows.c.source,
            rows.c.id,
        )
        .limit(limit)
        .offset(offset)
    ).mappings()
    items = []
    for row in page:
        data = dict(row)
        episode_status = data.pop("episode_status")
        identity = UUID(str(data["id"]))
        for name in ("object_id", "version_id"):
            data[name] = UUID(str(data[name])) if data[name] is not None else None
        data["asset_id"] = None
        if data["source"] == "HUMAN_REVIEW" and data["version_id"] is not None:
            data["asset_id"] = db.scalar(
                select(AssetVersion.asset_id)
                .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
                .where(AssetVersion.id == data["version_id"], VisualAsset.project_id == project_id)
            )
        blocker = (
            "PROJECT_ARCHIVED"
            if project.archived
            else ("EPISODE_ARCHIVED" if episode_status == "ARCHIVED" else None)
        )
        if data["source"] == "HUMAN_REVIEW":
            request = db.get(HumanReviewRequest, identity)
            assert request is not None
            found = review_blocker(db, request)
            blocker = (
                found
                if found in get_args(ReviewBlocker)
                else ("REVIEW_UNAVAILABLE" if found else None)
            )
            detail = _human_review_url(project_id, data)
        elif data["source"] in {"SCRIPT", "STORYBOARD"}:
            detail = _episode_url(project_id, data["episode_id"], "list")
            detail += f"&node={data['source'].lower()}:{identity}"
        elif data["source"] == "OUTLINE":
            detail = _episode_url(project_id, data["episode_id"], "outline")
            detail += f"&outline={identity}"
        else:
            detail = _episode_url(project_id, data["episode_id"], "tasks") + f"&run={identity}"
        items.append(
            PendingProjectReview.model_validate(
                {
                    **data,
                    "status": "BLOCKED" if blocker else "WAITING_REVIEW",
                    "blocking_code": blocker,
                    "detail_url": detail,
                }
            )
        )
    return PendingProjectReviewPage(items=items, total=total, limit=limit, offset=offset)


def project_deliveries(
    db: Session,
    project_id: UUID,
    actor: str,
    *,
    episode_id: UUID | None = None,
    limit: int = 20,
    offset: int = 0,
) -> ProjectDeliveryPage:
    _scope(db, project_id, actor, episode_id)
    query = (
        select(RenderArtifact, Episode)
        .join(
            RenderManifest,
            RenderManifest.id == RenderArtifact.manifest_id,
        )
        .join(Timeline, Timeline.id == RenderManifest.timeline_id)
        .join(
            Episode,
            Episode.id == Timeline.episode_id,
        )
        .join(RenderExecution, RenderExecution.id == RenderArtifact.job_id)
        .where(
            Episode.project_id == project_id,
            RenderManifest.project_id == project_id,
            RenderExecution.project_id == project_id,
            RenderExecution.status == "SUCCEEDED",
        )
    )
    if episode_id is not None:
        query = query.where(Episode.id == episode_id)
    total = db.scalar(select(func.count()).select_from(query.subquery())) or 0
    rows = db.execute(
        query.order_by(
            RenderArtifact.created_at.desc(),
            RenderArtifact.id.desc(),
        )
        .limit(limit)
        .offset(offset)
    )
    return ProjectDeliveryPage(
        items=[
            ProjectDelivery(
                **delivery_read(db, artifact, episode.id, actor).model_dump(),
                episode_no=episode.episode_no,
                episode_title=episode.title,
                detail_url=_episode_url(project_id, episode.id, "render")
                + f"&delivery={artifact.id}",
            )
            for artifact, episode in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )
