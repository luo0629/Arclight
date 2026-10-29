"""Authorized exact dependency and immutable input manifest API."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from arclight.api.collaboration import audit
from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.dependencies import (
    capture_manifest,
    query_dependencies,
    require_manifest,
)
from arclight.application.invalidation import invalidate
from arclight.domain.dependencies import ManifestCreate
from arclight.domain.invalidation import InvalidationCreate

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["dependencies"])


class ManifestRead(Resource):
    project_id: UUID
    target_asset_id: UUID
    created_by: str
    snapshot: dict = Field(validation_alias="snapshot_json")


class DependencyRead(Resource):
    source_asset_version_id: UUID
    target_asset_version_id: UUID
    dependency_type: str
    dependency_manifest_id: UUID | None


class BatchQuery(Input):
    version_ids: list[UUID] = Field(min_length=1, max_length=100)
    direction: Literal["incoming", "outgoing"] = "outgoing"
    limit: int = Field(default=100, strict=True, ge=1, le=1000)
    offset: int = Field(default=0, strict=True, ge=0)


class InvalidationRead(Resource):
    project_id: UUID
    created_by: str
    snapshot: dict = Field(validation_alias="snapshot_json")


@router.post("/dependencies/invalidate", status_code=201)
def invalidate_dependencies(
    project_id: UUID, payload: InvalidationCreate, db: Database, actor: Actor
) -> InvalidationRead:
    require_project(db, project_id, actor, write=True, owner=True)
    row, _ = invalidate(
        db,
        project_id,
        actor,
        payload.creation_key,
        [payload.model_dump(mode="json", exclude={"creation_key", "reason", "dependency_type"})],
        payload.reason,
    )
    return InvalidationRead.model_validate(row)


@router.post("/dependency-manifests", status_code=201)
def create_manifest(
    project_id: UUID, payload: ManifestCreate, db: Database, actor: Actor
) -> ManifestRead:
    require_project(db, project_id, actor, write=True)
    row, created = capture_manifest(db, project_id, actor, payload)
    if created:
        audit(db, project_id, actor, "dependency.manifest_captured", row.id)
    return ManifestRead.model_validate(row)


@router.get("/dependency-manifests/{manifest_id}")
def get_manifest(project_id: UUID, manifest_id: UUID, db: Database, actor: Actor) -> ManifestRead:
    require_project(db, project_id, actor)
    return ManifestRead.model_validate(require_manifest(db, project_id, manifest_id))


@router.get("/dependencies")
def dependencies(
    project_id: UUID,
    db: Database,
    actor: Actor,
    source_version_id: UUID | None = None,
    target_version_id: UUID | None = None,
    limit: Limit = 20,
    offset: Offset = 0,
) -> list[DependencyRead]:
    require_project(db, project_id, actor)
    return [
        DependencyRead.model_validate(row)
        for row in query_dependencies(
            db,
            project_id,
            source=source_version_id,
            target=target_version_id,
            limit=limit,
            offset=offset,
        )
    ]


@router.post("/dependencies/query")
def batch_dependencies(
    project_id: UUID, payload: BatchQuery, db: Database, actor: Actor
) -> list[DependencyRead]:
    require_project(db, project_id, actor)
    return [
        DependencyRead.model_validate(row)
        for row in query_dependencies(db, project_id, **payload.model_dump())
    ]
