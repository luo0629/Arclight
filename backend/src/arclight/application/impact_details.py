"""Describe affected episodes, shots and saved reviews without changing their history."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.persistence.models import (
    AssetVersion,
    AudioSnapshot,
    DependencyManifest,
    Episode,
    GenerationCandidate,
    GenerationJob,
    HumanReviewRequest,
    Shot,
    VersionBinding,
    VisualAsset,
)


def _id(value: object) -> UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return UUID(value)
    except ValueError:
        return None


def describe_impacts(db: Session, project_id: UUID, items: list[dict]) -> None:
    if not items:
        return
    by_id = {item["asset_version_id"]: item for item in items}
    ids = [UUID(identity) for identity in by_id]
    episodes = {
        str(row.id): row.episode_no
        for row in db.scalars(select(Episode).where(Episode.project_id == project_id))
    }

    def location(item: dict, episode_id: object) -> None:
        if (key := str(episode_id)) in episodes:
            item.update(episode_id=key, episode_no=episodes[key])

    for version, asset in db.execute(
        select(AssetVersion, VisualAsset)
        .join(VisualAsset, VisualAsset.id == AssetVersion.asset_id)
        .where(AssetVersion.id.in_(ids))
    ):
        item = by_id[str(version.id)]
        location(
            item, version.metadata_json.get("episode_id") or asset.metadata_json.get("episode_id")
        )
        if asset.asset_type == "AUDIO" and "episode_id" not in item:
            snapshot_id = _id(
                version.metadata_json.get("audio_snapshot_id")
                or asset.metadata_json.get("audio_snapshot_id")
            )
            snapshot = db.get(AudioSnapshot, snapshot_id) if snapshot_id is not None else None
            if snapshot is not None:
                location(item, snapshot.episode_id)

    for version_id, episode_id, snapshot in db.execute(
        select(
            GenerationCandidate.asset_version_id,
            GenerationJob.episode_id,
            DependencyManifest.snapshot_json,
        )
        .join(GenerationJob, GenerationJob.id == GenerationCandidate.job_id)
        .join(DependencyManifest, DependencyManifest.id == GenerationCandidate.input_manifest_id)
        .where(
            GenerationCandidate.project_id == project_id,
            GenerationCandidate.asset_version_id.in_(ids),
        )
    ):
        item = by_id[str(version_id)]
        location(item, episode_id)
        context = snapshot["context"]
        shot_id = _id((context.get("continuity_review") or {}).get("shot_id"))
        if shot_id is not None:
            item["shot_id"] = str(shot_id)

    for shot in db.scalars(
        select(Shot).where(Shot.storyboard_id.in_(ids)).order_by(Shot.storyboard_id, Shot.shot_no)
    ):
        by_id[str(shot.storyboard_id)].setdefault("shots", []).append(
            {"id": str(shot.id), "shot_no": shot.shot_no, "status": shot.status}
        )

    for row in db.scalars(
        select(HumanReviewRequest)
        .where(
            HumanReviewRequest.project_id == project_id,
            HumanReviewRequest.snapshot_json["version_id"].as_string().in_(list(by_id)),
        )
        .order_by(HumanReviewRequest.id)
    ):
        item = by_id[row.snapshot_json["version_id"]]
        location(item, row.episode_id)
        item.setdefault("reviews", []).append({"id": str(row.id), "kind": row.handler})

    for resource, path in (
        ("VIDEO_TEMPORAL_REVIEW", ("video_review", "report")),
        ("REVIEW_RESULT", ("review_contract", "review")),
    ):
        for binding in db.scalars(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == resource,
                VersionBinding.snapshot_json[path + ("target", "asset_version_id")]
                .as_string()
                .in_(list(by_id)),
            )
            .order_by(VersionBinding.resource_id)
        ):
            report = binding.snapshot_json[path[0]][path[1]]
            item = by_id[report["target"]["asset_version_id"]]
            location(item, report["episode_id"])
            item["shot_id"] = report["target"]["shot_id"]
            item.setdefault("reviews", []).append(
                {"id": str(binding.resource_id), "kind": resource}
            )
