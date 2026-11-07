"""Transactional manual proposal revisions; domain writes only happen at approval."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_protection import compare_art_proposal
from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.production_events import begin_serialized
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import ExistingAssetTarget, VisualBibleConcept
from arclight.domain.art_review import ArtArchive, ArtSave
from arclight.domain.versions import digest
from arclight.persistence.art_proposals import ArtProposalRecord, ArtProposalVersion
from arclight.persistence.models import VisualAsset


def asset_content(row: VisualAsset) -> dict:
    """Keep unmodified historical keys and the exact media selection in design history."""
    return {
        "asset_id": str(row.id),
        "asset_type": row.asset_type,
        "name": row.name,
        "revision": row.revision,
        "status": row.status,
        "current_version_id": str(row.current_version_id) if row.current_version_id else None,
        "lock_version": row.lock_version,
        "metadata": row.metadata_json,
        "character_profile": row.character_profile,
        "scene_profile": row.scene_profile,
        "prop_profile": row.prop_profile,
    }


def require_proposal(
    db: Session, project_id: UUID, proposal_id: UUID, actor: str, *, write: bool = False
) -> ArtProposalRecord:
    if write:
        begin_serialized(db)
        from arclight.application.model_gateway import lock_gate

        lock_gate(db)
    require_project(db, project_id, actor, write=write)
    row = db.get(ArtProposalRecord, proposal_id, populate_existing=True)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "美术提案不存在", 404)
    return row


def current_version(db: Session, row: ArtProposalRecord) -> ArtProposalVersion:
    version = db.scalar(
        select(ArtProposalVersion).where(
            ArtProposalVersion.proposal_id == row.id,
            ArtProposalVersion.version_no == row.version_no,
        )
    )
    if version is None:
        raise DomainError("ART_VERSION_INVALID", "提案版本记录不完整", 409)
    return version


def check_revision(row: ArtProposalRecord, expected: int) -> None:
    if row.revision != expected:
        raise DomainError("VERSION_CONFLICT", "提案已变化，请保留草稿并重新载入", 409)
    if row.archived:
        raise DomainError("ART_PROPOSAL_ARCHIVED", "归档提案只允许查看", 409)


def save_proposal(
    db: Session,
    project_id: UUID,
    actor: str,
    payload: ArtSave,
    *,
    proposal_id: UUID | None = None,
) -> tuple[ArtProposalRecord, ArtProposalVersion, bool]:
    payload = ArtSave.model_validate(payload.model_dump(mode="json"))
    begin_serialized(db)
    from arclight.application.model_gateway import lock_gate

    lock_gate(db)
    project = require_project(db, project_id, actor, write=True)
    if payload.proposal.project_id != project_id:
        raise DomainError("ART_SCOPE_INVALID", "提案项目与请求路径不一致", 422)
    fingerprint = digest(payload.model_dump(mode="json"))
    if proposal_id is None:
        if payload.expected_revision != 0:
            raise DomainError("VERSION_CONFLICT", "新提案基准修订须为零", 409)
        proposal_id = uuid5(project_id, "art-proposal:" + str(payload.creation_key))
        row = db.get(ArtProposalRecord, proposal_id, populate_existing=True)
        if row is not None:
            if row.request_sha256 != fingerprint or row.created_by != actor:
                raise DomainError("IDEMPOTENCY_CONFLICT", "相同创建键不能更换提案或作者", 409)
            version = db.scalar(
                select(ArtProposalVersion).where(
                    ArtProposalVersion.proposal_id == row.id, ArtProposalVersion.version_no == 1
                )
            )
            assert version is not None
            return row, version, False
        row = ArtProposalRecord(
            id=proposal_id,
            project_id=project_id,
            creation_key=payload.creation_key,
            request_sha256=fingerprint,
            created_by=actor,
            version_no=1,
            revision=0,
            archived=False,
        )
    else:
        row = require_proposal(db, project_id, proposal_id, actor, write=True)
        prior = db.scalar(
            select(ArtProposalVersion).where(
                ArtProposalVersion.proposal_id == row.id,
                ArtProposalVersion.creation_key == payload.creation_key,
            )
        )
        if prior is not None:
            if prior.request_sha256 != fingerprint or prior.created_by != actor:
                raise DomainError("IDEMPOTENCY_CONFLICT", "相同保存键不能更换修订或作者", 409)
            return row, prior, False
        check_revision(row, payload.expected_revision)
    comparison = compare_art_proposal(db, payload.proposal, actor)
    baseline = {}
    for option in payload.proposal.options:
        for design in option.designs:
            if isinstance(design, VisualBibleConcept):
                value = snapshot(db, project)
            elif isinstance(design.target, ExistingAssetTarget):
                asset = db.get(VisualAsset, design.target.asset_id, populate_existing=True)
                assert asset is not None
                value = asset_content(asset)
            else:
                value = None
            baseline[str(design.design_id)] = value
    if row not in db:
        db.add(row)
        db.flush()
    else:
        row.version_no += 1
        row.revision += 1
    content = payload.proposal.model_dump(mode="json")
    version = ArtProposalVersion(
        id=uuid5(row.id, "art-version:" + str(payload.creation_key)),
        proposal_id=row.id,
        version_no=row.version_no,
        creation_key=payload.creation_key,
        request_sha256=fingerprint,
        proposal_json=content,
        comparison_json=comparison.model_dump(mode="json"),
        baseline_json=baseline,
        input_sha256=digest(
            {
                "proposal": content,
                "comparison": comparison.model_dump(mode="json"),
                "baseline": baseline,
            }
        ),
        created_by=actor,
    )
    db.add(version)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "art_proposal.saved",
        row.id,
        {"version_id": str(version.id), "version_no": version.version_no},
    )
    return row, version, True


def archive_proposal(
    db: Session, project_id: UUID, proposal_id: UUID, actor: str, payload: ArtArchive
) -> ArtProposalRecord:
    row = require_proposal(db, project_id, proposal_id, actor, write=True)
    if row.revision != payload.expected_revision:
        raise DomainError("VERSION_CONFLICT", "提案修订已变化", 409)
    if row.archived != payload.archived:
        row.archived = payload.archived
        row.revision += 1
        db.flush()
        audit(
            db,
            project_id,
            actor,
            "art_proposal.archived" if row.archived else "art_proposal.restored",
            row.id,
            {"revision": row.revision},
        )
    return row
