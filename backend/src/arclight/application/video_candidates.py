"""Register decoded video in the existing immutable candidate and asset history."""

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.asset_objects import object_key, write_object
from arclight.application.audit import audit
from arclight.application.dependencies import (
    attach_output,
    lock_dependency_graph,
    require_manifest,
    validate_manifest_inputs,
)
from arclight.application.files import record_asset_file
from arclight.application.generation_video_inputs import validate_video_images
from arclight.application.production_versions import bind_output, digest
from arclight.application.render_inputs import file_storage
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.candidate_checks import CandidateVideoCheck, CheckedVideo
from arclight.domain.file_policy import POLICIES
from arclight.domain.generation_requests import ShotReviewBinding
from arclight.media.validation import probe
from arclight.persistence.models import (
    AssetVersion,
    DependencyManifest,
    FileRegistration,
    GenerationAttempt,
    GenerationCandidate,
    GenerationJob,
    GenerationMediaArtifact,
    StoredFile,
    User,
    VisualAsset,
)


@dataclass(frozen=True)
class ImportedVideo:
    data: bytes
    check: CheckedVideo


def validated_video(settings: Settings, file: StoredFile) -> ImportedVideo:
    """Re-read the recorded private object and run the existing bounded decoder."""
    if file.media_type != "video/mp4":
        raise DomainError("INVALID_VIDEO", "镜头视频必须是可完整解码的 MP4", 422)
    data = file_storage(settings, file).read(
        file.object_key, POLICIES[file.media_type].max_bytes, file.checksum
    )
    if len(data) != file.byte_size or hashlib.sha256(data).hexdigest() != file.checksum:
        raise DomainError("OBJECT_UNAVAILABLE", "视频与不可变文件回执不一致", 409)
    try:
        check = CheckedVideo.model_validate(
            {**probe(data, file.media_type), "checksum": file.checksum, "byte_size": len(data)}
        )
    except ValidationError as error:
        raise DomainError("INVALID_VIDEO", "视频缺少完整且有效的实测解码证明", 422) from error
    return ImportedVideo(data, check)


def video_sources(
    db: Session,
    job: GenerationJob,
    attempt: GenerationAttempt,
    manifest: DependencyManifest | None = None,
) -> tuple[DependencyManifest, dict]:
    """Validate actual production provenance; current pointers never replace its inputs."""
    from arclight.application.continuity_workflow import require_shot_binding
    from arclight.application.dependencies import exact_version

    original = require_manifest(db, job.project_id, job.input_manifest_id)
    if (
        job.task_type != "VIDEO"
        or job.target_type != "VIDEO"
        or attempt.job_id != job.id
        or attempt.input_manifest_id != original.id
        or attempt.status != "SUCCEEDED"
        or digest(attempt.input_snapshot_json) != digest(original.snapshot_json)
    ):
        raise DomainError("VIDEO_SOURCE_INVALID", "镜头视频与原始生成任务不匹配", 409)
    try:
        context = original.snapshot_json["context"]
        binding = ShotReviewBinding.model_validate(context.get("continuity_review"))
    except (KeyError, TypeError, ValidationError) as error:
        raise DomainError(
            "VIDEO_SOURCE_REQUIRED", "正式镜头视频须绑定精确分镜和镜头", 409
        ) from error
    require_shot_binding(db, job.project_id, binding, job.episode_id)
    board_version, board_asset = exact_version(db, job.project_id, binding.storyboard_id)
    if (
        board_version.stale
        or board_version.status not in {"APPROVED", "LOCKED"}
        or board_asset.status == "ARCHIVED"
    ):
        raise DomainError("ASSET_NOT_USABLE", "精确分镜版本已失效或不可用于生产", 409)
    if not any(
        item["source_asset_version_id"] == str(binding.storyboard_id)
        and item["dependency_type"] == "CONTENT_DEPENDENCY"
        and (not item.get("selectors") or "shot:" + str(binding.shot_id) in item["selectors"])
        for item in original.snapshot_json["inputs"]
    ):
        raise DomainError("VIDEO_SOURCE_REQUIRED", "镜头视频缺少精确分镜内容依赖", 409)
    if not context.get("video_images"):
        raise DomainError("VIDEO_SOURCE_REQUIRED", "镜头视频缺少已批准精确首帧来源", 409)
    validate_manifest_inputs(db, job.project_id, original, "VIDEO")
    validate_video_images(db, job, original.snapshot_json)
    effective = manifest or original
    if effective.id != original.id:
        # Reusing historical bytes cannot assert they were generated from new references.
        identity_fields = (
            "source_asset_version_id",
            "asset_id",
            "asset_type",
            "version_no",
            "checksum",
            "dependency_type",
            "selectors",
        )
        original_inputs = [
            {key: item.get(key) for key in identity_fields}
            for item in original.snapshot_json["inputs"]
        ]
        effective_inputs = [
            {key: item.get(key) for key in identity_fields}
            for item in effective.snapshot_json["inputs"]
        ]
        if effective_inputs != original_inputs:
            raise DomainError("VIDEO_SOURCE_CHANGED", "旧视频不能改称由新的输入版本生成", 409)
        validate_manifest_inputs(db, job.project_id, effective, "VIDEO")
    return effective, {
        "schema_version": "shot-video-source/1",
        "job_id": str(job.id),
        "attempt_id": str(attempt.id),
        "generation_manifest_id": str(original.id),
        "generation_manifest_sha256": digest(original.snapshot_json),
        **binding.model_dump(mode="json"),
        "video_images": deepcopy(context["video_images"]),
    }


