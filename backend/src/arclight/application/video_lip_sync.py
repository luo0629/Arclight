"""Create a non-selecting video replacement from exact private media, with no provider call."""

import base64
from dataclasses import dataclass
from hashlib import sha256
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.asset_objects import object_key, write_object
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import (
    capture_manifest,
    lock_dependency_graph,
    require_manifest,
)
from arclight.application.human_reviews import require_review_actor
from arclight.application.lip_sync_admission import exact_sync_audio, shot_dialogue
from arclight.application.render_inputs import file_storage
from arclight.application.video_candidates import (
    ImportedVideo,
    append_video_candidate_version,
    check_video_candidate_media,
)
from arclight.application.video_review_work import VideoReviewInputs, review_inputs
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.candidate_checks import CheckedVideo
from arclight.domain.dependencies import ManifestCreate
from arclight.domain.lip_sync import (
    MAX_SYNC_LINES,
    MAX_SYNC_MS,
    LipSyncAudioOption,
    LipSyncCreate,
    LipSyncDialogueOption,
    LipSyncLine,
    LipSyncOptions,
    LipSyncReceipt,
)
from arclight.domain.versions import digest
from arclight.media.execution import MediaExecutionError, execute_media
from arclight.media.lip_sync import MAX_AUDIO_BYTES, MAX_VIDEO_BYTES, LineMedia, SyncMedia
from arclight.media.render_plan import RenderAudioSource
from arclight.media.validation import probe
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import (
    AssetVersion,
    GenerationAttempt,
    GenerationCandidate,
    StoredFile,
    Storyboard,
    VisualAsset,
)


@dataclass(frozen=True)
class SyncInputs:
    video: VideoReviewInputs
    board: Storyboard
    asset: VisualAsset
    lines: tuple[LipSyncLine, ...]
    video_file: StoredFile
    audio_files: tuple[StoredFile, ...]
    source_rights_note: str


def exact_video_file(db: Session, project_id: UUID, inputs: VideoReviewInputs) -> StoredFile:
    media = inputs.check.video
    assert media is not None
    file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == inputs.version.id))
    if (
        file is None
        or file.project_id != project_id
        or file.checksum != media.checksum
        or file.byte_size != media.byte_size
        or file.object_key != inputs.version.object_key
        or file.media_type != "video/mp4"
        or not file.source_note.strip()
        or not file.rights_note.strip()
    ):
        raise DomainError("LIP_SYNC_SOURCE_INVALID", "原候选缺少精确视频文件回执", 409)
    return file


def available_cues(db: Session, project_id: UUID, candidate_id: UUID) -> LipSyncOptions:
    inputs = review_inputs(db, project_id, candidate_id)
    board = db.get(Storyboard, inputs.target.storyboard_id)
    assert board is not None
    dialogue = shot_dialogue(inputs.shot)
    choices = []
    for line in dialogue:
        sources = []
        if line.lipsync == "required":
            identities = db.scalars(
                select(AudioLineBinding.asset_version_id)
                .join(AudioSnapshot, AudioSnapshot.id == AudioLineBinding.snapshot_id)
                .join(AssetVersion, AssetVersion.id == AudioLineBinding.asset_version_id)
                .join(VisualAsset, VisualAsset.current_version_id == AssetVersion.id)
                .where(
                    AudioSnapshot.episode_id == board.episode_id,
                    AudioLineBinding.line_id == line.line_id,
                    VisualAsset.project_id == project_id,
                )
                .order_by(AssetVersion.created_at.desc(), AssetVersion.id)
                .limit(50)
            ).all()
            for identity in identities:
                try:
                    binding, file = exact_sync_audio(db, project_id, board, line.line_id, identity)
                except DomainError:
                    continue
                sources.append(
                    LipSyncAudioOption(
                        audio_version_id=identity,
                        duration_ms=binding.duration_ms,
                        name=file.original_name,
                    )
                )
        choices.append(
            LipSyncDialogueOption(**line.model_dump(exclude={"emotion", "pacing"}), sources=sources)
        )
    return LipSyncOptions(
        candidate_id=candidate_id,
        requires_video_rights_confirmation=not exact_video_file(
            db, project_id, inputs
        ).rights_confirmed,
        dialogue=choices,
    )


