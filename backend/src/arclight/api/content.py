from typing import cast
from uuid import UUID

from fastapi import APIRouter, Request
from pydantic import Field
from sqlalchemy import func, select

from arclight.agents.contracts import ScriptContent, StoryboardContent
from arclight.api.collaboration import audit, require_idle
from arclight.api.content_history import require_version
from arclight.api.content_history import router as history_router
from arclight.api.dependencies import Actor, Database
from arclight.api.schemas import EpisodeRead, Input, ScriptRead, StoryboardRead
from arclight.application.commands import require_episode
from arclight.application.content_dependencies import register_content, review_registry
from arclight.application.content_duration import validate_duration
from arclight.application.content_references import (
    validate_script_assets,
    validate_storyboard_assets,
)
from arclight.application.episode_copy import copy_episode
from arclight.core.errors import DomainError
from arclight.persistence.models import Episode, Script, Shot, Storyboard

router = APIRouter(prefix="/api/v1", tags=["content"])
router.include_router(history_router)


class ScriptVersionInput(Input):
    expected_version_no: int = Field(ge=0)
    base_version_id: UUID | None = None
    content: ScriptContent


class StoryboardVersionInput(Input):
    expected_version_no: int = Field(ge=0)
    base_version_id: UUID | None = None
    script_version_id: UUID
    content: StoryboardContent


class ContentReview(Input):
    decision: str = Field(pattern="^(APPROVE|REJECT)$")
    comment: str = Field(default="", max_length=2000)


def lock_edit(db, episode_id, actor):
    episode = require_episode(db, episode_id, actor, write=True)
    db.execute(select(Episode.id).where(Episode.id == episode_id).with_for_update())
    require_idle(db, [episode_id])
    return episode


def next_version(db, model, episode_id, expected):
    latest = (
        db.scalar(select(func.max(model.version_no)).where(model.episode_id == episode_id)) or 0
    )
    if latest != expected:
        raise DomainError("VERSION_CONFLICT", "内容已有新版本，请刷新后再保存", 409)
    return latest + 1


class EpisodeCopyInput(Input):
    episode_no: int = Field(strict=True, gt=0, le=2147483647)
    title: str = Field(min_length=1, max_length=160)
    reuse_assets: bool = Field(default=True, strict=True)


@router.post("/episodes/{episode_id}/copy", status_code=201)
def duplicate_episode(
    episode_id: UUID, payload: EpisodeCopyInput, db: Database, actor: Actor
) -> EpisodeRead:
    source = lock_edit(db, episode_id, actor)
    return EpisodeRead.model_validate(copy_episode(db, source, actor, **payload.model_dump()))


@router.post("/episodes/{episode_id}/scripts", status_code=201)
def save_script(
    episode_id: UUID, payload: ScriptVersionInput, request: Request, db: Database, actor: Actor
) -> ScriptRead:
    episode = lock_edit(db, episode_id, actor)
    if payload.base_version_id:
        require_version(db, Script, episode_id, payload.base_version_id)
    validate_script_assets(db, episode.project_id, payload.content)
    validate_duration(
        payload.content,
        episode.target_duration_ms,
        request.app.state.settings.text_duration_tolerance_percent,
    )
    version = Script(
        episode_id=episode_id,
        run_id=None,
        version_no=next_version(db, Script, episode_id, payload.expected_version_no),
        content_json=payload.content.model_dump(),
        status="READY_FOR_REVIEW",
    )
    db.add(version)
    db.flush()
    register_content(db, version, actor)
    audit(
        db,
        episode.project_id,
        actor,
        "script.version_created",
        version.id,
        {"base_version_id": str(payload.base_version_id)} if payload.base_version_id else {},
    )
    return ScriptRead.model_validate(version)