def check_video_candidate_media(
    db: Session,
    job: GenerationJob,
    attempt: GenerationAttempt,
    candidate_id: UUID,
    video: ImportedVideo | None,
    error_code: str | None = None,
    *,
    manifest: DependencyManifest | None = None,
) -> CandidateVideoCheck:
    effective = manifest or require_manifest(db, job.project_id, job.input_manifest_id)
    references_valid = False
    try:
        video_sources(db, job, attempt, effective)
        references_valid = True
        if video is not None:
            expected = attempt.input_snapshot_json["context"]["generation"]["output_spec"]
            actual = video.check
            tolerance = max(50, round(1000 / actual.frame_rate))
            if (
                any(
                    expected.get(key, getattr(actual, key)) != getattr(actual, key)
                    for key in ("width", "height")
                )
                or expected.get("format", "mp4") != "mp4"
                or "duration_seconds" in expected
                and abs(actual.duration_ms - round(expected["duration_seconds"] * 1000)) > tolerance
                or "frame_rate" in expected
                and expected["frame_rate"] != actual.frame_rate
                or "fps" in expected
                and expected["fps"] != actual.frame_rate
            ):
                error_code = error_code or "VIDEO_OUTPUT_MISMATCH"
    except DomainError as error:
        error_code = error_code or error.code
    if video is None:
        error_code = error_code or "INVALID_VIDEO"
    return CandidateVideoCheck(
        candidate_id=candidate_id,
        input_manifest_id=effective.id,
        manifest_sha256=digest(effective.snapshot_json),
        status="FAILED" if error_code else "PASSED",
        references_valid=references_valid,
        video=video.check if video else None,
        error_code=error_code,
    )