def exact_inputs(
    db: Session, project_id: UUID, source_id: UUID, payload: LipSyncCreate
) -> SyncInputs:
    inputs = review_inputs(db, project_id, source_id)
    board = db.get(Storyboard, inputs.target.storyboard_id)
    asset = db.get(VisualAsset, inputs.version.asset_id, populate_existing=True)
    assert board is not None and asset is not None and inputs.check.video is not None
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "锁定或归档的视频不能另存口型校正候选", 409)
    if asset.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "候选资产已变化，请刷新后重试", 409)
    if inputs.version.metadata_json.get("lip_sync") is not None:
        raise DomainError("LIP_SYNC_SOURCE_INVALID", "局部返修请从原始精确视频重新校正", 409)
    required = [line.line_id for line in shot_dialogue(inputs.shot) if line.lipsync == "required"]
    if (
        not required
        or len(required) > MAX_SYNC_LINES
        or [cue.line_id for cue in payload.cues] != required
    ):
        raise DomainError(
            "LIP_SYNC_CUES_INVALID", "须按镜头顺序覆盖全部可见对白，不得校正豁免台词", 422
        )
    media = inputs.check.video
    video_file = exact_video_file(db, project_id, inputs)
    rights = video_file.rights_note if video_file.rights_confirmed else payload.source_rights_note
    if rights is None:
        raise DomainError("VIDEO_RIGHTS_UNCONFIRMED", "请先声明原视频可用于口型衍生制作", 409)
    if media.duration_ms > MAX_SYNC_MS or media.byte_size > MAX_VIDEO_BYTES:
        raise DomainError("LIP_SYNC_LIMIT", "当前本机路线仅支持 15 秒、16 MiB 以内的镜头视频", 422)
    lines = []
    audio_files = []
    previous_end = 0
    for cue in payload.cues:
        binding, file = exact_sync_audio(db, project_id, board, cue.line_id, cue.audio_version_id)
        line = LipSyncLine(
            **cue.model_dump(),
            duration_ms=binding.duration_ms,
            audio_file_id=file.id,
            audio_sha256=file.checksum,
        )
        if (
            previous_end > line.start_ms
            or line.end_ms > media.duration_ms
            or max(line.closed_frame_ms, line.open_frame_ms) >= media.duration_ms
        ):
            raise DomainError(
                "LIP_SYNC_RANGE_INVALID", "台词起止与张闭嘴帧必须在视频内且台词不重叠", 422
            )
        previous_end = line.end_ms
        audio_files.append(file)
        lines.append(line)
    if sum(file.byte_size for file in audio_files) > MAX_AUDIO_BYTES:
        raise DomainError("LIP_SYNC_LIMIT", "该镜头台词音频超出本机校正字节限制", 422)
    return SyncInputs(inputs, board, asset, tuple(lines), video_file, tuple(audio_files), rights)


def frozen_bytes(settings: Settings, file: StoredFile, limit: int) -> str:
    data = file_storage(settings, file).read(file.object_key, limit, file.checksum)
    if len(data) != file.byte_size or sha256(data).hexdigest() != file.checksum:
        raise DomainError("OBJECT_UNAVAILABLE", "口型输入与不可变文件回执不一致", 409)
    return base64.b64encode(data).decode("ascii")


def media_input(settings: Settings, sources: SyncInputs) -> bytes:
    return (
        SyncMedia(
            video=frozen_bytes(settings, sources.video_file, MAX_VIDEO_BYTES),
            lines=tuple(
                LineMedia(
                    audio=RenderAudioSource.model_validate(
                        {
                            "data": frozen_bytes(settings, file, MAX_AUDIO_BYTES),
                            "media_type": file.media_type,
                        }
                    ),
                    **line.model_dump(
                        exclude={"line_id", "audio_version_id", "audio_file_id", "audio_sha256"}
                    ),
                )
                for line, file in zip(sources.lines, sources.audio_files, strict=True)
            ),
        )
        .model_dump_json()
        .encode()
    )


