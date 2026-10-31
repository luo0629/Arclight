"""Permission-scoped component registry and immutable production provenance queries."""

from datetime import UTC, datetime
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query
from pydantic import field_validator
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Resource
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.production_versions import register
from arclight.core.errors import DomainError
from arclight.domain.versions import Family, Kind, VersionRegistration
from arclight.persistence.models import ProductionVersion, VersionBinding

router = APIRouter(prefix="/api/v1/projects/{project_id}/production-versions", tags=["versions"])


class VersionResource(Resource):
    @field_validator("created_at", "updated_at")
    @classmethod
    def utc_timestamps(cls, value: datetime) -> datetime:
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class VersionRead(VersionResource):
    project_id: UUID
    family: Family
    kind: Kind
    name: str
    version: str
    digest: str
    definition_json: dict


class BindingRead(VersionResource):
    project_id: UUID
    resource_type: str
    resource_id: UUID
    snapshot_json: dict


@router.post("", status_code=201, summary="追加版本定义；登记不代表Agent或模型已执行")
def register_version(
    project_id: UUID, payload: VersionRegistration, db: Database, actor: Actor
) -> VersionRead:
    require_project(db, project_id, actor)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    entry = register(db, project_id, payload)
    audit(
        db,
        project_id,
        actor,
        "production_version.registered",
        entry.id,
        {"family": entry.family, "kind": entry.kind, "digest": entry.digest},
    )
    return VersionRead.model_validate(entry)


@router.get("")
def list_versions(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
    family: Family | None = None,
    kind: Kind | None = None,
) -> list[VersionRead]:
    require_project(db, project_id, actor)
    statement = select(ProductionVersion).where(ProductionVersion.project_id == project_id)
    if family is not None:
        statement = statement.where(ProductionVersion.family == family)
    if kind is not None:
        statement = statement.where(ProductionVersion.kind == kind)
    return [
        VersionRead.model_validate(row)
        for row in db.scalars(
            statement.order_by(ProductionVersion.created_at, ProductionVersion.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/bindings", summary="查询精确运行及产物绑定；旧记录无绑定时返回空列表")
def list_bindings(
    project_id: UUID,
    db: Database,
    actor: Actor,
    limit: Limit = 20,
    offset: Offset = 0,
    resource_id: UUID | None = None,
    resource_type: Annotated[str | None, Query(max_length=32)] = None,
) -> list[BindingRead]:
    require_project(db, project_id, actor)
    statement = select(VersionBinding).where(VersionBinding.project_id == project_id)
    if resource_id is not None:
        statement = statement.where(VersionBinding.resource_id == resource_id)
    if resource_type is not None:
        statement = statement.where(VersionBinding.resource_type == resource_type)
    return [
        BindingRead.model_validate(row)
        for row in db.scalars(
            statement.order_by(VersionBinding.created_at, VersionBinding.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/{version_id}")
def read_version(project_id: UUID, version_id: UUID, db: Database, actor: Actor) -> VersionRead:
    require_project(db, project_id, actor)
    row = db.get(ProductionVersion, version_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "版本登记不存在", 404)
    return VersionRead.model_validate(row)