def append_video_candidate_version(
    db: Session,
    settings: Settings,
    job: GenerationJob,
    attempt: GenerationAttempt,
    video: ImportedVideo,
    creation_key: UUID,
    actor: str,
    *,
    candidate_id: UUID,
    media_notes: dict,
    source_file_id: UUID,
    artifact_id: UUID | None = None,
    replaces_version_id: UUID | None = None,
    manifest: DependencyManifest | None = None,
    restored_from_version_id: UUID | None = None,
    restoration_note: str | None = None,
    expected_revision: int | None = None,
) -> AssetVersion:
    from arclight.application.generation_candidates import candidate_asset

    check = check_video_candidate_media(db, job, attempt, candidate_id, video, manifest=manifest)
    if check.status != "PASSED":
        raise DomainError(
            check.error_code or "VIDEO_SOURCE_INVALID", "视频文件或生产来源不符合冻结合同", 409
        )
    effective, origin = video_sources(db, job, attempt, manifest)
    source = db.get(StoredFile, source_file_id)
    if (
        source is None
        or source.project_id != job.project_id
        or source.media_type != video.check.media_type
        or source.checksum != video.check.checksum
        or source.byte_size != video.check.byte_size
    ):
        raise DomainError("VIDEO_SOURCE_INVALID", "视频文件来源与实测数据不一致", 409)
    if artifact_id is None and restored_from_version_id is None and not source.rights_confirmed:
        raise DomainError("VIDEO_RIGHTS_UNCONFIRMED", "替换视频须先确认使用授权", 409)
    if artifact_id is not None:
        artifact = db.get(GenerationMediaArtifact, artifact_id)
        if (
            artifact is None
            or artifact.job_id != job.id
            or artifact.attempt_id != attempt.id
            or artifact.file_id != source.id
            or artifact.error_code is not None
            or any(
                video.check.model_dump(mode="json").get(key) != value
                for key, value in artifact.media_json.items()
            )
        ):
            raise DomainError("VIDEO_SOURCE_INVALID", "视频原始结果回执不匹配", 409)
    asset = candidate_asset(db, job)
    prior_imports = (
        db.scalar(
            select(func.count(GenerationCandidate.id)).where(
                GenerationCandidate.job_id == job.id,
                GenerationCandidate.source == "GENERATED",
                GenerationCandidate.asset_version_id.is_not(None),
            )
        )
        or 0
    )
    changed_target = expected_revision is None and (
        asset.status in {"LOCKED", "ARCHIVED"}
        or job.target_asset_id is not None
        and (
            asset.current_version_id != job.expected_current_version_id
            or asset.lock_version != job.expected_lock_version
            or asset.revision != (job.expected_asset_revision or 0) + prior_imports
        )
    )
    if expected_revision is not None:
        from arclight.application.asset_mutations import change_asset

        change_asset(db, asset, expected_revision)
    version = AssetVersion(
        id=uuid4(),
        asset_id=asset.id,
        version_no=(
            db.scalar(
                select(func.max(AssetVersion.version_no)).where(AssetVersion.asset_id == asset.id)
            )
            or 0
        )
        + 1,
        checksum=video.check.checksum,
        creation_key=creation_key,
        created_by=actor,
        status="DRAFT",
        stale=changed_target,
        metadata_json={
            "storage_backend": settings.object_storage_backend.upper(),
            "dependency_manifest_id": str(effective.id),
            "generation_job_id": str(job.id),
            "generation_attempt_id": str(attempt.id),
            "required_review": "VIDEO_TEMPORAL",
            "target_changed_since_submission": changed_target,
            "replaces_version_id": str(replaces_version_id) if replaces_version_id else None,
            "video_source": {
                **origin,
                "artifact_id": str(artifact_id) if artifact_id else None,
                "source_file_id": str(source.id),
            },
            "video_check": video.check.model_dump(mode="json"),
            "candidate_id": str(candidate_id),
            "asset_snapshot": {
                "id": str(asset.id),
                "name": asset.name,
                "asset_type": asset.asset_type,
                "revision": asset.revision,
                "current_version_id": str(asset.current_version_id)
                if asset.current_version_id
                else None,
            },
            "media": {
                **video.check.model_dump(mode="json", exclude={"checksum"}),
                **media_notes,
            },
            **(
                {
                    "restored_from_version_id": str(restored_from_version_id),
                    "restoration_note": restoration_note,
                }
                if restored_from_version_id
                else {}
            ),
        },
    )
    version.object_key = object_key(job.project_id, version.id, settings)
    write_object(db, settings, job.project_id, version.id, video.data, video.check.media_type)
    db.add(version)
    if expected_revision is None:
        # Late generated results retain stale history, never the selection,
        # lifecycle or lock of a target made read-only while the call ran.
        asset.revision += 1
    db.flush()
    record_asset_file(db, settings, job.project_id, version)
    db.add(FileRegistration(file_id=source.id, asset_version_id=version.id, created_by=actor))
    attach_output(db, job.project_id, effective.id, version)
    bind_output(db, job.project_id, "ATTEMPT", attempt.id, "ASSET_VERSION", version.id)
    return version


