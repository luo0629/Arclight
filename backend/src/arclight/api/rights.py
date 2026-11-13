"""Shared declarations and a project-scoped view of immutable usage-right evidence."""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID

from fastapi import APIRouter, Response
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import case, func, literal, select, union_all

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input
from arclight.application.commands import require_project
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    StoredFile,
    StorySource,
    StorySourceRevision,
    UploadCompletion,
    VisualAsset,
)

Category = Literal["STORY", "REFERENCE_IMAGE", "AUDIO", "MUSIC", "FONT"]
RightsStatus = Literal["CONFIRMED", "UNDECLARED"]
router = APIRouter(prefix="/api/v1/projects/{project_id}/rights", tags=["rights"])


class UsageDeclaration(Input):
    source_note: str = Field(min_length=1, max_length=1000)
    rights_note: str = Field(min_length=1, max_length=1000)
    rights_confirmed: Literal[True]

    @field_validator("rights_confirmed", mode="before")
    @classmethod
    def explicit_confirmation(cls, value: object) -> object:
        if value is not True:
            raise ValueError("必须明确确认素材使用权")
        return value


class RightsRead(BaseModel):
    id: UUID
    record_type: Literal["STORY_SOURCE", "FILE", "ASSET_VERSION"]
    category: Category
    original_name: str
    checksum: str
    source_note: str
    rights_note: str
    rights_status: RightsStatus
    recorded_by: str
    recorded_at: datetime
    episode_id: UUID | None
    asset_version_id: UUID | None

    @field_validator("recorded_at")
    @classmethod
    def utc_timestamp(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def rights_records(project_id: UUID):
    # Select metadata only: story text/raw_bytes and private storage keys never load.
    stories = (
        select(
            StorySource.id,
            literal("STORY_SOURCE").label("record_type"),
            literal("STORY").label("category"),
            StorySource.original_name,
            StorySource.sha256.label("checksum"),
            StorySource.source_note,
            StorySource.rights_note,
            case(
                (StorySourceRevision.rights_status == "CONFIRMED", "CONFIRMED"),
                else_="UNDECLARED",
            ).label("rights_status"),
            StorySource.created_by.label("recorded_by"),
            StorySource.created_at.label("recorded_at"),
            StorySource.episode_id,
            literal(None, type_=StoredFile.asset_version_id.type).label("asset_version_id"),
        )
        .join(Episode, Episode.id == StorySource.episode_id)
        .outerjoin(StorySourceRevision, StorySourceRevision.source_id == StorySource.id)
        .where(Episode.project_id == project_id)
    )
    files = (
        select(
            StoredFile.id,
            literal("FILE").label("record_type"),
            case(
                (UploadCompletion.purpose.is_not(None), UploadCompletion.purpose),
                (VisualAsset.asset_type == "MUSIC", "MUSIC"),
                (StoredFile.media_type.like("image/%"), "REFERENCE_IMAGE"),
                (StoredFile.media_type.like("text/%"), "STORY"),
                (StoredFile.media_type.like("font/%"), "FONT"),
                else_="AUDIO",
            ).label("category"),
            StoredFile.original_name,
            StoredFile.checksum,
            StoredFile.source_note,
            StoredFile.rights_note,
            case((StoredFile.rights_confirmed.is_(True), "CONFIRMED"), else_="UNDECLARED").label(
                "rights_status"
            ),
            StoredFile.created_by.label("recorded_by"),
            StoredFile.created_at.label("recorded_at"),
            literal(None, type_=StorySource.episode_id.type).label("episode_id"),
            StoredFile.asset_version_id,
        )
        .outerjoin(UploadCompletion, UploadCompletion.file_id == StoredFile.id)
        .outerjoin(AssetVersion, AssetVersion.id == StoredFile.asset_version_id)
        .outerjoin(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(StoredFile.project_id == project_id)
    )
    # Pre-file-ledger images still carry exact declarations in immutable version metadata.
    media = AssetVersion.metadata_json["media"]
    legacy_images = (
        select(
            AssetVersion.id,
            literal("ASSET_VERSION").label("record_type"),
            literal("REFERENCE_IMAGE").label("category"),
            func.coalesce(media["original_name"].as_string(), "").label("original_name"),
            AssetVersion.checksum,
            func.coalesce(media["source_note"].as_string(), "").label("source_note"),
            func.coalesce(media["rights_note"].as_string(), "").label("rights_note"),
            case(
                (media["rights_confirmed"].as_boolean().is_(True), "CONFIRMED"), else_="UNDECLARED"
            ).label("rights_status"),
            AssetVersion.created_by.label("recorded_by"),
            AssetVersion.created_at.label("recorded_at"),
            literal(None, type_=StorySource.episode_id.type).label("episode_id"),
            AssetVersion.id.label("asset_version_id"),
        )
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(
            VisualAsset.project_id == project_id,
            media["media_type"].as_string().in_(["image/png", "image/jpeg"]),
            ~select(StoredFile.id).where(StoredFile.asset_version_id == AssetVersion.id).exists(),
        )
    )
    return union_all(stories, files, legacy_images).subquery()


@router.get("")
def list_rights(
    project_id: UUID,
    db: Database,
    actor: Actor,
    response: Response,
    category: Category | None = None,
    rights_status: RightsStatus | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[RightsRead]:
    require_project(db, project_id, actor)
    records = rights_records(project_id)
    statement = select(records)
    if category is not None:
        statement = statement.where(records.c.category == category)
    if rights_status is not None:
        statement = statement.where(records.c.rights_status == rights_status)
    response.headers["Cache-Control"] = "no-store"
    return [
        RightsRead.model_validate(row)
        for row in db.execute(
            statement.order_by(records.c.recorded_at, records.c.record_type, records.c.id)
            .limit(limit)
            .offset(offset)
        ).mappings()
    ]
