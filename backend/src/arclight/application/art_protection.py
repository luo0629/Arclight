"""Compare and recheck art suggestions under the existing project transaction gate."""

import json
import re
from collections.abc import Iterator
from typing import Literal

from pydantic import JsonValue
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.art_proposals import validate_art_proposal
from arclight.application.commands import require_project
from arclight.application.visual_bible import snapshot
from arclight.core.errors import DomainError
from arclight.domain.art_proposals import (
    ArtConcept,
    ArtProposal,
    ExistingAssetTarget,
    VisualBibleConcept,
)
from arclight.domain.art_protection import (
    ArtComparison,
    ArtConfirmation,
    ArtFieldChange,
    ArtOptionComparison,
)
from arclight.domain.asset_metadata import AssetMetadata
from arclight.domain.asset_profiles import CharacterProfile, PropProfile, SceneProfile
from arclight.domain.versions import digest
from arclight.persistence.models import AssetSelectionLock, AssetVersion, Project, VisualAsset

Protection = Literal["NONE", "APPROVED", "LOCKED"]


def _with_defaults(
    defaults: dict[str, JsonValue], stored: dict[str, JsonValue]
) -> dict[str, JsonValue]:
    """Fill absent historical defaults without normalizing or dropping stored content."""
    result = dict(defaults)
    for key, value in stored.items():
        previous = result.get(key)
        result[key] = (
            _with_defaults(previous, value)
            if isinstance(previous, dict) and isinstance(value, dict)
            else value
        )
    return result


def _differences(
    before: JsonValue, proposed: JsonValue, path: str
) -> Iterator[tuple[str, JsonValue, JsonValue]]:
    if before == proposed:
        return
    if (
        isinstance(before, dict)
        and isinstance(proposed, dict)
        and path.count(".") < 3
        and all(
            re.fullmatch(r"[a-z][a-z0-9_]*", key)
            and len(path + "." + key) <= 160
            # Missing and explicit null differ; show the parent to retain that distinction.
            and (key in before and key in proposed or before.get(key) != proposed.get(key))
            for key in before.keys() | proposed.keys()
        )
    ):
        for key in sorted(before.keys() | proposed.keys()):
            yield from _differences(before.get(key), proposed.get(key), path + "." + key)
    else:
        # Lists are one field: reference/outfit identity changes cannot hide in indices.
        yield path, before, proposed


def _asset_protection(db: Session, asset: VisualAsset) -> Protection:
    lock = db.scalar(
        select(AssetSelectionLock)
        .where(AssetSelectionLock.asset_id == asset.id)
        .execution_options(populate_existing=True)
    )
    version = (
        db.get(AssetVersion, asset.current_version_id, populate_existing=True)
        if asset.current_version_id
        else None
    )
    if asset.current_version_id is not None and (version is None or version.asset_id != asset.id):
        raise DomainError("ART_LOCK_STATE_INVALID", "资产选用与版本身份不一致", 409)
    if asset.status == "LOCKED" or lock is not None or (version and version.status == "LOCKED"):
        if (
            asset.status != "LOCKED"
            or lock is None
            or version is None
            or lock.version_id != version.id
            or version.status != "LOCKED"
            or version.stale
        ):
            raise DomainError("ART_LOCK_STATE_INVALID", "资产锁状态不完整，须先核验原选用锁", 409)
        return "LOCKED"
    if asset.status == "APPROVED" or (version and version.status == "APPROVED"):
        return "APPROVED"
    return "NONE"


def _asset_content(asset: VisualAsset, design: ArtConcept) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {
        "name": asset.name,
        "metadata": _with_defaults(AssetMetadata().model_dump(mode="json"), asset.metadata_json),
    }
    profile_defaults = {
        "CHARACTER": CharacterProfile,
        "SCENE": SceneProfile,
        "PROP": PropProfile,
    }
    if design.kind in profile_defaults:
        stored = getattr(asset, design.kind.lower() + "_profile")
        result["profile"] = (
            _with_defaults(profile_defaults[design.kind]().model_dump(mode="json"), stored)
            if stored is not None
            else None
        )
    return result