def require_video_candidate_media_check(
    db: Session, project_id: UUID, candidate_id: UUID
) -> CandidateVideoCheck:
    candidate = db.get(GenerationCandidate, candidate_id)
    if candidate is None or candidate.project_id != project_id:
        raise DomainError("NOT_FOUND", "镜头视频候选不存在", 404)
    try:
        check = CandidateVideoCheck.model_validate(candidate.media_check_json)
    except ValidationError as error:
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频缺少有效的完整媒体检查", 409) from error
    version = (
        db.get(AssetVersion, candidate.asset_version_id) if candidate.asset_version_id else None
    )
    job = db.get(GenerationJob, candidate.job_id)
    attempt = db.get(GenerationAttempt, candidate.attempt_id)
    manifest = require_manifest(db, project_id, candidate.input_manifest_id)
    if (
        check.status != "PASSED"
        or candidate.error_code is not None
        or check.candidate_id != candidate.id
        or check.input_manifest_id != manifest.id
        or check.manifest_sha256 != digest(manifest.snapshot_json)
        or version is None
        or job is None
        or attempt is None
        or job.project_id != project_id
        or version.metadata_json.get("candidate_id") != str(candidate.id)
        or version.metadata_json.get("generation_job_id") != str(job.id)
        or version.metadata_json.get("generation_attempt_id") != str(attempt.id)
        or version.metadata_json.get("dependency_manifest_id") != str(manifest.id)
        or check.video is None
        or check.video.checksum != version.checksum
        or version.metadata_json.get("video_check") != check.video.model_dump(mode="json")
        or any(
            version.metadata_json.get("media", {}).get(key) != value
            for key, value in check.video.model_dump(mode="json").items()
            if key != "checksum"
        )
    ):
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频候选与精确文件检查不匹配", 409)
    asset = db.get(VisualAsset, version.asset_id)
    if asset is None or asset.project_id != project_id or asset.asset_type != "VIDEO":
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频资产身份不匹配", 409)
    _, origin = video_sources(db, job, attempt, manifest)
    recorded = version.metadata_json.get("video_source", {})
    if any(recorded.get(key) != value for key, value in origin.items()):
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频生产来源已变化", 409)
    try:
        source_file = db.get(StoredFile, UUID(recorded["source_file_id"]))
        artifact = (
            db.get(GenerationMediaArtifact, UUID(recorded["artifact_id"]))
            if recorded.get("artifact_id")
            else None
        )
    except (KeyError, ValueError, TypeError) as error:
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频缺少精确原始文件身份", 409) from error
    if (
        source_file is None
        or source_file.project_id != project_id
        or source_file.checksum != check.video.checksum
        or source_file.byte_size != check.video.byte_size
        or source_file.media_type != check.video.media_type
        or candidate.source == "GENERATED"
        and (
            artifact is None
            or artifact.attempt_id != attempt.id
            or artifact.job_id != job.id
            or artifact.file_id != source_file.id
            or artifact.ordinal != candidate.ordinal
            or artifact.error_code is not None
        )
        or candidate.source == "REPLACEMENT"
        and candidate.source_file_id != source_file.id
    ):
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频原始文件与候选身份不一致", 409)
    return check


def require_video_version(
    db: Session,
    settings: Settings,
    project_id: UUID,
    asset: VisualAsset,
    version: AssetVersion,
    actor: str,
) -> ImportedVideo:
    from arclight.application.commands import require_project

    require_project(db, project_id, actor)
    if (
        asset.project_id != project_id
        or asset.asset_type != "VIDEO"
        or version.asset_id != asset.id
    ):
        raise DomainError("NOT_FOUND", "镜头视频版本不存在", 404)
    candidate = db.scalar(
        select(GenerationCandidate).where(GenerationCandidate.asset_version_id == version.id)
    )
    if candidate is None:
        raise DomainError("MEDIA_CHECK_REQUIRED", "镜头视频尚未登记有效候选", 409)
    check = require_video_candidate_media_check(db, project_id, candidate.id)
    stored = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if stored is None or stored.project_id != project_id or stored.object_key != version.object_key:
        raise DomainError("OBJECT_UNAVAILABLE", "视频缺少精确私有文件回执", 409)
    video = validated_video(settings, stored)
    if video.check != check.video:
        raise DomainError("MEDIA_CHECK_REQUIRED", "视频实测结果与不可变候选检查不一致", 409)
    return video


