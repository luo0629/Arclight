"""Add readable media facts to existing exact canvas versions; never infer completion."""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.generation_targets import candidate_episode_ids
from arclight.application.video_candidates import require_video_candidate_media_check
from arclight.core.errors import DomainError
from arclight.persistence.audio_production import AudioLineBinding, AudioSnapshot
from arclight.persistence.models import AssetVersion, Episode, GenerationCandidate, StoredFile
from arclight.persistence.render_manifests import RenderArtifact, RenderManifest
from arclight.persistence.rendering import RenderExecution
from arclight.persistence.timelines import Timeline

AUDIO_TYPES = {"audio/wav", "audio/mpeg", "audio/ogg", "audio/flac", "audio/mp4"}


def media_facts(
    db: Session, project_id: UUID, versions: Sequence[AssetVersion]
) -> dict[UUID, dict]:
    """Use relational registrations, exact checksums and an allowlist of public fields."""
    if not versions:
        return {}
    by_id = {row.id: row for row in versions}
    files: dict[UUID, StoredFile] = {}
    image_versions: set[UUID] = set()
    video_files: dict[UUID, StoredFile] = {}
    for stored in db.scalars(
        select(StoredFile).where(
            StoredFile.project_id == project_id,
            StoredFile.asset_version_id.in_(by_id),
        )
    ):
        version = by_id.get(stored.asset_version_id) if stored.asset_version_id else None
        if (
            version is not None
            and stored.storage_backend in {"LOCAL", "S3"}
            and stored.byte_size > 0
            and stored.checksum == version.checksum
            and stored.object_key == version.object_key
        ):
            if stored.rights_confirmed:
                files[version.id] = stored
            if stored.media_type.startswith("image/"):
                image_versions.add(version.id)
            if stored.media_type == "video/mp4":
                video_files[version.id] = stored
    facts: dict[UUID, dict] = {}
    for candidate in db.scalars(
        select(GenerationCandidate).where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.asset_version_id.in_(video_files),
        )
    ):
        candidate_version_id = candidate.asset_version_id
        if candidate_version_id is None:
            continue
        try:
            check = require_video_candidate_media_check(db, project_id, candidate.id)
        except DomainError:
            continue
        file = video_files.get(candidate_version_id)
        if (
            file is not None
            and check.video is not None
            and file.checksum == check.video.checksum
            and file.byte_size == check.video.byte_size
        ):
            facts[candidate_version_id] = {
                "preview_kind": "VIDEO",
                "candidate_id": str(candidate.id),
                "media_file_id": str(file.id),
                "media_type": file.media_type,
                "preview_path": (
                    f"/projects/{project_id}/generation-candidates/{candidate.id}/content"
                ),
                "duration_ms": check.video.duration_ms,
            }
    for version_id, episode_id in candidate_episode_ids(
        db, project_id, image_versions | facts.keys()
    ).items():
        facts.setdefault(version_id, {})["media_episode_id"] = str(episode_id)
    for version_id, audio_file in files.items():
        if audio_file.media_type in AUDIO_TYPES:
            facts[version_id] = {
                "preview_kind": "AUDIO",
                "media_file_id": str(audio_file.id),
                "media_type": audio_file.media_type,
            }
    for binding, snapshot in db.execute(
        select(AudioLineBinding, AudioSnapshot)
        .join(AudioSnapshot, AudioSnapshot.id == AudioLineBinding.snapshot_id)
        .join(Episode, Episode.id == AudioSnapshot.episode_id)
        .where(
            Episode.project_id == project_id,
            AudioLineBinding.asset_version_id.in_(by_id),
        )
    ):
        file = files.get(binding.asset_version_id)
        if file is not None and file.id == binding.file_id and file.media_type in AUDIO_TYPES:
            facts[binding.asset_version_id].update(
                duration_ms=binding.duration_ms,
                media_episode_id=str(snapshot.episode_id),
            )
    for timeline in db.scalars(
        select(Timeline)
        .join(Episode, Episode.id == Timeline.episode_id)
        .where(Episode.project_id == project_id, Timeline.id.in_(by_id), Timeline.sealed.is_(True))
    ):
        facts[timeline.id] = {
            "preview_kind": "TIMELINE",
            "media_episode_id": str(timeline.episode_id),
            "duration_ms": timeline.duration_ms,
        }
    for artifact, _manifest, timeline in db.execute(
        select(RenderArtifact, RenderManifest, Timeline)
        .join(RenderManifest, RenderManifest.id == RenderArtifact.manifest_id)
        .join(Timeline, Timeline.id == RenderManifest.timeline_id)
        .join(Episode, Episode.id == Timeline.episode_id)
        .join(RenderExecution, RenderExecution.id == RenderArtifact.job_id)
        .where(
            Episode.project_id == project_id,
            RenderManifest.project_id == project_id,
            RenderArtifact.asset_version_id.in_(by_id),
            RenderExecution.status == "SUCCEEDED",
        )
    ):
        file = files.get(artifact.asset_version_id)
        if (
            file is not None
            and file.id == artifact.file_id
            and file.media_type == "video/mp4"
            and file.checksum == artifact.checksum
            and file.byte_size == artifact.byte_size
        ):
            facts[artifact.asset_version_id] = {
                "preview_kind": "DELIVERY",
                "delivery_id": str(artifact.id),
                "media_episode_id": str(timeline.episode_id),
                "duration_ms": artifact.duration_ms,
            }
    return facts
