"""Current render preflight, shared by export reviews and the trusted renderer.

Database facts are separate from object/decoder I/O so FLOW-03 remains transactional.
An admission response is an observation, not a reusable authorization token.
"""

import hashlib
import json
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.dependencies import exact_version, require_manifest
from arclight.application.render_audio import validate_line_audio
from arclight.application.render_inputs import file_storage, font_facts, source_facts
from arclight.application.render_manifests import checked_snapshot, manifest_row, output_profile
from arclight.application.timeline_store import checked_timeline
from arclight.application.timelines import validate_snapshot_facts
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.file_policy import POLICIES
from arclight.domain.render_manifests import (
    RenderAdmissionRead,
    RenderManifestSnapshot,
    RenderVisual,
)
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.versions import digest
from arclight.media.execution import MediaObserver
from arclight.media.validation import probe
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    DependencyOutput,
    Episode,
    Project,
    Script,
    Shot,
    StoredFile,
    Storyboard,
)
from arclight.persistence.render_manifests import RenderManifest
from arclight.persistence.timelines import Timeline


@dataclass(frozen=True)
class RenderFacts:
    snapshot: RenderManifestSnapshot
    input_sha256: str
    source_version_ids: list[UUID]


def admission_facts(db: Session, row: RenderManifest) -> RenderFacts:
    """Rebuild authoritative database facts; no model, object or parser calls."""
    snapshot = checked_snapshot(row)
    timeline_row = db.get(Timeline, row.timeline_id, populate_existing=True)
    episode = db.get(Episode, snapshot.timeline.episode_id, populate_existing=True)
    project = db.get(Project, row.project_id, populate_existing=True)
    if project is None or project.deleted_at is not None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
    if project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目只允许查看", 409)
    if (
        episode is None
        or episode.project_id != row.project_id
        or (
            timeline_row is None or not timeline_row.sealed or timeline_row.episode_id != episode.id
        )
    ):
        raise DomainError("RENDER_INPUT_INVALID", "渲染清单的时间轴或剧集范围无效", 409)
    if episode.status == "ARCHIVED":
        raise DomainError("EPISODE_ARCHIVED", "归档剧集只允许查看", 409)
    timeline = checked_timeline(db, timeline_row, row.project_id)
    version, asset = exact_version(db, row.project_id, timeline.id)
    if (
        version.stale
        or version.status in {"STALE", "ARCHIVED", "REJECTED"}
        or asset.status in {"STALE", "ARCHIVED", "REJECTED"}
    ):
        raise DomainError("ASSET_NOT_USABLE", "时间轴或其上游输入已失效", 409)
    if (
        timeline.snapshot != snapshot.timeline
        or timeline.content_sha256 != snapshot.timeline_sha256
    ):
        raise DomainError("RENDER_INPUT_CHANGED", "时间轴与冻结清单不一致", 409)
    dependency = require_manifest(db, row.project_id, timeline.dependency_manifest_id)
    if (
        dependency.id != snapshot.dependency_manifest_id
        or digest(dependency.snapshot_json) != snapshot.dependency_manifest_sha256
        or db.scalar(
            select(DependencyOutput.id).where(
                DependencyOutput.dependency_manifest_id == dependency.id,
                DependencyOutput.asset_version_id == timeline.id,
            )
        )
        is None
    ):
        raise DomainError("RENDER_INPUT_CHANGED", "时间轴的精确依赖回执已变化", 409)
    validate_snapshot_facts(db, episode, snapshot.timeline)
    validate_line_audio(db, snapshot.timeline, snapshot.line_audio_decisions, complete=True)
    sources = source_facts(db, row.project_id, snapshot.timeline)
    font = font_facts(
        db,
        row.project_id,
        row.font_file_id,
        required=any(
            track.enabled and track.clips
            for track in snapshot.timeline.tracks
            if track.kind == "SUBTITLE"
        ),
    )
    visuals = tuple(
        RenderVisual(clip_id=clip.id, settings=clip.visual or ClipVisual())
        for track in snapshot.timeline.tracks
        if track.kind == "VIDEO"
        for clip in sorted(track.clips, key=lambda item: item.ordinal)
    )
    if (
        sources != snapshot.sources
        or font != snapshot.font
        or visuals != snapshot.visuals
        or snapshot.output != output_profile(snapshot.timeline.output_profile, snapshot.output.name)
    ):
        raise DomainError("RENDER_INPUT_CHANGED", "素材、字体、视觉或输出规格与清单不一致", 409)
    inputs = [
        {
            "id": str(version.id),
            "sha256": version.checksum,
            "revision": version.revision,
            "status": version.status,
            "asset_status": asset.status,
        }
    ]
    for source in sources:
        version, asset = exact_version(db, row.project_id, source.asset_version_id)
        if (
            version.stale
            or version.status not in {"APPROVED", "LOCKED"}
            or asset.status in {"STALE", "ARCHIVED", "REJECTED"}
        ):
            raise DomainError("ASSET_NOT_USABLE", "导出输入必须已批准且未失效或归档", 409)
        if source.asset_type in {"SCRIPT", "STORYBOARD"}:
            artifact = (
                db.get(Script, version.id)
                if source.asset_type == "SCRIPT"
                else db.get(Storyboard, version.id)
            )
            if (
                artifact is None
                or artifact.episode_id != episode.id
                or artifact.status != "APPROVED"
                or hashlib.sha256(
                    json.dumps(artifact.content_json, sort_keys=True).encode()
                ).hexdigest()
                != version.checksum
            ):
                raise DomainError("RENDER_INPUT_CHANGED", "剧本或分镜缺少精确有效的批准正文", 409)
        inputs.append(
            {
                "id": str(version.id),
                "sha256": version.checksum,
                "revision": version.revision,
                "status": version.status,
                "asset_id": str(asset.id),
                "asset_status": asset.status,
                "asset_revision": asset.revision,
                "selection": str(asset.current_version_id)
                if source.asset_type in {"IMAGE", "SHOT_IMAGE", "MUSIC"}
                else None,
            }
        )
    shots = list(
        db.scalars(
            select(Shot)
            .where(Shot.storyboard_id == timeline.snapshot.storyboard_id)
            .order_by(Shot.shot_no)
        )
    )
    if any(shot.status != "APPROVED" for shot in shots):
        raise DomainError("ASSET_NOT_USABLE", "全部必需镜头须保持批准状态", 409)
    for track in snapshot.timeline.tracks:
        if track.kind in {"VIDEO", "SUBTITLE"} and any(
            clip.source_start_ms for clip in track.clips
        ):
            raise DomainError("RENDER_SOURCE_RANGE_INVALID", "静图或字幕不能使用非零媒体入点", 409)
    return RenderFacts(
        snapshot=snapshot,
        input_sha256=digest(
            {
                "manifest_sha256": row.content_sha256,
                "inputs": inputs,
                "shots": [
                    {"id": str(shot.id), "status": shot.status, "content": shot.content_json}
                    for shot in shots
                ],
            }
        ),
        source_version_ids=sorted(
            {row.timeline_id, *(source.asset_version_id for source in sources)}
        ),
    )