def restore_video_version_as_new(
    db: Session,
    settings: Settings,
    project_id: UUID,
    asset: VisualAsset,
    source: AssetVersion,
    *,
    creation_key: UUID,
    expected_revision: int,
    comment: str,
    dependency_manifest_id: UUID | None,
    actor: str,
) -> AssetVersion:
    from arclight.application.commands import require_project

    require_project(db, project_id, actor, write=True)
    existing = db.scalar(
        select(AssetVersion).where(
            AssetVersion.asset_id == asset.id, AssetVersion.creation_key == creation_key
        )
    )
    if existing is not None:
        if (
            existing.metadata_json.get("restored_from_version_id") != str(source.id)
            or existing.metadata_json.get("restoration_note") != comment
            or existing.metadata_json.get("dependency_manifest_id")
            != (str(dependency_manifest_id) if dependency_manifest_id else None)
        ):
            raise DomainError("IDEMPOTENCY_CONFLICT", "恢复标识不能更换视频或输入来源", 409)
        return existing
    if asset.status in {"ARCHIVED", "LOCKED"}:
        raise DomainError("ASSET_READ_ONLY", "锁定或归档的视频资产不能恢复为新版本", 409)
    if dependency_manifest_id is None:
        raise DomainError("DEPENDENCY_MANIFEST_REQUIRED", "恢复视频须先确认原始精确输入清单", 409)
    manifest = require_manifest(db, project_id, dependency_manifest_id)
    if manifest.target_asset_id != asset.id or manifest.snapshot_json["context"].get(
        "rebuild_from_version_id"
    ) != str(source.id):
        raise DomainError("INVALID_REBUILD_INPUT", "视频恢复清单与原版本不匹配", 409)
    video = require_video_version(db, settings, project_id, asset, source, actor)
    prior = db.scalar(
        select(GenerationCandidate).where(GenerationCandidate.asset_version_id == source.id)
    )
    stored = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == source.id))
    assert prior is not None and stored is not None
    job = db.get(GenerationJob, prior.job_id)
    attempt = db.get(GenerationAttempt, prior.attempt_id)
    assert job is not None and attempt is not None
    candidate_id = uuid4()
    check = check_video_candidate_media(db, job, attempt, candidate_id, video, manifest=manifest)
    if check.status != "PASSED":
        raise DomainError(check.error_code or "VIDEO_SOURCE_INVALID", "视频原始输入已不可恢复", 409)
    notes = {
        key: source.metadata_json["media"][key]
        for key in ("original_name", "source_note", "rights_note", "rights_confirmed")
    }
    version = append_video_candidate_version(
        db,
        settings,
        job,
        attempt,
        video,
        creation_key,
        actor,
        candidate_id=candidate_id,
        media_notes=notes,
        source_file_id=stored.id,
        replaces_version_id=source.id,
        manifest=manifest,
        restored_from_version_id=source.id,
        restoration_note=comment,
        expected_revision=expected_revision,
    )
    db.add(
        GenerationCandidate(
            id=candidate_id,
            project_id=project_id,
            job_id=job.id,
            attempt_id=attempt.id,
            input_manifest_id=manifest.id,
            source="REPLACEMENT",
            asset_version_id=version.id,
            source_file_id=stored.id,
            replaces_candidate_id=prior.id,
            creation_key=creation_key,
            request_fingerprint=digest(
                {
                    "restored_from_version_id": str(source.id),
                    "manifest_id": str(manifest.id),
                    "comment": comment,
                }
            ),
            media_check_json=check.model_dump(mode="json"),
            created_by=actor,
        )
    )
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "asset.video_restored_as_new",
        version.id,
        {"source_version_id": str(source.id), "candidate_id": str(candidate_id)},
    )
    return version