@router.post("/episodes/{episode_id}/storyboards", status_code=201)
def save_storyboard(
    episode_id: UUID, payload: StoryboardVersionInput, request: Request, db: Database, actor: Actor
) -> StoryboardRead:
    episode = lock_edit(db, episode_id, actor)
    script = db.get(Script, payload.script_version_id)
    if script is None or script.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "剧本版本不存在", 404)
    if script.status != "APPROVED":
        raise DomainError("SCRIPT_NOT_APPROVED", "分镜必须引用已批准的剧本版本", 409)
    source = ScriptContent.model_validate(script.content_json)
    validate_script_assets(db, episode.project_id, source)
    validate_duration(
        source,
        episode.target_duration_ms,
        request.app.state.settings.text_duration_tolerance_percent,
    )
    try:
        payload.content.validate_against(source)
    except ValueError:
        raise DomainError("INVALID_REFERENCES", "分镜引用了无效的场景或角色", 422) from None
    if payload.base_version_id:
        require_version(db, Storyboard, episode_id, payload.base_version_id)
    validate_storyboard_assets(db, episode.project_id, payload.content)
    validate_duration(
        payload.content,
        episode.target_duration_ms,
        request.app.state.settings.text_duration_tolerance_percent,
    )
    version = Storyboard(
        episode_id=episode_id,
        run_id=None,
        script_version_id=script.id,
        version_no=next_version(db, Storyboard, episode_id, payload.expected_version_no),
        content_json=payload.content.model_dump(),
        status="READY_FOR_REVIEW",
    )
    db.add(version)
    db.flush()
    for shot in payload.content.shots:
        db.add(
            Shot(
                storyboard_id=version.id,
                shot_no=shot.shot_no,
                duration_ms=shot.duration_ms,
                description=shot.visual_description,
                content_json=shot.model_dump(),
                status="READY_FOR_REVIEW",
            )
        )
    register_content(db, version, actor)
    audit(
        db,
        episode.project_id,
        actor,
        "storyboard.version_created",
        version.id,
        {
            "script_version_id": str(script.id),
            **(
                {"base_version_id": str(payload.base_version_id)} if payload.base_version_id else {}
            ),
        },
    )
    return StoryboardRead.model_validate(version)


@router.post("/episodes/{episode_id}/{resource_type}/{version_id}/review")
def review_content(
    episode_id: UUID,
    resource_type: str,
    version_id: UUID,
    payload: ContentReview,
    request: Request,
    db: Database,
    actor: Actor,
) -> dict:
    episode = require_episode(db, episode_id, actor, write=True, owner=True)
    if resource_type not in ("scripts", "storyboards"):
        raise DomainError("NOT_FOUND", "内容类型不存在", 404)
    model = Script if resource_type == "scripts" else Storyboard
    version = cast(
        Script | Storyboard | None,
        db.scalar(select(model).where(model.id == version_id).with_for_update()),
    )
    if version is None or version.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "内容版本不存在", 404)
    if version.run_id is not None:
        raise DomainError("WORKFLOW_REVIEW_REQUIRED", "工作流产物必须通过任务审核入口处理", 409)
    if version.status != "READY_FOR_REVIEW":
        raise DomainError("ALREADY_REVIEWED", "该版本已审核；修改请创建新版本", 409)
    if payload.decision == "APPROVE":
        if isinstance(version, Script):
            validate_script_assets(
                db, episode.project_id, ScriptContent.model_validate(version.content_json)
            )
        else:
            source = db.get(Script, version.script_version_id)
            if source is None or source.episode_id != episode_id or source.status != "APPROVED":
                raise DomainError("SCRIPT_NOT_APPROVED", "请先批准本剧集的来源剧本版本", 409)
            validate_storyboard_assets(
                db, episode.project_id, StoryboardContent.model_validate(version.content_json)
            )
        parsed = (
            ScriptContent if isinstance(version, Script) else StoryboardContent
        ).model_validate(version.content_json)
        validate_duration(
            parsed,
            episode.target_duration_ms,
            request.app.state.settings.text_duration_tolerance_percent,
        )
    version.status = "APPROVED" if payload.decision == "APPROVE" else "REJECTED"
    if isinstance(version, Storyboard):
        for shot in db.scalars(select(Shot).where(Shot.storyboard_id == version.id)):
            shot.status = version.status
    review_registry(db, version, actor)
    audit(
        db, episode.project_id, actor, f"{resource_type}.reviewed", version.id, payload.model_dump()
    )
    return {"version_id": str(version_id), "status": version.status}
