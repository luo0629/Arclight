"""Create media jobs and freeze exact source identities in the existing DAG manifest."""

import hashlib
import json
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import exact_version, lock_dependency_graph
from arclight.application.generation_events import enqueue_dispatch
from arclight.application.generation_targets import require_available_target
from arclight.core.errors import DomainError
from arclight.domain.dependencies import validate_dependency_type
from arclight.domain.generation_requests import GenerationCreate
from arclight.persistence.models import (
    DependencyInvalidation,
    DependencyManifest,
    Episode,
    GenerationJob,
    VisualAsset,
)


def request_fingerprint(payload: GenerationCreate) -> str:
    return hashlib.sha256(
        json.dumps(
            payload.model_dump(mode="json"),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode()
    ).hexdigest()


def validate_target(db: Session, project_id: UUID, payload: GenerationCreate) -> VisualAsset | None:
    if payload.episode_id is not None:
        episode = db.get(Episode, payload.episode_id, populate_existing=True)
        if episode is None or episode.project_id != project_id:
            raise DomainError("NOT_FOUND", "目标剧集不存在", 404)
        if episode.status == "ARCHIVED":
            raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    if payload.target_asset_id is None:
        return None
    asset = db.get(VisualAsset, payload.target_asset_id, populate_existing=True)
    if asset is None or asset.project_id != project_id:
        raise DomainError("NOT_FOUND", "目标资产不存在", 404)
    if asset.asset_type != payload.target_type:
        raise DomainError("INVALID_GENERATION_TARGET", "目标资产类型不一致", 422)
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "目标资产只读", 409)
    expected = payload.expected_asset
    assert expected is not None
    if (asset.revision, asset.lock_version, asset.current_version_id) != (
        expected.revision,
        expected.lock_version,
        expected.current_version_id,
    ):
        raise DomainError("VERSION_CONFLICT", "目标资产已变化，请刷新后创建任务", 409)
    return asset


def create_job(
    db: Session,
    project_id: UUID,
    actor: str,
    key: str,
    payload: GenerationCreate,
) -> GenerationJob:
    require_project(db, project_id, actor)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True)
    fingerprint = request_fingerprint(payload)
    existing = db.scalar(
        select(GenerationJob).where(
            GenerationJob.project_id == project_id,
            GenerationJob.idempotency_key == key,
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一幂等键不能用于不同媒体请求", 409)
        return existing
    asset = validate_target(db, project_id, payload)
    require_available_target(db, payload.target_asset_id)
    inputs = []
    for item in payload.inputs:
        version, source = exact_version(db, project_id, item.source_asset_version_id)
        validate_dependency_type(source.asset_type, payload.target_type, item.dependency_type)
        if (
            version.stale
            or version.status not in {"APPROVED", "LOCKED"}
            or source.status == "ARCHIVED"
        ):
            raise DomainError("ASSET_NOT_USABLE", "输入版本必须已批准且有效", 409)
        inputs.append(
            {
                **item.model_dump(mode="json"),
                "asset_id": str(source.id),
                "asset_type": source.asset_type,
                "asset_name": source.name,
                "version_no": version.version_no,
                "checksum": version.checksum,
            }
        )
    manifest = DependencyManifest(
        project_id=project_id,
        target_asset_id=asset.id if asset else None,
        creation_key=uuid4(),
        request_fingerprint=fingerprint,
        created_by=actor,
        snapshot_json={
            "schema_version": 1,
            "invalidation_seq": db.scalar(
                select(func.max(DependencyInvalidation.event_no)).where(
                    DependencyInvalidation.project_id == project_id
                )
            )
            or 0,
            "target": {
                "asset_id": str(asset.id) if asset else None,
                "asset_type": payload.target_type,
                "episode_id": str(payload.episode_id) if payload.episode_id else None,
                "expected_asset": payload.expected_asset.model_dump(mode="json")
                if payload.expected_asset
                else None,
            },
            "inputs": inputs,
            "context": {
                "generation": payload.spec.model_dump(mode="json"),
                "origin": "generation_request",
            },
        },
    )
    db.add(manifest)
    db.flush()
    job = GenerationJob(
        project_id=project_id,
        episode_id=payload.episode_id,
        target_type=payload.target_type,
        target_asset_id=payload.target_asset_id,
        input_manifest_id=manifest.id,
        task_type=payload.task_type,
        provider=payload.spec.provider,
        model=payload.spec.model,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        created_by=actor,
        expected_asset_revision=asset.revision if asset else None,
        expected_lock_version=asset.lock_version if asset else None,
        expected_current_version_id=asset.current_version_id if asset else None,
    )
    db.add(job)
    db.flush()
    enqueue_dispatch(db, job)
    audit(
        db,
        project_id,
        actor,
        "generation.created",
        job.id,
        {
            "input_manifest_id": str(manifest.id),
            "target_type": job.target_type,
            "provider": job.provider,
            "model": job.model,
        },
    )
    return job
