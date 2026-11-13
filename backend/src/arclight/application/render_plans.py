"""Compile frozen admitted images into a byte-only plan with bounded render work."""

import base64

from pydantic import ValidationError
from sqlalchemy.orm import Session

from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.render_manifests import RenderManifestSnapshot
from arclight.domain.timeline_visuals import ClipVisual
from arclight.media.execution import MediaObserver
from arclight.media.render_plan import (
    MAX_FRAMES,
    MAX_PIXEL_FRAMES,
    RenderClip,
    RenderPlan,
)
from arclight.media.versions import renderer_software
from arclight.persistence.models import StoredFile


def check_support(snapshot: RenderManifestSnapshot) -> None:
    if any(track.clips for track in snapshot.timeline.tracks if track.kind != "VIDEO"):
        raise DomainError(
            "RENDER_TRACK_UNSUPPORTED", "混音和字幕轨尚未接入，请勿忽略其内容导出", 409
        )
    if snapshot.software != renderer_software():
        raise DomainError(
            "RENDER_SOFTWARE_CHANGED", "渲染软件版本已变化，请另建清单并重新批准", 409
        )
    frames = (snapshot.timeline.duration_ms * snapshot.output.fps + 999) // 1000
    if (
        frames > MAX_FRAMES
        or frames * snapshot.output.width * snapshot.output.height > MAX_PIXEL_FRAMES
    ):
        raise DomainError("RENDER_WORK_LIMIT", "渲染超出当前受限编码器的帧数或像素工作量", 409)


def build_plan(
    db: Session,
    settings: Settings,
    snapshot: RenderManifestSnapshot,
    *,
    observer: MediaObserver | None = None,
) -> bytes:
    check_support(snapshot)
    sources = {item.asset_version_id: item for item in snapshot.sources}
    fades = snapshot.timeline.video_fades()
    clips = []
    for clip in sorted(
        next(t for t in snapshot.timeline.tracks if t.kind == "VIDEO").clips,
        key=lambda c: c.ordinal,
    ):
        if observer is not None:
            observer(None)
        reference = sources[clip.source_asset_version_id].file
        assert reference is not None
        file = db.get(StoredFile, reference.file_id)
        assert file is not None
        data = file_storage(settings, file).read(file.object_key, None, reference.sha256)
        if len(data) != reference.byte_size:
            raise DomainError("OBJECT_UNAVAILABLE", "精确图片大小与文件回执不符", 409)
        clips.append(
            RenderClip(
                image=base64.b64encode(data).decode("ascii"),
                start_ms=clip.start_ms,
                duration_ms=clip.duration_ms,
                visual=clip.visual or ClipVisual(),
                fade_in_ms=fades[clip.id].incoming_ms,
                fade_out_ms=fades[clip.id].outgoing_ms,
            )
        )
    try:
        plan = RenderPlan(
            output=snapshot.output, duration_ms=snapshot.timeline.duration_ms, clips=tuple(clips)
        )
    except ValidationError:
        raise DomainError("RENDER_PLAN_INVALID", "渲染计划超限或时间范围无效", 409) from None
    return plan.model_dump_json().encode()
