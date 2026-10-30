"""Append attempts and retain normalized submission/result facts without raw credentials."""

import re
from copy import deepcopy
from decimal import Decimal
from typing import get_args
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_events import enqueue_dispatch
from arclight.application.generation_state import check_revision, find_job, transition
from arclight.application.generation_targets import require_available_target
from arclight.core.errors import DomainError
from arclight.domain.generation import GenerationStatus
from arclight.domain.generation_requests import GenerationResult, GenerationSpec, MediaErrorCode
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    DependencyManifest,
    Episode,
    GenerationAttempt,
    GenerationJob,
    Project,
    VisualAsset,
)


def require_writable_target(db: Session, job: GenerationJob) -> None:
    project = db.get(Project, job.project_id, populate_existing=True)
    if project is None or project.deleted_at is not None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
    if project.archived:
        raise DomainError("PROJECT_ARCHIVED", "归档项目只允许查看", 409)
    if job.episode_id is not None:
        episode = db.get(Episode, job.episode_id, populate_existing=True)
        if episode is None or episode.project_id != job.project_id or episode.status == "ARCHIVED":
            raise DomainError("EPISODE_READ_ONLY", "目标剧集不可生成", 409)
    if job.target_asset_id is not None:
        asset = db.get(VisualAsset, job.target_asset_id, populate_existing=True)
        if (
            asset is None
            or asset.project_id != job.project_id
            or asset.status in {"ARCHIVED", "LOCKED"}
        ):
            raise DomainError("ASSET_READ_ONLY", "目标资产不可生成", 409)
        if job.expected_asset_revision is None or job.expected_lock_version is None:
            raise DomainError("INVALID_GENERATION_INPUT", "媒体任务缺少冻结目标版本", 409)
        if (asset.revision, asset.lock_version, asset.current_version_id) != (
            job.expected_asset_revision,
            job.expected_lock_version,
            job.expected_current_version_id,
        ):
            raise DomainError("VERSION_CONFLICT", "目标资产已变化，请捕获新输入后生成", 409)
        require_available_target(db, asset.id, exclude_job_id=job.id)


def start_attempt(db: Session, job_id: UUID, expected_revision: int) -> GenerationAttempt:
    row = find_job(db, job_id, lock=True)
    check_revision(row, expected_revision)
    require_writable_target(db, row)
    if row.status not in {"CREATED", "RETRY_WAIT"} or row.cancel_requested_at is not None:
        raise DomainError("GENERATION_NOT_SUBMITTABLE", "媒体任务当前不能提交新尝试", 409)
    manifest = db.get(DependencyManifest, row.input_manifest_id)
    if manifest is None or manifest.project_id != row.project_id:
        raise DomainError("INVALID_GENERATION_INPUT", "媒体输入清单不匹配", 409)
    try:
        snapshot = manifest.snapshot_json
        spec = GenerationSpec.model_validate(snapshot["context"]["generation"])
        target = snapshot["target"]
        matches = (
            snapshot["schema_version"] == 1
            and manifest.request_fingerprint == row.request_fingerprint
            and manifest.target_asset_id == row.target_asset_id
            and target["asset_id"] == (str(row.target_asset_id) if row.target_asset_id else None)
            and target["asset_type"] == row.target_type
            and target["episode_id"] == (str(row.episode_id) if row.episode_id else None)
            and target["expected_asset"]
            == (
                {
                    "revision": row.expected_asset_revision,
                    "lock_version": row.expected_lock_version,
                    "current_version_id": str(row.expected_current_version_id)
                    if row.expected_current_version_id
                    else None,
                }
                if row.target_asset_id
                else None
            )
            and spec.provider == row.provider
            and spec.model == row.model
            and isinstance(snapshot["inputs"], list)
        )
    except (KeyError, TypeError, ValidationError):
        matches = False
    if not matches:
        raise DomainError("INVALID_GENERATION_INPUT", "媒体任务缺少匹配的冻结输入", 409)
    attempt = GenerationAttempt(
        job_id=row.id,
        attempt_no=row.attempt_count + 1,
        input_manifest_id=manifest.id,
        request_fingerprint=row.request_fingerprint,
        provider=row.provider,
        model=row.model,
        input_snapshot_json=deepcopy(manifest.snapshot_json),
        submitted_at=utcnow(),
    )
    row = transition(db, row.id, GenerationStatus.SUBMITTING, expected_revision)
    row.attempt_count += 1
    row.provider_request_id = None
    db.add(attempt)
    db.flush()
    audit(
        db,
        row.project_id,
        "media-worker",
        "generation.attempt_started",
        row.id,
        {
            "attempt_id": str(attempt.id),
            "attempt_no": attempt.attempt_no,
            "input_manifest_id": str(manifest.id),
        },
    )
    return attempt


def current_attempt(db: Session, job: GenerationJob, attempt_id: UUID) -> GenerationAttempt:
    row = db.get(GenerationAttempt, attempt_id, populate_existing=True)
    if row is None or row.job_id != job.id or row.attempt_no != job.attempt_count:
        raise DomainError("ATTEMPT_CONFLICT", "尝试不属于当前媒体调用", 409)
    return row


