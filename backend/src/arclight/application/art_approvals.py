"""Apply a confirmed option inside the unified review/resume transaction."""

from uuid import uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.art_proposal_store import asset_content
from arclight.application.art_protection import require_art_confirmation
from arclight.application.audit import audit
from arclight.application.visual_bible import append_version, snapshot
from arclight.domain.art_proposals import ArtProposal, ExistingAssetTarget, VisualBibleConcept
from arclight.domain.art_protection import ArtConfirmation
from arclight.domain.asset_metadata import AssetMetadata
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.models import Project, VisualAsset


def apply_option(
    db: Session, version: ArtProposalVersion, confirmation: ArtConfirmation, actor: str
) -> None:
    proposal = ArtProposal.model_validate(version.proposal_json)
    require_art_confirmation(db, proposal, actor, confirmation)
    option = next(item for item in proposal.options if item.option_id == confirmation.option_id)
    from arclight.application.art_concept_assets import approve_references

    approve_references(db, version, proposal, option, actor)
    changed_designs = {change.design_id for change in confirmation.confirmed_changes}
    project = db.get(Project, proposal.project_id, populate_existing=True)
    assert project is not None
    for design in option.designs:
        if design.design_id not in changed_designs:
            continue
        key = uuid5(version.id, "art-approved:" + str(design.design_id))
        before = version.baseline_json[str(design.design_id)]
        if isinstance(design, VisualBibleConcept):
            patch = design.fields.model_dump(exclude_unset=True)
            bible, _ = append_version(
                db,
                project,
                actor,
                design.version_no,
                key,
                patch=patch,
                confirmed_locked_fields=frozenset(patch),
            )
            after = snapshot(db, project)
            row = ArtApprovedVersion(
                id=key,
                proposal_version_id=version.id,
                design_id=design.design_id,
                target_type=design.kind,
                asset_id=None,
                visual_bible_version_id=bible.id,
                version_no=bible.version_no,
                before_json=before,
                content_json=after,
                content_sha256=digest(after),
                created_by=actor,
            )
        else:
            if isinstance(design.target, ExistingAssetTarget):
                asset = db.get(VisualAsset, design.target.asset_id, populate_existing=True)
                assert asset is not None
                asset.revision += 1
            else:
                asset = VisualAsset(
                    id=uuid5(version.id, "art-asset:" + str(design.design_id)),
                    project_id=project.id,
                    asset_type=design.kind,
                    name=design.name,
                    metadata_json=AssetMetadata().model_dump(mode="json"),
                    creation_source={"kind": "MANUAL", "note": "经人工审核的美术提案"},
                    created_by=actor,
                    revision=0,
                    lock_version=0,
                    status="DRAFT",
                )
                db.add(asset)
            asset.name = design.name
            if design.metadata is not None:
                asset.metadata_json = design.metadata.model_dump(mode="json")
            profile = getattr(design, "profile", None)
            if profile is not None:
                setattr(asset, design.kind.lower() + "_profile", profile.model_dump(mode="json"))
            db.flush()
            after = asset_content(asset)
            row = ArtApprovedVersion(
                id=key,
                proposal_version_id=version.id,
                design_id=design.design_id,
                target_type=design.kind,
                asset_id=asset.id,
                visual_bible_version_id=None,
                version_no=(
                    db.scalar(
                        select(func.count())
                        .select_from(ArtApprovedVersion)
                        .where(ArtApprovedVersion.asset_id == asset.id)
                    )
                    or 0
                )
                + 1,
                before_json=before,
                content_json=after,
                content_sha256=digest(after),
                created_by=actor,
            )
        db.add(row)
        db.flush()
        audit(
            db,
            project.id,
            actor,
            "art_proposal.version_approved",
            row.id,
            {
                "proposal_version_id": str(version.id),
                "design_id": str(design.design_id),
                "target_type": design.kind,
            },
        )