def render_correction(data: bytes) -> ImportedVideo:
    try:
        result = execute_media("lip-sync", "mouth-pose", data, timeout=60)
    except MediaExecutionError as error:
        if error.reason in {"BUSY", "SANDBOX_UNAVAILABLE"}:
            raise DomainError(
                "LIP_SYNC_UNAVAILABLE", "本机媒体校正服务暂不可用，请稍后重试", 503
            ) from error
        raise DomainError(
            "LIP_SYNC_RENDER_FAILED",
            "无法校正口型：请检查可辨识的张闭嘴帧、固定帧率、正面口部区域和音频范围",
            422,
        ) from error
    try:
        check = CheckedVideo.model_validate(
            {
                **probe(result, "video/mp4"),
                "checksum": sha256(result).hexdigest(),
                "byte_size": len(result),
            }
        )
    except ValidationError as error:
        raise DomainError("LIP_SYNC_RENDER_FAILED", "校正输出未满足完整视频校验", 409) from error
    if not check.has_audio:
        raise DomainError("LIP_SYNC_RENDER_FAILED", "校正输出缺少同步对白轨", 409)
    return ImportedVideo(result, check)


def save_candidate(
    db: Session,
    settings: Settings,
    actor: str,
    payload: LipSyncCreate,
    request_hash: str,
    sources: SyncInputs,
    video: ImportedVideo,
) -> UUID:
    inputs = sources.video
    job = inputs.job
    prior = db.get(GenerationCandidate, inputs.target.candidate_id)
    assert prior is not None and inputs.check.video is not None
    attempt = db.get(GenerationAttempt, prior.attempt_id)
    assert attempt is not None
    original = require_manifest(db, job.project_id, job.input_manifest_id)
    source_media = inputs.check.video
    if (
        video.check.duration_ms != source_media.duration_ms
        or video.check.decoded_frames != source_media.decoded_frames
        or video.check.frame_rate != source_media.frame_rate
        or (video.check.width, video.check.height) != (source_media.width, source_media.height)
    ):
        raise DomainError("LIP_SYNC_RENDER_FAILED", "局部校正不得改变镜头画幅、帧率或时长", 409)
    identity = uuid4()
    receipt = LipSyncReceipt(
        source_candidate_id=prior.id,
        source_video_version_id=inputs.version.id,
        source_video_sha256=inputs.version.checksum,
        script_version_id=sources.board.script_version_id,
        output_sha256=video.check.checksum,
        lines=sources.lines,
    )
    manifest, _ = capture_manifest(
        db,
        job.project_id,
        actor,
        ManifestCreate.model_validate(
            {
                "creation_key": uuid5(payload.creation_key, "lip-sync-inputs"),
                "target_asset_id": sources.asset.id,
                "inputs": [
                    *[
                        {
                            key: row[key]
                            for key in ("source_asset_version_id", "dependency_type", "selectors")
                        }
                        for row in original.snapshot_json["inputs"]
                    ],
                    *[
                        {
                            "source_asset_version_id": line.audio_version_id,
                            "dependency_type": "CONTENT_DEPENDENCY",
                            "selectors": ["dialogue:" + line.line_id],
                        }
                        for line in receipt.lines
                    ],
                ],
                "context": original.snapshot_json["context"]
                | {"lip_sync": receipt.model_dump(mode="json")},
            }
        ),
    )
    file_id = uuid4()
    notes = {
        "original_name": "lip-sync.mp4",
        "source_note": f"本机口型校正：{inputs.version.id}；精确台词音频见输入清单",
        "rights_note": sources.source_rights_note,
        "rights_confirmed": True,
    }
    write_object(db, settings, job.project_id, file_id, video.data, "video/mp4")
    db.add(
        StoredFile(
            id=file_id,
            project_id=job.project_id,
            object_key=object_key(job.project_id, file_id, settings),
            storage_backend=settings.object_storage_backend.upper(),
            bucket=settings.s3_bucket if settings.object_storage_backend == "s3" else None,
            checksum=video.check.checksum,
            byte_size=video.check.byte_size,
            media_type="video/mp4",
            created_by=actor,
            **notes,
        )
    )
    db.flush()
    check = check_video_candidate_media(db, job, attempt, identity, video, manifest=manifest)
    output = append_video_candidate_version(
        db,
        settings,
        job,
        attempt,
        video,
        payload.creation_key,
        actor,
        candidate_id=identity,
        media_notes=notes,
        source_file_id=file_id,
        replaces_version_id=prior.asset_version_id,
        expected_revision=payload.expected_revision,
        manifest=manifest,
        lip_sync=receipt,
    )
    if output.stale:
        raise DomainError("LIP_SYNC_SOURCE_CHANGED", "校正期间精确音视频输入已失效", 409)
    db.add(
        GenerationCandidate(
            id=identity,
            project_id=job.project_id,
            job_id=job.id,
            attempt_id=attempt.id,
            input_manifest_id=manifest.id,
            source="REPLACEMENT",
            asset_version_id=output.id,
            source_file_id=file_id,
            replaces_candidate_id=prior.id,
            creation_key=payload.creation_key,
            request_fingerprint=request_hash,
            created_by=actor,
            media_check_json=check.model_dump(mode="json"),
        )
    )
    db.flush()
    audit(
        db,
        job.project_id,
        actor,
        "generation.video_lip_sync_created",
        identity,
        {
            "source_candidate_id": str(prior.id),
            "manifest_id": str(manifest.id),
            "asset_version_id": str(output.id),
            "engine": receipt.engine,
        },
    )
    return identity