def set_provider_request(
    job: GenerationJob, attempt: GenerationAttempt, request_id: str | None
) -> None:
    if request_id is None:
        return
    if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,200}", request_id):
        raise DomainError("INVALID_PROVIDER_RECEIPT", "供应商回执标识不合法", 502)
    if attempt.provider_request_id is not None and attempt.provider_request_id != request_id:
        raise DomainError("PROVIDER_RECEIPT_CONFLICT", "供应商回执标识不能改绑", 409)
    attempt.provider_request_id = request_id
    job.provider_request_id = request_id


def record_submission(
    db: Session,
    job_id: UUID,
    attempt_id: UUID,
    request_id: str,
) -> GenerationAttempt:
    job = find_job(db, job_id, lock=True)
    attempt = current_attempt(db, job, attempt_id)
    if attempt.finished_at is not None or attempt.status != "SUBMITTING":
        raise DomainError("ATTEMPT_CONFLICT", "尝试已经收到提交回执", 409)
    set_provider_request(job, attempt, request_id)
    attempt.status = "SUBMITTED"
    if job.status != "CANCEL_REQUESTED":
        transition(db, job.id, GenerationStatus.SUBMITTED, job.revision)
    db.flush()
    return attempt


def finish_attempt(
    db: Session,
    job_id: UUID,
    attempt_id: UUID,
    status: GenerationStatus,
    *,
    request_id: str | None = None,
    result: GenerationResult | None = None,
    error_code: MediaErrorCode | None = None,
    latency_ms: int | None = None,
    cost: Decimal | None = None,
) -> GenerationAttempt:
    if status not in {
        GenerationStatus.SUCCEEDED,
        GenerationStatus.FAILED,
        GenerationStatus.CANCELLED,
    }:
        raise DomainError("INVALID_ATTEMPT_OUTCOME", "必须提供明确的调用结果", 409)
    if status == GenerationStatus.SUCCEEDED and (result is None or error_code is not None):
        raise DomainError("INVALID_ATTEMPT_OUTCOME", "成功调用必须保存规范化结果", 409)
    if status != GenerationStatus.SUCCEEDED and (result is not None or error_code is None):
        raise DomainError("INVALID_ATTEMPT_OUTCOME", "失败或取消调用必须提供稳定错误码", 409)
    if error_code is not None and (
        error_code not in get_args(MediaErrorCode) or error_code == "RESULT_UNKNOWN"
    ):
        raise DomainError("INVALID_ATTEMPT_OUTCOME", "调用错误必须使用稳定错误码", 422)
    if (
        latency_ms is not None
        and (type(latency_ms) is not int or not 0 <= latency_ms <= 2147483647)
    ) or (
        cost is not None
        and (
            not isinstance(cost, Decimal)
            or not cost.is_finite()
            or not 0 <= cost < Decimal("1000000000000")
            or cost != cost.quantize(Decimal("0.000001"))
        )
    ):
        raise DomainError("INVALID_ATTEMPT_USAGE", "调用用量与费用必须为非负有限值", 422)
    job = find_job(db, job_id, lock=True)
    attempt = current_attempt(db, job, attempt_id)
    if attempt.finished_at is not None:
        raise DomainError("ATTEMPT_FINISHED", "历史尝试不能覆盖", 409)
    set_provider_request(job, attempt, request_id)
    transition(db, job.id, status, job.revision)
    attempt.status = status
    attempt.result_json = result.model_dump(mode="json") if result else None
    attempt.error_code = error_code
    attempt.latency_ms = latency_ms
    attempt.cost = cost
    attempt.finished_at = utcnow()
    db.flush()
    audit(
        db,
        job.project_id,
        "media-worker",
        "generation.attempt_finished",
        job.id,
        {
            "attempt_id": str(attempt.id),
            "attempt_no": attempt.attempt_no,
            "status": status,
            "error_code": error_code,
        },
    )
    return attempt


def request_retry(db: Session, job_id: UUID, expected_revision: int, actor: str) -> GenerationJob:
    job = find_job(db, job_id, lock=True)
    require_project(db, job.project_id, actor, write=True)
    check_revision(job, expected_revision)
    require_writable_target(db, job)
    latest = db.scalar(
        select(GenerationAttempt).where(
            GenerationAttempt.job_id == job.id,
            GenerationAttempt.attempt_no == job.attempt_count,
        )
    )
    if (
        job.status != "FAILED"
        or job.cancel_requested_at is not None
        or latest is None
        or latest.status != "FAILED"
        or latest.finished_at is None
    ):
        raise DomainError(
            "GENERATION_NOT_RETRYABLE", "仅明确失败的调用可显式重试；未知结果须先对账", 409
        )
    row = transition(db, job.id, GenerationStatus.RETRY_WAIT, expected_revision, actor=actor)
    enqueue_dispatch(db, row)
    return row