def import_video_candidates(factory: sessionmaker, settings: Settings, attempt_id: UUID) -> int:
    """Read/probe outside the graph lock, then atomically append each immutable outcome."""
    from arclight.application.commands import require_project

    with factory() as db:
        attempt = db.get(GenerationAttempt, attempt_id)
        job = db.get(GenerationJob, attempt.job_id) if attempt else None
        if (
            attempt is None
            or job is None
            or attempt.status != "SUCCEEDED"
            or job.task_type != "VIDEO"
            or not attempt.input_snapshot_json.get("context", {}).get("continuity_review")
        ):
            return 0
        job_id, project_id = job.id, job.project_id
        artifacts = list(
            db.scalars(
                select(GenerationMediaArtifact).where(
                    GenerationMediaArtifact.attempt_id == attempt_id
                )
            )
        )
        pending = []
        for artifact in artifacts:
            if (
                db.scalar(
                    select(GenerationCandidate.id).where(
                        GenerationCandidate.attempt_id == attempt_id,
                        GenerationCandidate.ordinal == artifact.ordinal,
                    )
                )
                is not None
            ):
                continue
            file = db.get(StoredFile, artifact.file_id) if artifact.file_id else None
            if file is not None:
                db.expunge(file)
            pending.append((artifact.id, artifact.ordinal, artifact.error_code, file))
    imported = 0
    for artifact_id, ordinal, error_code, file in pending:
        video = None
        if error_code is None and file is not None:
            try:
                video = validated_video(settings, file)
            except DomainError as error:
                if error.status_code >= 500:
                    continue
                error_code = error.code
            except (OSError, ConnectionError, TimeoutError):
                continue
        with factory.begin() as db:
            try:
                lock_dependency_graph(db, project_id)
            except DomainError as error:
                if error.code not in {"NOT_FOUND", "PROJECT_ARCHIVED"}:
                    raise
                # Preserve the durable artifact as pending. Archived/deleted
                # projects cannot accept new history; unarchiving permits recovery.
                continue
            job = db.get(GenerationJob, job_id, populate_existing=True)
            attempt = db.get(GenerationAttempt, attempt_id, populate_existing=True)
            if (
                job is None
                or job.project_id != project_id
                or job.task_type != "VIDEO"
                or job.target_type != "VIDEO"
                or attempt is None
                or attempt.job_id != job.id
                or attempt.status != "SUCCEEDED"
                or attempt.result_json is None
            ):
                continue
            if (
                db.scalar(
                    select(GenerationCandidate.id).where(
                        GenerationCandidate.attempt_id == attempt_id,
                        GenerationCandidate.ordinal == ordinal,
                    )
                )
                is not None
            ):
                continue
            try:
                require_project(db, project_id, job.created_by, write=True)
                if job.created_by != "service-owner":
                    user = db.get(User, UUID(job.created_by), populate_existing=True)
                    if user is None or user.disabled:
                        raise DomainError("FORBIDDEN", "任务创建者已不可执行", 403)
            except DomainError as error:
                if error.code not in {"NOT_FOUND", "FORBIDDEN", "PROJECT_ARCHIVED"}:
                    raise
                # Keep one immutable failed candidate instead of promoting bytes
                # after the original creator has lost production authority.
                error_code = error_code or error.code
            candidate_id, key = uuid4(), uuid5(attempt_id, f"video:{ordinal}")
            check = check_video_candidate_media(db, job, attempt, candidate_id, video, error_code)
            version = None
            if check.status == "PASSED" and video is not None and file is not None:
                version = append_video_candidate_version(
                    db,
                    settings,
                    job,
                    attempt,
                    video,
                    key,
                    "media-worker",
                    candidate_id=candidate_id,
                    source_file_id=file.id,
                    artifact_id=artifact_id,
                    media_notes={
                        "original_name": file.original_name,
                        "source_note": file.source_note,
                        "rights_note": file.rights_note,
                        "rights_confirmed": file.rights_confirmed,
                    },
                )
            row = GenerationCandidate(
                id=candidate_id,
                project_id=job.project_id,
                job_id=job.id,
                attempt_id=attempt.id,
                input_manifest_id=job.input_manifest_id,
                ordinal=ordinal,
                source="GENERATED",
                output_handle=attempt.result_json["output_handles"][ordinal],
                asset_version_id=version.id if version else None,
                creation_key=key,
                request_fingerprint=digest({"artifact_id": str(artifact_id)}),
                error_code=check.error_code,
                media_check_json=check.model_dump(mode="json"),
                created_by="media-worker",
            )
            db.add(row)
            db.flush()
            audit(
                db,
                job.project_id,
                "media-worker",
                "generation.video_candidate_registered",
                row.id,
                {
                    "artifact_id": str(artifact_id),
                    "asset_version_id": str(version.id) if version else None,
                    "error_code": check.error_code,
                },
            )
            imported += 1
    return imported
