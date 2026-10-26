from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Request
from pydantic import Field, StringConstraints, field_validator, model_validator
from sqlalchemy import select

from arclight.agents.story_analysis import StoryAnalysisContent
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import AcceptedRun, Input, Resource
from arclight.application.commands import require_episode
from arclight.application.story_analysis import start_analysis
from arclight.core.errors import DomainError
from arclight.persistence.models import StoryAnalysis, WorkflowRun

router = APIRouter(prefix="/api/v1/episodes/{episode_id}", tags=["story-analysis"])


class AnalysisCreate(Input):
    story: Annotated[str, StringConstraints(strip_whitespace=False)] | None = Field(
        default=None, min_length=1, max_length=20000
    )
    source_id: UUID | None = None

    @model_validator(mode="after")
    def one_source(self) -> "AnalysisCreate":
        if (self.story is None) == (self.source_id is None):
            raise ValueError("请提供故事文本或一个故事来源，二选一")
        return self


class AnalysisRead(Resource):
    episode_id: UUID
    run_id: UUID
    version_no: int
    source_id: UUID | None
    source_name: str
    source_sha256: str | None
    story_sha256: str
    created_by: str

    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class AnalysisDetail(AnalysisRead):
    content: StoryAnalysisContent
    source_text: str


@router.post("/story-analysis-runs", status_code=202)
def create_analysis(
    episode_id: UUID,
    payload: AnalysisCreate,
    db: Database,
    actor: Actor,
    request: Request,
    idempotency_key: Annotated[str, Header(min_length=1, max_length=128)],
) -> AcceptedRun:
    run = start_analysis(
        db,
        episode_id,
        actor,
        payload.story,
        payload.source_id,
        idempotency_key,
        request.state.request_id,
        request.app.state.settings,
    )
    return AcceptedRun(
        run_id=run.id,
        status=run.status,
        status_url=f"/api/v1/runs/{run.id}",
        events_url=f"/api/v1/runs/{run.id}/events",
    )


@router.get("/story-analyses")
def list_analyses(
    episode_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[AnalysisRead]:
    require_episode(db, episode_id, actor)
    return [
        AnalysisRead.model_validate(row)
        for row in db.scalars(
            select(StoryAnalysis)
            .where(StoryAnalysis.episode_id == episode_id)
            .order_by(StoryAnalysis.version_no.desc())
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/story-analyses/{analysis_id}")
def analysis_detail(
    episode_id: UUID, analysis_id: UUID, db: Database, actor: Actor
) -> AnalysisDetail:
    require_episode(db, episode_id, actor)
    row = db.get(StoryAnalysis, analysis_id)
    if row is None or row.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "提炼结果不存在", 404)
    run = db.get(WorkflowRun, row.run_id)
    if run is None:
        raise DomainError("NOT_FOUND", "提炼任务不存在", 404)
    return AnalysisDetail(
        **AnalysisRead.model_validate(row).model_dump(),
        content=StoryAnalysisContent.model_validate(row.content_json),
        source_text=run.input_json["story"],
    )
