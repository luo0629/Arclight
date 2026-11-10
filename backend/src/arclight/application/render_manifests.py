"""Capture immutable render inputs under existing project/episode authorization and DAG locks."""

from datetime import UTC
from math import gcd
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import require_manifest
from arclight.application.render_inputs import capture_font, capture_sources
from arclight.application.timeline_store import read_timeline
from arclight.application.timelines import write_episode
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import (
    OutputGeometry,
    RenderManifestCreate,
    RenderManifestRead,
    RenderManifestSnapshot,
    RenderOutputProfile,
    RenderProfile,
    RenderVisual,
)
from arclight.domain.timeline_visuals import ClipVisual
from arclight.domain.versions import digest
from arclight.media.versions import renderer_software
from arclight.persistence.base import utcnow
from arclight.persistence.models import DependencyOutput
from arclight.persistence.render_manifests import RenderManifest
from arclight.persistence.timelines import Timeline


def output_profile(geometry: dict, profile: RenderProfile) -> RenderOutputProfile:
    try:
        original = OutputGeometry.model_validate(geometry)
    except ValidationError:
        raise DomainError(
            "RENDER_PROFILE_INVALID", "时间轴输出规格不受当前编码器支持", 409
        ) from None
    width, height, fps = original.width, original.height, original.fps
    if profile == "PREVIEW":
        # Even dimensions with exactly the original aspect ratio; never upscale.
        divisor = gcd(width, height)
        unit_width, unit_height = 2 * width // divisor, 2 * height // divisor
        multiple = min(divisor // 2, 640 // max(unit_width, unit_height))
        if multiple < 1:
            raise DomainError("RENDER_PROFILE_INVALID", "无法生成保持画幅的有界预览尺寸", 409)
        width, height, fps = unit_width * multiple, unit_height * multiple, min(fps, 24)
    return RenderOutputProfile(
        name=profile,
        width=width,
        height=height,
        fps=fps,
        video_bitrate=2_000_000 if profile == "PREVIEW" else 8_000_000,
        audio_bitrate=96_000 if profile == "PREVIEW" else 192_000,
    )


def manifest_row(db: Session, manifest_id: UUID, actor: str, *, write: bool = False):
    row = db.get(RenderManifest, manifest_id)
    if row is None:
        raise DomainError("NOT_FOUND", "渲染清单不存在", 404)
    require_project(db, row.project_id, actor)
    if write:
        timeline = db.get(Timeline, row.timeline_id)
        assert timeline is not None
        write_episode(db, timeline.episode_id, actor)
    return row


def read_render_manifest(db: Session, manifest_id: UUID, actor: str) -> RenderManifestRead:
    row = manifest_row(db, manifest_id, actor)
    try:
        snapshot = RenderManifestSnapshot.model_validate(row.snapshot_json)
        valid = (
            digest(row.snapshot_json) == row.content_sha256
            and snapshot.timeline_id == row.timeline_id
            and snapshot.dependency_manifest_id == row.dependency_manifest_id
            and (snapshot.font.file_id if snapshot.font else None) == row.font_file_id
            and digest(snapshot.timeline.model_dump(mode="json")) == snapshot.timeline_sha256
            and snapshot.created_at
            == (
                row.created_at.replace(tzinfo=UTC)
                if row.created_at.tzinfo is None
                else row.created_at.astimezone(UTC)
            )
        )
    except (ValidationError, ValueError, TypeError):
        valid = False
    if not valid:
        raise DomainError("RENDER_HISTORY_INVALID", "渲染清单内容与不可变摘要不符", 409)
    return RenderManifestRead(
        id=row.id,
        project_id=row.project_id,
        created_by=row.created_by,
        content_sha256=row.content_sha256,
        snapshot=snapshot,
    )


def create_render_manifest(
    db: Session,
    settings: Settings,
    timeline_id: UUID,
    actor: str,
    payload: RenderManifestCreate,
) -> RenderManifestRead:
    timeline_row = db.get(Timeline, timeline_id)
    if timeline_row is None or not timeline_row.sealed:
        raise DomainError("NOT_FOUND", "时间轴版本不存在", 404)
    episode = write_episode(db, timeline_row.episode_id, actor)
    request_hash = digest(
        {
            "timeline_id": str(timeline_id),
            **payload.model_dump(mode="json", exclude={"creation_key"}),
        }
    )
    previous = db.scalar(
        select(RenderManifest).where(
            RenderManifest.project_id == episode.project_id,
            RenderManifest.creation_key == payload.creation_key,
        )
    )
    if previous is not None:
        if previous.request_sha256 != request_hash or previous.created_by != actor:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一渲染键不能用于不同输入或创建者", 409)
        return read_render_manifest(db, previous.id, actor)
    timeline = read_timeline(db, timeline_id, actor)
    dependency = require_manifest(db, episode.project_id, timeline.dependency_manifest_id)
    if (
        db.scalar(
            select(DependencyOutput.id).where(
                DependencyOutput.dependency_manifest_id == dependency.id,
                DependencyOutput.asset_version_id == timeline_id,
            )
        )
        is None
    ):
        raise DomainError("RENDER_INPUT_INVALID", "时间轴缺少精确依赖产物回执", 409)
    sources = capture_sources(db, settings, episode.project_id, timeline.snapshot)
    font = capture_font(
        db,
        settings,
        episode.project_id,
        payload.font_file_id,
        required=any(track.clips for track in timeline.snapshot.tracks if track.kind == "SUBTITLE"),
    )
    now = utcnow()
    snapshot = RenderManifestSnapshot(
        timeline_id=timeline.id,
        timeline_sha256=timeline.content_sha256,
        dependency_manifest_id=dependency.id,
        dependency_manifest_sha256=digest(dependency.snapshot_json),
        timeline=timeline.snapshot,
        visuals=tuple(
            RenderVisual(clip_id=clip.id, settings=clip.visual or ClipVisual())
            for track in timeline.snapshot.tracks
            if track.kind == "VIDEO"
            for clip in sorted(track.clips, key=lambda clip: clip.ordinal)
        ),
        sources=sources,
        output=output_profile(timeline.snapshot.output_profile, payload.profile),
        subtitle_style=payload.subtitle_style,
        font=font,
        software=renderer_software(),
        created_at=now,
    )
    frozen = snapshot.model_dump(mode="json")
    row = RenderManifest(
        id=uuid4(),
        project_id=episode.project_id,
        timeline_id=timeline_id,
        dependency_manifest_id=dependency.id,
        font_file_id=font.file_id if font else None,
        creation_key=payload.creation_key,
        request_sha256=request_hash,
        content_sha256=digest(frozen),
        snapshot_json=frozen,
        created_by=actor,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        episode.project_id,
        actor,
        "render.manifest_created",
        row.id,
        {"timeline_id": str(timeline_id), "profile": payload.profile, "sha256": row.content_sha256},
    )
    return read_render_manifest(db, row.id, actor)
