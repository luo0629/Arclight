"""Exact source adapters; arbitrary generation context never creates a shot binding."""

from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.production_versions import find_binding, input_summary
from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.persistence.models import (
    DependencyManifest,
    Episode,
    GenerationJob,
    Script,
    Storyboard,
    WorkflowRun,
)


@dataclass(frozen=True)
class StageSource:
    project_id: UUID
    episode_id: UUID
    revision: int
    status: str
    input_sha256: str
    input_version_ids: tuple[UUID, ...]


def current_text(db: Session, episode_id: UUID) -> tuple[UUID | None, UUID | None]:
    script = db.scalar(
        select(Script.id)
        .where(Script.episode_id == episode_id)
        .order_by(Script.version_no.desc())
        .limit(1)
    )
    board = db.scalar(
        select(Storyboard.id)
        .where(Storyboard.episode_id == episode_id)
        .order_by(Storyboard.version_no.desc())
        .limit(1)
    )
    return script, board


def read_source(db: Session, kind: str, source_id: UUID, *, lock: bool = False) -> StageSource:
    if kind == "TEXT":
        run = db.get(
            WorkflowRun,
            source_id,
            populate_existing=True,
            with_for_update={"key_share": True} if lock else None,
        )
        episode = db.get(Episode, run.episode_id) if run else None
        if run is None or episode is None:
            raise DomainError("STAGE_SOURCE_NOT_FOUND", "阶段运行不存在", 404)
        binding = find_binding(db, episode.project_id, "WORKFLOW", run.id)
        # The digest binds all frozen reference assets; explicit text source versions
        # are additionally checked against the registry. Do not expand unbounded context.
        identities: set[UUID] = set()
        if run.input_json.get("script_version_id"):
            identities.add(UUID(run.input_json["script_version_id"]))
        rewrite = run.input_json.get("rewrite_input", {}).get("request", {})
        if rewrite.get("base_version_id"):
            identities.add(UUID(rewrite["base_version_id"]))
        return StageSource(
            episode.project_id,
            episode.id,
            run.revision,
            run.status,
            digest(
                {
                    "input": input_summary(run.input_json),
                    "versions": binding.snapshot_json if binding else None,
                }
            ),
            tuple(sorted(identities)),
        )
    if kind != "MEDIA":
        raise DomainError("STAGE_SOURCE_INVALID", "阶段来源类型不支持", 422)
    job = db.get(
        GenerationJob,
        source_id,
        populate_existing=True,
        with_for_update={"key_share": True} if lock else None,
    )
    manifest = db.get(DependencyManifest, job.input_manifest_id) if job else None
    if job is None or job.episode_id is None or manifest is None:
        raise DomainError("STAGE_SOURCE_NOT_FOUND", "阶段媒体任务或输入不存在", 404)
    if manifest.project_id != job.project_id:
        raise DomainError("STAGE_SOURCE_INVALID", "阶段媒体输入范围错误", 409)
    media_inputs = tuple(
        sorted(UUID(row["source_asset_version_id"]) for row in manifest.snapshot_json["inputs"])
    )
    return StageSource(
        job.project_id,
        job.episode_id,
        job.revision,
        job.status,
        digest(manifest.snapshot_json),
        tuple(dict.fromkeys(media_inputs)),
    )
