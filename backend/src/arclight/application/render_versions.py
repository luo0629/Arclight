"""Append render versions through the existing asset, file and dependency registries."""

from uuid import uuid5

from pydantic import Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.dependencies import attach_output, capture_manifest, lock_dependency_graph
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.dependencies import DependencyInput, DependencyType, ManifestCreate
from arclight.domain.render_inspections import RenderInspectionResult
from arclight.domain.render_manifests import RenderManifestSnapshot
from arclight.persistence.models import (
    AssetVersion,
    Episode,
    RenderArtifact,
    RenderAttempt,
    RenderExecution,
    RenderInspection,
    RenderJob,
    StoredFile,
    VisualAsset,
)


class RenderInputs(ManifestCreate):
    # Internal only: the public API keeps its 64-input bound. Five tracks of
    # <=256 clips can each carry source, script and audio version references.
    inputs: list[DependencyInput] = Field(min_length=1, max_length=4096)


def register_render_version(
    db: Session,
    settings: Settings,
    run: RenderExecution,
    job: RenderJob,
    attempt: RenderAttempt,
    snapshot: RenderManifestSnapshot,
    result: RenderInspectionResult,
) -> RenderArtifact:
    """Called under the final owned publication gate; never decode under its locks."""
    lock_dependency_graph(db, job.project_id)
    previous = db.scalar(select(RenderArtifact).where(RenderArtifact.job_id == job.id))
    if previous is not None:
        if previous.checksum != result.output_sha256 or previous.object_key != attempt.output_key:
            raise DomainError("RENDER_RESULT_CONFLICT", "本任务的成片已登记且不可替换", 409)
        return previous
    inspection = db.scalar(
        select(RenderInspection).where(RenderInspection.attempt_id == attempt.id)
    )
    if (
        inspection is None
        or not inspection.passed
        or inspection.output_sha256 != result.output_sha256
        or inspection.byte_size != result.byte_size
        or inspection.input_sha256 != attempt.input_sha256
        or result.measurement is None
        or result.measurement.video is None
    ):
        raise DomainError("RENDER_INSPECTION_REQUIRED", "缺少精确产物的通过检查", 409)
    episode = db.get(Episode, snapshot.timeline.episode_id)
    assert episode is not None and episode.project_id == job.project_id
    asset_id = uuid5(episode.id, "dependency:RENDER")
    asset = db.get(VisualAsset, asset_id, populate_existing=True)
    if asset is None:
        asset = VisualAsset(
            id=asset_id,
            project_id=job.project_id,
            asset_type="RENDER",
            name=f"第{episode.episode_no}集 · 成片",
            created_by=run.created_by,
            metadata_json={"episode_id": str(episode.id), "render_registry": True},
        )
        db.add(asset)
        db.flush()
    if (
        asset.project_id != job.project_id
        or asset.asset_type != "RENDER"
        or asset.metadata_json.get("episode_id") != str(episode.id)
    ):
        raise DomainError("RENDER_RESULT_CONFLICT", "成片资产作用域不一致", 409)
    render_kinds: tuple[DependencyType, ...] = ("RENDER_DEPENDENCY", "SEQUENCE_DEPENDENCY")
    inputs = [
        DependencyInput(source_asset_version_id=snapshot.timeline_id, dependency_type=kind)
        for kind in render_kinds
    ]
    for source in snapshot.sources:
        inputs.append(
            DependencyInput(
                source_asset_version_id=source.asset_version_id,
                dependency_type=(
                    "CONTENT_DEPENDENCY"
                    if source.asset_type == "SCRIPT"
                    else "SEQUENCE_DEPENDENCY"
                    if source.asset_type == "STORYBOARD"
                    else "RENDER_DEPENDENCY"
                ),
            )
        )
    dependencies, _ = capture_manifest(
        db,
        job.project_id,
        run.created_by,
        RenderInputs(
            creation_key=uuid5(job.id, "render-inputs"),
            target_asset_id=asset.id,
            inputs=inputs,
            context={
                "episode_id": str(episode.id),
                "render_manifest_id": str(job.manifest_id),
                "render_manifest_sha256": job.manifest_sha256,
                "render_job_id": str(job.id),
                "timeline_id": str(snapshot.timeline_id),
                "font_file_id": str(snapshot.font.file_id) if snapshot.font else None,
                "software": snapshot.software.model_dump(mode="json"),
            },
        ),
    )
    number = (
        db.scalar(
            select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
        )
        or 0
    ) + 1
    duration_ms = max(1, (result.measurement.video.duration_us + 500) // 1000)
    version = AssetVersion(
        id=uuid5(job.id, "render-version"),
        asset_id=asset.id,
        version_no=number,
        creation_key=job.id,
        object_key=attempt.output_key,
        checksum=result.output_sha256,
        created_by=run.created_by,
        metadata_json={
            "episode_id": str(episode.id),
            "render_registry": True,
            "render_job_id": str(job.id),
            "render_manifest_id": str(job.manifest_id),
            "render_manifest_sha256": job.manifest_sha256,
            "timeline_id": str(snapshot.timeline_id),
            "timeline_sha256": snapshot.timeline_sha256,
            "dependency_manifest_id": str(dependencies.id),
            "inspection_id": str(inspection.id),
            "profile": snapshot.output.name,
            "storage_backend": settings.object_storage_backend.upper(),
            "storage_bucket": settings.s3_bucket
            if settings.object_storage_backend == "s3"
            else None,
            "media": {
                "media_type": "video/mp4",
                "byte_size": result.byte_size,
                "duration_ms": duration_ms,
            },
        },
    )
    db.add(version)
    db.flush()
    attach_output(db, job.project_id, dependencies.id, version)
    if version.stale:
        raise DomainError("RENDER_INPUT_CHANGED", "登记时精确输入已失效", 409)
    file = StoredFile(
        project_id=job.project_id,
        asset_version_id=version.id,
        storage_backend=settings.object_storage_backend.upper(),
        bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
        object_key=attempt.output_key,
        checksum=result.output_sha256,
        byte_size=result.byte_size,
        media_type="video/mp4",
        original_name=f"render-v{number}.mp4",
        source_note=f"Render Manifest {job.manifest_id} sha256:{job.manifest_sha256}",
        rights_note="已复验清单内精确素材的授权声明",
        rights_confirmed=True,
        created_by=run.created_by,
    )
    db.add(file)
    db.flush()
    artifact = RenderArtifact(
        manifest_id=job.manifest_id,
        job_id=job.id,
        asset_version_id=version.id,
        file_id=file.id,
        object_key=file.object_key,
        checksum=file.checksum,
        byte_size=file.byte_size,
        duration_ms=duration_ms,
    )
    db.add(artifact)
    db.flush()
    audit(
        db,
        job.project_id,
        run.created_by,
        "render.version_registered",
        artifact.id,
        {
            "job_id": str(job.id),
            "asset_version_id": str(version.id),
            "version_no": number,
            "file_id": str(file.id),
            "dependency_manifest_id": str(dependencies.id),
            "inspection_id": str(inspection.id),
        },
    )
    return artifact
