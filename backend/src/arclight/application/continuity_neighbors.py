"""Resolve explicit production selections for immutable neighboring shot versions."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.continuity_selection import require_shot_review
from arclight.core.errors import DomainError
from arclight.domain.continuity_workflow import REVIEW_WORK_RESOURCE
from arclight.persistence.models import (
    AssetVersion,
    GenerationCandidate,
    VersionBinding,
    VisualAsset,
)


def selected_neighbor(db: Session, project_id: UUID, shot_id: UUID) -> dict | None:
    """Ambiguous, stale or unreviewed selections remain missing evidence, never PASS."""
    rows = list(
        db.scalars(
            select(AssetVersion)
            .join(VisualAsset, VisualAsset.current_version_id == AssetVersion.id)
            .join(GenerationCandidate, GenerationCandidate.asset_version_id == AssetVersion.id)
            .join(VersionBinding, VersionBinding.resource_id == GenerationCandidate.id)
            .where(
                VisualAsset.project_id == project_id,
                VisualAsset.status.not_in(["ARCHIVED", "STALE", "REJECTED"]),
                AssetVersion.status.in_(["APPROVED", "LOCKED"]),
                AssetVersion.stale.is_(False),
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == REVIEW_WORK_RESOURCE,
                VersionBinding.snapshot_json["continuity_work"]["target"]["shot_id"].as_string()
                == str(shot_id),
            )
            .limit(2)
        )
    )
    if len(rows) != 1:
        return None
    version = rows[0]
    try:
        record = require_shot_review(db, version)
    except DomainError:
        return None
    if record.review.target.shot_id != shot_id:
        return None
    return {"asset_id": str(version.asset_id), "asset_version_id": str(version.id)}