def validate_render_objects(
    db: Session, settings: Settings, facts: RenderFacts, *, observer: MediaObserver | None = None
) -> None:
    """Read exact receipts and fully decode media; never trust declared audio duration."""
    snapshot = facts.snapshot
    durations = {}
    references = [source.file for source in snapshot.sources if source.file is not None]
    if snapshot.font is not None:
        references.append(snapshot.font)
    for reference in references:
        if observer is not None:
            observer(None)
        file = db.get(StoredFile, reference.file_id)
        assert file is not None  # admission_facts resolved the same immutable receipt.
        data = file_storage(settings, file).read(
            file.object_key, POLICIES[file.media_type].max_bytes, file.checksum
        )
        if len(data) != reference.byte_size:
            raise DomainError("OBJECT_UNAVAILABLE", "素材大小与不可变文件回执不符", 409)
        details = (
            probe(data, file.media_type)
            if observer is None
            else probe(data, file.media_type, observer=observer)
        )
        if file.asset_version_id is not None and file.media_type.startswith("audio/"):
            durations[file.asset_version_id] = details["duration_ms"]
            version, asset = exact_version(db, file.project_id, file.asset_version_id)
            if asset.asset_type == "MUSIC":
                from arclight.application.music_assets import music_resource_facts
                from arclight.domain.audio_production import AudioValidationRead

                resource = music_resource_facts(db, file.project_id, asset, version)
                if AudioValidationRead.model_validate(details) != resource.validation:
                    raise DomainError(
                        "MUSIC_OUTPUT_MISMATCH", "音乐音效与冻结的实际文件检查记录不符", 409
                    )
    for track in snapshot.timeline.tracks:
        if track.kind in {"VOICE", "SFX", "MUSIC"}:
            for clip in track.clips:
                if (
                    clip.source_start_ms + clip.duration_ms
                    > durations[clip.source_asset_version_id]
                ):
                    raise DomainError(
                        "RENDER_SOURCE_RANGE_INVALID", "音频入出点超出真实可解码时长", 409
                    )


def admit_render(
    db: Session, settings: Settings, manifest_id: UUID, actor: str
) -> RenderAdmissionRead:
    """Must be called again by the renderer at execution; this DTO grants no delivery."""
    from arclight.application.render_reviews import require_export_approval

    row = manifest_row(db, manifest_id, actor, write=True)
    facts = admission_facts(db, row)
    validate_render_objects(db, settings, facts)
    review_id, decision_id = require_export_approval(db, row, facts)
    return RenderAdmissionRead(
        manifest_id=row.id,
        manifest_sha256=row.content_sha256,
        profile=facts.snapshot.output.name,
        review_request_id=review_id,
        decision_id=decision_id,
        checked_at=utcnow(),
    )
