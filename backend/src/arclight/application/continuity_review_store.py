"""Append review contract artifacts using GEN-11's immutable version-binding store."""

from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.continuity_reviews import validate_review_contract
from arclight.application.model_gateway import lock_gate
from arclight.application.production_events import begin_serialized
from arclight.application.production_versions import bind, checked_snapshot, find_binding
from arclight.core.errors import DomainError
from arclight.domain.continuity_reviews import (
    ContinuityReview,
    ReviewContractRecord,
    review_input_sha256,
)
from arclight.domain.enums import RunStatus
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import WorkflowResult
from arclight.persistence.models import VersionBinding

REVIEW_RESOURCE = "REVIEW_RESULT"


def _record(db: Session, binding: VersionBinding) -> ReviewContractRecord:
    try:
        snapshot = checked_snapshot(db, binding)
        record = ReviewContractRecord.model_validate(snapshot["review_contract"])
        if (
            record.review.project_id != binding.project_id
            or record.review.result_id != binding.resource_id
            or snapshot["components"]
            != [entry.model_dump(mode="json") for entry in record.review.versions]
        ):
            raise ValueError("Review artifact identity or versions do not match its receipt")
        return record
    except (ValidationError, KeyError, TypeError, ValueError) as exc:
        raise DomainError("REVIEW_RECORD_INVALID", "审查合同记录格式或身份不匹配", 409) from exc


def require_review_record(
    db: Session, project_id: UUID, result_id: UUID, actor: str
) -> ReviewContractRecord:
    require_project(db, project_id, actor)
    row = find_binding(db, project_id, REVIEW_RESOURCE, result_id)
    if row is None:
        raise DomainError("NOT_FOUND", "审查合同记录不存在", 404)
    return _record(db, row)


def save_review_contract(
    db: Session, review: ContinuityReview, actor: str
) -> tuple[ReviewContractRecord, bool]:
    """Save an advisory contract, without granting media status or publishing review events."""
    review = ContinuityReview.model_validate(review.model_dump(mode="json"))
    begin_serialized(db)
    lock_gate(db)
    require_project(db, review.project_id, actor, write=True)
    existing = find_binding(db, review.project_id, REVIEW_RESOURCE, review.result_id)
    fingerprint = digest(review.model_dump(mode="json"))
    if existing is not None:
        record = require_review_record(db, review.project_id, review.result_id, actor)
        if record.review_sha256 != fingerprint or record.recorded_by != actor:
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同审查结果ID不能更换内容或作者", 409)
        return record, False
    validate_review_contract(db, review, actor)
    prior_row = db.scalar(
        select(VersionBinding)
        .where(
            VersionBinding.project_id == review.project_id,
            VersionBinding.resource_type == REVIEW_RESOURCE,
            VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                "candidate_id"
            ].as_string()
            == str(review.target.candidate_id),
            VersionBinding.snapshot_json["review_contract"]["review"]["target"][
                "shot_id"
            ].as_string()
            == str(review.target.shot_id),
        )
        .order_by(VersionBinding.snapshot_json["review_contract"]["sequence"].as_integer().desc())
        .limit(1)
    )
    previous = _record(db, prior_row) if prior_row is not None else None
    if previous is None:
        if review.rerun is not None:
            raise DomainError("REVIEW_RERUN_INVALID", "首次审查不能引用不存在的历史", 409)
        sequence = 1
    else:
        rerun = review.rerun
        if (
            rerun is None
            or rerun.previous_review_id != previous.review.result_id
            or rerun.previous_sha256 != digest(previous.model_dump(mode="json"))
            or previous.review.target != review.target
            or previous.review.episode_id != review.episode_id
        ):
            raise DomainError(
                "REVIEW_RERUN_CONFLICT", "重跑须引用同一精确候选和镜头的最新审查记录", 409
            )
        sequence = previous.sequence + 1
    result = WorkflowResult(
        result_id=review.result_id,
        project_id=review.project_id,
        episode_id=review.episode_id,
        agent_name="continuity_reviewer",
        status=RunStatus.SUCCEEDED,
        summary=review.summary,
        artifact_ids=[review.result_id],
        warnings=[],
        requires_human_review=True,
    )
    record = ReviewContractRecord(
        review=review,
        input_sha256=review_input_sha256(review),
        review_sha256=fingerprint,
        sequence=sequence,
        recorded_by=actor,
        recorded_at=datetime.now(UTC),
        result=result,
    )
    bind(
        db,
        review.project_id,
        REVIEW_RESOURCE,
        review.result_id,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [entry.model_dump(mode="json") for entry in review.versions],
            "parameters": {},
            "parameter_sha256": digest({}),
            "review_contract": record.model_dump(mode="json"),
        },
    )
    audit(
        db,
        review.project_id,
        actor,
        "continuity_review.contract_saved",
        review.result_id,
        {
            "candidate_id": str(review.target.candidate_id),
            "review_sha256": fingerprint,
            "input_sha256": record.input_sha256,
            "sequence": sequence,
            "previous_review_id": str(review.rerun.previous_review_id) if review.rerun else None,
            "state": record.state,
        },
    )
    db.flush()
    return record, True
