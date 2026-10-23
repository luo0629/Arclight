from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Query, Request
from sqlalchemy import or_, select

from arclight.agents.contracts import ScriptContent
from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import (
    AcceptedRun,
    DialogueLineRead,
    EpisodeCreate,
    EpisodeRead,
    EventRead,
    ProjectCreate,
    ProjectRead,
    ReviewCreate,
    RunCreate,
    RunRead,
    ScriptRead,
    ShotRead,
    StoryboardRead,
    StoryboardRunCreate,
)
from arclight.application.commands import (
    cancel_run,
    require_episode,
    require_project,
    require_run,
    resume_run,
    retry_run,
    start_run,
)
from arclight.core.errors import DomainError
from arclight.persistence.models import (
    Episode,
    Project,
    ProjectMember,
    Script,
    Shot,
    Storyboard,
    WorkflowEvent,
)

router = APIRouter(prefix="/api/v1")
Limit = Annotated[int, Query(ge=1, le=100)]
Offset = Annotated[int, Query(ge=0)]


@router.post("/projects", tags=["projects"], status_code=201)
def create_project(payload: ProjectCreate, db: Database, actor: Actor) -> ProjectRead:
    project = Project(owner_id=actor, **payload.model_dump())
    db.add(project)
    db.flush()
    return ProjectRead.model_validate(project)


@router.get("/projects", tags=["projects"])
def list_projects(
    db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[ProjectRead]:
    visible = Project.owner_id == actor
    if actor != "service-owner":
        visible = or_(
            visible,
            Project.id.in_(
                select(ProjectMember.project_id).where(ProjectMember.user_id == UUID(actor))
            ),
        )
    rows = db.scalars(
        select(Project)
        .where(visible)
        .order_by(Project.updated_at.desc(), Project.id)
        .limit(limit)
        .offset(offset)
    )
    return [ProjectRead.model_validate(row) for row in rows]


@router.get("/projects/{project_id}", tags=["projects"])
def get_project(project_id: UUID, db: Database, actor: Actor) -> ProjectRead:
    return ProjectRead.model_validate(require_project(db, project_id, actor))


@router.post("/projects/{project_id}/episodes", tags=["episodes"], status_code=201)
def create_episode(
    project_id: UUID, payload: EpisodeCreate, db: Database, actor: Actor
) -> EpisodeRead:
    require_project(db, project_id, actor, write=True)
    episode = Episode(project_id=project_id, **payload.model_dump())
    db.add(episode)
    db.flush()
    return EpisodeRead.model_validate(episode)


@router.get("/projects/{project_id}/episodes", tags=["episodes"])
def list_episodes(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[EpisodeRead]:
    require_project(db, project_id, actor)
    return [
        EpisodeRead.model_validate(row)
        for row in db.scalars(
            select(Episode)
            .where(Episode.project_id == project_id)
            .order_by(Episode.episode_no)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/episodes/{episode_id}", tags=["episodes"])
def get_episode(episode_id: UUID, db: Database, actor: Actor) -> EpisodeRead:
    return EpisodeRead.model_validate(require_episode(db, episode_id, actor))


@router.post("/episodes/{episode_id}/script-runs", tags=["workflows"], status_code=202)
def create_script_run(
    episode_id: UUID,
    payload: RunCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    if len(payload.story) > request.app.state.settings.max_story_chars:
        raise DomainError("STORY_TOO_LARGE", "故事超出当前服务长度限制", 422)
    run = start_run(db, episode_id, actor, payload.story, key, request.state.request_id)
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.post("/episodes/{episode_id}/storyboard-runs", tags=["workflows"], status_code=202)
def create_storyboard_run(
    episode_id: UUID,
    payload: StoryboardRunCreate,
    request: Request,
    db: Database,
    actor: Actor,
    key: Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=128)],
) -> AcceptedRun:
    run = start_run(
        db,
        episode_id,
        actor,
        "",
        key,
        request.state.request_id,
        script_version_id=payload.script_version_id,
    )
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.get("/runs/{run_id}", tags=["workflows"])
def get_run(run_id: UUID, db: Database, actor: Actor) -> RunRead:
    return RunRead.model_validate(require_run(db, run_id, actor))


@router.post("/runs/{run_id}/resume", tags=["workflows"], status_code=202)
def review_and_resume(run_id: UUID, payload: ReviewCreate, db: Database, actor: Actor) -> RunRead:
    run = require_run(db, run_id, actor, lock=True)
    resume_run(db, run, actor, payload)
    db.flush()
    return RunRead.model_validate(run)


@router.post("/runs/{run_id}/cancel", tags=["workflows"])
def cancel(run_id: UUID, db: Database, actor: Actor) -> RunRead:
    run = cancel_run(db, require_run(db, run_id, actor, lock=True))
    db.flush()
    return RunRead.model_validate(run)


@router.get("/runs/{run_id}/events", tags=["workflows"])
def get_events(
    run_id: UUID, db: Database, actor: Actor, limit: Limit = 50, offset: Offset = 0
) -> list[EventRead]:
    require_run(db, run_id, actor)
    return [
        EventRead.model_validate(row)
        for row in db.scalars(
            select(WorkflowEvent)
            .where(WorkflowEvent.run_id == run_id)
            .order_by(WorkflowEvent.created_at, WorkflowEvent.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/episodes/{episode_id}/scripts", tags=["scripts"])
def get_scripts(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[ScriptRead]:
    require_episode(db, episode_id, actor)
    return [
        ScriptRead.model_validate(row)
        for row in db.scalars(
            select(Script)
            .where(Script.episode_id == episode_id)
            .order_by(Script.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/episodes/{episode_id}/dialogue-lines", tags=["scripts"])
def get_dialogue_lines(
    episode_id: UUID,
    script_version_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[DialogueLineRead]:
    require_episode(db, episode_id, actor)
    script = db.get(Script, script_version_id)
    if script is None or script.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "剧本版本不存在", 404)
    content = ScriptContent.model_validate(script.content_json)
    lines = [
        {
            "script_version_id": str(script.id),
            "scene_id": scene.scene_id,
            "scene_no": scene.scene_no,
            "sequence_no": index + 1,
            **line.model_dump(),
        }
        for scene in content.scenes
        for index, line in enumerate(scene.dialogue)
    ]
    return [DialogueLineRead.model_validate(row) for row in lines[offset : offset + limit]]


@router.get("/episodes/{episode_id}/storyboards", tags=["storyboards"])
def get_storyboards(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[StoryboardRead]:
    require_episode(db, episode_id, actor)
    return [
        StoryboardRead.model_validate(row)
        for row in db.scalars(
            select(Storyboard)
            .where(Storyboard.episode_id == episode_id)
            .order_by(Storyboard.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/episodes/{episode_id}/shots", tags=["storyboards"])
def get_shots(
    episode_id: UUID,
    db: Database,
    actor: Actor,
    storyboard_id: UUID,
    limit: Limit = 100,
    offset: Offset = 0,
) -> list[ShotRead]:
    require_episode(db, episode_id, actor)
    storyboard = db.get(Storyboard, storyboard_id)
    if storyboard is None or storyboard.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "分镜版本不存在", 404)
    return [
        ShotRead.model_validate(row)
        for row in db.scalars(
            select(Shot)
            .where(Shot.storyboard_id == storyboard_id)
            .order_by(Shot.shot_no)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.post("/runs/{run_id}/retry", tags=["workflows"], status_code=202)
def retry(run_id: UUID, db: Database, actor: Actor) -> RunRead:
    run = retry_run(db, require_run(db, run_id, actor, lock=True))
    db.flush()
    return RunRead.model_validate(run)
