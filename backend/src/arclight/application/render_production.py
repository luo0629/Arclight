"""Project the existing formal report into the shared production evidence model."""

from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.render_reports import delivery_acceptance, read_machine_report
from arclight.core.errors import DomainError
from arclight.domain.enums import AssetStatus
from arclight.domain.production_state import ReviewEvidence, VersionEvidence, WarningAcceptance
from arclight.persistence.models import AssetVersion, Episode, RenderExecution
from arclight.persistence.render_manifests import RenderArtifact, RenderManifest, RenderReportLink


@dataclass(frozen=True)
class RenderProductionEvidence:
    render: VersionEvidence | None = None
    validation: ReviewEvidence | None = None
    review: ReviewEvidence | None = None
    warnings: list[str] = field(default_factory=list)
    acceptance: WarningAcceptance | None = None


def render_evidence(
    db: Session, episode: Episode, timeline: VersionEvidence | None, actor: str
) -> RenderProductionEvidence:
    """The newest formal result for the current timeline wins; previews grant nothing."""
    if timeline is None:
        return RenderProductionEvidence()
    artifact = db.scalar(
        select(RenderArtifact)
        .join(RenderManifest, RenderManifest.id == RenderArtifact.manifest_id)
        .join(RenderExecution, RenderExecution.id == RenderArtifact.job_id)
        .join(RenderReportLink, RenderReportLink.artifact_id == RenderArtifact.id)
        .where(
            RenderManifest.project_id == episode.project_id,
            RenderManifest.timeline_id == timeline.version_id,
            RenderExecution.status == "SUCCEEDED",
            RenderReportLink.inspection_id.is_not(None),
        )
        .order_by(RenderArtifact.created_at.desc(), RenderArtifact.id.desc())
        .limit(1)
    )
    if artifact is None:
        return RenderProductionEvidence()
    version = db.get(AssetVersion, artifact.asset_version_id, populate_existing=True)
    try:
        report = read_machine_report(db, artifact, verify_object=False)
    except DomainError:
        # Invalid durable proof cannot complete the current production state.
        return RenderProductionEvidence()
    if version is None or report is None:
        return RenderProductionEvidence()
    render = VersionEvidence(
        project_id=episode.project_id,
        episode_id=episode.id,
        version_id=version.id,
        kind="RENDER",
        status=AssetStatus(version.status),
        stale=version.stale,
        source_version_ids=sorted(
            {report.timeline_id, *(item.asset_version_id for item in report.sources)}
        ),
    )
    warnings: list[str] = sorted({item.code for item in report.warnings})
    try:
        read_machine_report(db, artifact, current=True, verify_object=False)
    except DomainError:
        # Retain historical evidence, but an obsolete export cannot complete production.
        return RenderProductionEvidence(render=render, warnings=warnings)
    link = db.scalar(select(RenderReportLink).where(RenderReportLink.artifact_id == artifact.id))
    assert link is not None
    validation = ReviewEvidence(
        project_id=episode.project_id,
        episode_id=episode.id,
        review_id=link.id,
        version_id=version.id,
        source_version_id=timeline.version_id,
        actor_id="arclight-delivery-v1",
        decision="PASS",
    )
    accepted = delivery_acceptance(db, artifact, actor)
    review = None
    acceptance = None
    if accepted.is_delivery:
        review = ReviewEvidence(
            project_id=episode.project_id,
            episode_id=episode.id,
            review_id=accepted.review_id or link.id,
            version_id=version.id,
            source_version_id=timeline.version_id,
            actor_id=accepted.reviewed_by_user_id or "arclight-delivery-v1",
            decision="APPROVE",
        )
        if warnings:
            acceptance = WarningAcceptance(**review.model_dump(), warnings=warnings)
    return RenderProductionEvidence(render, validation, review, warnings, acceptance)