def _design_changes(db: Session, project: Project, design: ArtConcept) -> list[ArtFieldChange]:
    if isinstance(design, VisualBibleConcept):
        current = snapshot(db, project)
        return [
            ArtFieldChange(
                design_id=design.design_id,
                target_type=design.kind,
                target_id=design.version_id,
                field_path="fields." + name,
                before=current["fields"][name],
                proposed=value,
                protection="LOCKED" if name in current["locked_fields"] else "NONE",
            )
            for name, value in sorted(design.fields.model_dump(exclude_unset=True).items())
            if current["fields"][name] != value
        ]
    proposed: dict[str, JsonValue] = {"name": design.name}
    if design.metadata is not None:
        proposed["metadata"] = design.metadata.model_dump(mode="json")
    profile = getattr(design, "profile", None)
    if profile is not None:
        proposed["profile"] = profile.model_dump(mode="json")
    if not isinstance(design.target, ExistingAssetTarget):
        return [
            ArtFieldChange(
                design_id=design.design_id,
                target_type=design.kind,
                target_id=None,
                field_path="asset",
                before=None,
                proposed=proposed,
                protection="NONE",
            )
        ]
    asset = db.get(VisualAsset, design.target.asset_id, populate_existing=True)
    assert asset is not None  # validate_art_proposal ran under the project write gate.
    protection = _asset_protection(db, asset)
    current_content = _asset_content(asset, design)
    return [
        ArtFieldChange(
            design_id=design.design_id,
            target_type=design.kind,
            target_id=asset.id,
            field_path=path,
            before=before,
            proposed=after,
            protection=protection,
        )
        for root, value in sorted(proposed.items())
        for path, before, after in _differences(current_content.get(root), value, root)
    ]


def compare_art_proposal(db: Session, proposal: ArtProposal, actor: str) -> ArtComparison:
    """Editor preflight; the existing project gate gives a coherent, fresh baseline.

    The caller owns the transaction. No proposal, approval, asset or audit is saved.
    """
    proposal = ArtProposal.model_validate(proposal.model_dump(mode="json"))
    project = require_project(db, proposal.project_id, actor, write=True)
    validate_art_proposal(db, proposal, actor)
    options = []
    for option in proposal.options:
        changes = [
            change for design in option.designs for change in _design_changes(db, project, design)
        ]
        if len(changes) > 512:
            raise DomainError("ART_COMPARISON_TOO_LARGE", "比较字段过多，请缩小提案范围", 422)
        options.append(ArtOptionComparison(option_id=option.option_id, changes=changes))
    content = {
        "schema_version": "art-comparison/1",
        "project_id": str(project.id),
        "proposal_sha256": digest(proposal.model_dump(mode="json")),
        "options": [option.model_dump(mode="json") for option in options],
        "unresolved_question_ids": [str(question.question_id) for question in proposal.questions],
        "requires_human_review": True,
    }
    if len(json.dumps(content, ensure_ascii=False).encode()) > 262144:
        raise DomainError("ART_COMPARISON_TOO_LARGE", "比较内容过大，请缩小提案范围", 422)
    return ArtComparison.model_validate({**content, "comparison_sha256": digest(content)})


def require_art_confirmation(
    db: Session,
    proposal: ArtProposal,
    actor: str,
    confirmation: ArtConfirmation,
) -> ArtOptionComparison:
    """Recheck Owner acknowledgement in the caller's eventual approval transaction.

    This does not approve, unlock or write anything. ART-04 must retain the same
    transaction and use the existing version/lock services when persisting a decision.
    Never treat a successful return as a reusable permission after transaction end.
    """
    proposal = ArtProposal.model_validate(proposal.model_dump(mode="json"))
    confirmation = ArtConfirmation.model_validate(confirmation.model_dump(mode="json"))
    require_project(db, proposal.project_id, actor, write=True, owner=True)
    comparison = compare_art_proposal(db, proposal, actor)
    if comparison.comparison_sha256 != confirmation.comparison_sha256:
        raise DomainError("ART_COMPARISON_STALE", "提案或比较范围已变化，请重新查看并确认", 409)
    if comparison.unresolved_question_ids:
        raise DomainError("ART_QUESTIONS_UNRESOLVED", "提案仍有未决问题，不能确认正式变更", 409)
    option = next(
        (row for row in comparison.options if row.option_id == confirmation.option_id), None
    )
    if option is None:
        raise DomainError("ART_OPTION_INVALID", "确认方案不属于当前提案", 422)
    if not option.changes:
        raise DomainError("NO_ART_CHANGE", "所选方案没有内容变化", 422)
    expected = {(change.design_id, change.field_path) for change in option.changes}
    actual = {(change.design_id, change.field_path) for change in confirmation.confirmed_changes}
    if actual != expected:
        raise DomainError("ART_CONFIRMATION_REQUIRED", "请明确确认所选方案的全部变更字段", 409)
    return option
