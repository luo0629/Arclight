"""Narrow candidate reading and exact ART-04 approval, without changing selection or locks."""

from copy import deepcopy
from uuid import UUID, uuid5

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.art_concept_jobs import concept_origin
from arclight.application.audit import audit
from arclight.application.context_sources import asset_sources, verify_images
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.art_protection import ArtFieldChange
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtApprovedVersion, ArtProposalVersion
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_candidates import GenerationCandidate
from arclight.persistence.models import AssetVersion, StoredFile, VisualAsset


def concept_candidate(db: Session, run_id: UUID | None, version_id: UUID):
    version = db.get(AssetVersion, version_id, populate_existing=True)
    candidate = db.scalar(
        select(GenerationCandidate).where(
            GenerationCandidate.asset_version_id == version_id,
            GenerationCandidate.source == "GENERATED",
        )
    )
    job = db.get(GenerationJob, candidate.job_id) if candidate else None
    origin = concept_origin(db, job) if job else None
    if origin is None or (run_id is not None and origin["run_id"] != str(run_id)):
        raise DomainError("ART_CONCEPT_REFERENCE_INVALID", "图片不是当前受控概念图候选", 422)
    asset = db.get(VisualAsset, version.asset_id, populate_existing=True) if version else None
    if (
        version is None
        or asset is None
        or job is None
        or asset.project_id != job.project_id
        or asset.asset_type != job.target_type
        or asset.status in {"ARCHIVED", "REJECTED", "STALE"}
        or version.stale
        or version.status not in {"DRAFT", "READY_FOR_REVIEW", "APPROVED", "LOCKED"}
        or version.metadata_json.get("required_review") != "ART_PROPOSAL"
    ):
        raise DomainError("ART_CONCEPT_REFERENCE_INVALID", "概念图候选已失效或不符合媒体门禁", 409)
    scope = asset.metadata_json.get("usage_scope", {})
    if scope.get("kind", "PROJECT") != "PROJECT" and str(job.episode_id) not in scope.get(
        "episode_ids", []
    ):
        raise DomainError("CONTEXT_SCOPE_NOT_ALLOWED", "概念图不在当前剧集使用范围", 403)
    return version, asset, origin


def approved_concept_reference(db: Session, version: AssetVersion) -> bool:
    return any(
        row.content_json.get("kind") == "CONCEPT_REFERENCE"
        and row.content_json.get("asset_version_id") == str(version.id)
        and row.content_json.get("checksum") == version.checksum
        and row.content_json.get("rights_confirmed") is True
        for row in db.scalars(
            select(ArtApprovedVersion).where(ArtApprovedVersion.asset_id == version.asset_id)
        )
    )


def read_concept_reference(db: Session, settings: Settings, run_id: UUID, version_id: UUID) -> dict:
    version, asset, origin = concept_candidate(db, run_id, version_id)
    file = db.scalar(select(StoredFile).where(StoredFile.asset_version_id == version.id))
    if file is None or file.project_id != asset.project_id or file.checksum != version.checksum:
        raise DomainError("ART_CONCEPT_REFERENCE_INVALID", "概念图缺少验证后的原图", 409)
    snapshot = {
        "asset_id": str(asset.id),
        "asset_version_id": str(version.id),
        "version_no": version.version_no,
        "checksum": version.checksum,
        "asset_type": asset.asset_type,
        "name": asset.name,
        "directory_revision": asset.revision,
        "locked": asset.status == "LOCKED",
        "purpose": "CONCEPT_COMPARISON",
        "profile": {},
    }
    sources = asset_sources(db, asset.project_id, [snapshot])
    verify_images(db, settings, asset.project_id, sources)
    image = sources[0]["object_ref"]
    if image is None:
        raise DomainError("ART_CONCEPT_REFERENCE_INVALID", "概念图原图不可读取", 409)
    return {
        **snapshot,
        "metadata_sha256": digest(version.metadata_json),
        "project_access": "AUTHORIZED",
        "usage_scope": deepcopy(asset.metadata_json.get("usage_scope", {"kind": "PROJECT"})),
        "rights_status": "CONFIRMED"
        if approved_concept_reference(db, version)
        else "PENDING_ART_APPROVAL",
        "rights_note": file.rights_note,
        "source_note": file.source_note,
        "provenance": origin,
        "asset": snapshot,
        "image": image,
    }


def reference_changes(db: Session, proposal, option) -> list[ArtFieldChange]:
    result = []
    for reference in proposal.references:
        if reference.reference_id not in option.reference_ids or reference.usage != "COMPARISON":
            continue
        row = db.get(AssetVersion, reference.asset.asset_version_id)
        if row is None or row.metadata_json.get("required_review") != "ART_PROPOSAL":
            continue
        version, asset, origin = concept_candidate(db, None, row.id)
        if approved_concept_reference(db, version):
            continue
        if asset.project_id != proposal.project_id or reference.asset.asset_id != asset.id:
            raise DomainError("ART_CONCEPT_REFERENCE_INVALID", "概念图参考作用域不匹配", 422)
        if asset.status == "LOCKED" or version.status == "LOCKED":
            raise DomainError("ART_CONCEPT_REFERENCE_LOCKED", "锁定候选不能更改参考批准状态", 409)
        result.append(
            ArtFieldChange(
                design_id=reference.reference_id,
                target_type=asset.asset_type,
                target_id=asset.id,
                field_path="reference.approval_and_rights",
                before={
                    "asset_version_id": str(version.id),
                    "checksum": version.checksum,
                    "status": version.status,
                    "asset_revision": asset.revision,
                    "lock_version": asset.lock_version,
                    "current_version_id": str(asset.current_version_id)
                    if asset.current_version_id
                    else None,
                    "rights_confirmed": False,
                    "origin": origin,
                },
                proposed={
                    "asset_version_id": str(version.id),
                    "checksum": version.checksum,
                    "status": "APPROVED",
                    "rights_confirmed": True,
                },
                protection="NONE",
            )
        )
    return result


def approve_references(
    db: Session, proposal_version: ArtProposalVersion, proposal, option, actor: str
):
    for change in reference_changes(db, proposal, option):
        reference = next(
            item for item in proposal.references if item.reference_id == change.design_id
        )
        version, asset, _ = concept_candidate(db, None, reference.asset.asset_version_id)
        version.status = "APPROVED"
        version.revision += 1
        assert isinstance(change.proposed, dict)
        content = {"kind": "CONCEPT_REFERENCE", **change.proposed}
        row = ArtApprovedVersion(
            id=uuid5(proposal_version.id, "art-reference:" + str(change.design_id)),
            proposal_version_id=proposal_version.id,
            design_id=change.design_id,
            target_type=asset.asset_type,
            asset_id=asset.id,
            visual_bible_version_id=None,
            version_no=(
                db.scalar(
                    select(func.max(ArtApprovedVersion.version_no)).where(
                        ArtApprovedVersion.asset_id == asset.id
                    )
                )
                or 0
            )
            + 1,
            before_json=change.before,
            content_json=content,
            content_sha256=digest(content),
            created_by=actor,
        )
        db.add(row)
        db.flush()
        audit(
            db,
            asset.project_id,
            actor,
            "art_reference.approved",
            version.id,
            {
                "proposal_version_id": str(proposal_version.id),
                "option_id": str(option.option_id),
                "reference_id": str(change.design_id),
                "rights_confirmed": True,
            },
        )
