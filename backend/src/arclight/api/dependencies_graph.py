"""Authorized exact dependency and immutable input manifest API."""

from typing import Literal
from uuid import UUID

from fastapi import APIRouter
from pydantic import Field

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Input, Resource
from arclight.application.commands import require_project
from arclight.application.dependencies import (
    capture_manifest,
    exact_version,
    query_dependencies,
    require_manifest,
)
from arclight.application.invalidation import impact_preview, invalidate
from arclight.domain.dependencies import ManifestCreate
from arclight.domain.invalidation import (
    BatchInvalidationCreate,
    ChangeKind,
    DependencyChange,
    InvalidationCreate,
)

router = APIRouter(prefix="/api/v1/projects/{project_id}", tags=["dependencies"])


class ManifestRead(Resource):
    project_id: UUID
    target_asset_id: UUID | None
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
    event_no: int
    snapshot: dict = Field(validation_alias="snapshot_json")


class ImpactQuery(Input):
    changes: list[DependencyChange] = Field(min_length=1, max_length=100)


@router.post("/dependencies/impact")
def preview_dependencies(
    project_id: UUID, payload: ImpactQuery, db: Database, actor: Actor
) -> dict:
    require_project(db, project_id, actor)
    return impact_preview(
        db,
        project_id,
        [x.model_dump(mode="json", exclude={"dependency_type"}) for x in payload.changes],
    )


@router.get("/dependency-impact")
def dependency_impact(
    project_id: UUID,
    asset_version_id: UUID,
    db: Database,
    actor: Actor,
    change_kind: ChangeKind = "CONTENT",
) -> dict:
    require_project(db, project_id, actor)
    version, _ = exact_version(db, project_id, asset_version_id)
    return impact_preview(
        db,
        project_id,
        [
            {
                "source_asset_version_id": str(version.id),
                "expected_revision": version.revision,
                "change_kind": change_kind,
                "selectors": [],
            }
        ],
    )


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
    row, _ = capture_manifest(db, project_id, actor, payload)
    return ManifestRead.model_validate(row)


@router.post("/dependencies/invalidate-batch", status_code=201)
def invalidate_dependency_batch(
    project_id: UUID, payload: BatchInvalidationCreate, db: Database, actor: Actor
) -> InvalidationRead:
    require_project(db, project_id, actor, write=True, owner=True)
    row, _ = invalidate(
        db,
        project_id,
        actor,
        payload.creation_key,
        [item.model_dump(mode="json", exclude={"dependency_type"}) for item in payload.changes],
        payload.reason,
    )
    return InvalidationRead.model_validate(row)


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