def replay(db: Session, project_id: UUID, key: UUID, request_hash: str) -> UUID | None:
    previous = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id, GenerationCandidate.creation_key == key
        )
    )
    if previous is None:
        return None
    if previous.request_fingerprint != request_hash:
        raise DomainError("IDEMPOTENCY_CONFLICT", "校正保存键不能更换镜头或输入", 409)
    return previous.id


def lock_write(db: Session, project_id: UUID, actor: str) -> None:
    require_project(db, project_id, actor)
    lock_dependency_graph(db, project_id)
    require_review_actor(db, project_id, actor, owner=False)


def create_lip_sync(
    factory: sessionmaker[Session],
    settings: Settings,
    project_id: UUID,
    source_id: UUID,
    actor: str,
    payload: LipSyncCreate,
) -> UUID:
    request_hash = digest(
        {
            "lip_sync_source": str(source_id),
            "cues": [cue.model_dump(mode="json") for cue in payload.cues],
            "source_rights_note": payload.source_rights_note,
        }
    )
    with factory.begin() as db:
        lock_write(db, project_id, actor)
        if prior := replay(db, project_id, payload.creation_key, request_hash):
            return prior
        sources = exact_inputs(db, project_id, source_id, payload)
        source_version, script_version, lines = (
            sources.video.target,
            sources.board.script_version_id,
            sources.lines,
        )
        encoded = media_input(settings, sources)
    # The sandbox never holds a project database lock. Recheck CAS and exact inputs after encoding.
    output = render_correction(encoded)
    with factory.begin() as db:
        lock_write(db, project_id, actor)
        if prior := replay(db, project_id, payload.creation_key, request_hash):
            return prior
        current = exact_inputs(db, project_id, source_id, payload)
        if (current.video.target, current.board.script_version_id, current.lines) != (
            source_version,
            script_version,
            lines,
        ):
            raise DomainError("LIP_SYNC_SOURCE_CHANGED", "校正期间精确音视频输入已变化", 409)
        return save_candidate(db, settings, actor, payload, request_hash, current, output)
