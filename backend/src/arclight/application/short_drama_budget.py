"""Verify text and media ownership for a short-drama production grant."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.application.commands import require_episode
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.production_versions import bind, find_binding
from arclight.core.errors import DomainError
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.generation import GenerationJob
from arclight.persistence.models import WorkflowRun

MEDIA_SCOPE = "SHORT_DRAMA_MEDIA_JOB"


def root_for_workflow(db: Session, workflow_id: UUID | None) -> WorkflowRun | None:
    if workflow_id is None:
        return None
    workflow = db.get(WorkflowRun, workflow_id)
    if workflow is None:
        return None
    if workflow.input_json.get("short_drama"):
        return workflow
    if workflow.workflow_type not in {
        "script_storyboard",
        "storyboard",
        "art_proposal",
        "continuity_review",
    } or not workflow.input_json.get("created_by"):
        return None
    control = db.get(AgentRunLimit, workflow_id)
    root = db.get(WorkflowRun, control.root_run_id) if control else None
    return root if root and root.input_json.get("short_drama") else None


def require_manual_workflow(db: Session, workflow_id: UUID | None) -> None:
    if root_for_workflow(db, workflow_id) is not None:
        raise DomainError("SHORT_DRAMA_AUTOMATIC", "本次短剧由后台自动推进", 409)


def root_for_job(db: Session, job: GenerationJob) -> WorkflowRun | None:
    scope = find_binding(db, job.project_id, MEDIA_SCOPE, job.id)
    if scope is None:
        return None
    root = db.get(WorkflowRun, UUID(scope.snapshot_json["run_id"]), populate_existing=True)
    if root is None or not root.input_json.get("short_drama"):
        raise DomainError("SHORT_DRAMA_GRANT_REQUIRED", "媒体任务缺少原短剧制作授权", 409)
    return root


def bind_media_job(db: Session, run_id: UUID, job: GenerationJob) -> None:
    if job.episode_id is None:
        raise DomainError("SHORT_DRAMA_MEDIA_SCOPE", "本次媒体任务必须属于原制作剧集", 409)
    episode = require_episode(db, job.episode_id, job.created_by, owner=True, write=True)
    root = db.get(WorkflowRun, run_id, populate_existing=True)
    if (
        root is None
        or root.workflow_type != "producer"
        or root.status != "RUNNING"
        or root.episode_id != episode.id
        or episode.project_id != job.project_id
        or root.input_json.get("created_by") != job.created_by
        or not root.input_json.get("short_drama")
    ):
        raise DomainError("SHORT_DRAMA_MEDIA_SCOPE", "媒体任务与本次短剧制作授权不一致", 409)
    bind(db, job.project_id, MEDIA_SCOPE, job.id, {"run_id": str(run_id)})


def require_active_production(db: Session, root: WorkflowRun, project_id: UUID) -> None:
    from arclight.application.short_drama import require_exact_inputs
    from arclight.application.short_drama_delivery import require_run_authorization

    lock_dependency_graph(db, project_id)
    db.refresh(root)
    control = db.get(AgentRunLimit, root.id)
    if (
        root.status != "RUNNING"
        or control is None
        or control.root_run_id != root.id
        or control.project_id != project_id
    ):
        raise DomainError("SHORT_DRAMA_INACTIVE", "本次短剧制作当前不可执行", 409)
    episode, _, spec = require_run_authorization(db, root)
    if episode.project_id != project_id:
        raise DomainError("SHORT_DRAMA_INACTIVE", "本次短剧制作不属于此项目", 409)
    require_exact_inputs(db, episode, spec)
