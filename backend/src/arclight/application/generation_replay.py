"""Owner-authorized new jobs retain immutable dead-letter/source associations."""

import hashlib
import json
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.generation_jobs import create_job
from arclight.application.generation_state import check_revision, find_job
from arclight.application.media_services import selection_for_job
from arclight.application.production_versions import check_media
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import GenerationCreate, MediaInput, safe_metadata
from arclight.domain.generation_scheduling import ExecutionPolicy
from arclight.persistence.generation import GenerationJob
from arclight.persistence.generation_queues import GenerationLane
from arclight.persistence.generation_scheduling import GenerationDeadLetter, GenerationReplay
from arclight.persistence.models import DependencyManifest


class ReplayRequest(MediaInput):
    expected_revision: int = Field(strict=True, ge=0)
    reason: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def meaningful_reason(self):
        if not self.reason.strip():
            raise ValueError("Replay requires a reason")
        safe_metadata(self.reason)
        return self


def replay_job(
    db: Session,
    project_id: UUID,
    source_id: UUID,
    actor: str,
    key: str,
    payload: ReplayRequest,
    policy: ExecutionPolicy,
) -> GenerationJob:
    require_project(db, project_id, actor, write=True, owner=True)
    lock_dependency_graph(db, project_id)
    require_project(db, project_id, actor, write=True, owner=True)
    source = find_job(db, source_id, lock=True)
    if source.project_id != project_id:
        raise DomainError("NOT_FOUND", "Generation job does not exist", 404)
    from arclight.application.continuity_revision_store import job_revision

    if job_revision(db, source) is not None:
        raise DomainError(
            "REVISION_REPLAY_UNSAFE",
            "审查修订不能通过死信重放重置次数；请处理原任务或重新审查",
            409,
        )
    fingerprint = hashlib.sha256(
        json.dumps({"source": str(source_id), "reason": payload.reason}, sort_keys=True).encode()
    ).hexdigest()
    existing = db.scalar(
        select(GenerationReplay).where(
            GenerationReplay.project_id == project_id, GenerationReplay.creation_key == key
        )
    )
    if existing is not None:
        if existing.request_fingerprint != fingerprint:
            raise DomainError(
                "IDEMPOTENCY_CONFLICT", "Replay key already has a different request", 409
            )
        return find_job(db, existing.new_job_id)
    check_revision(source, payload.expected_revision)
    dead = db.scalar(select(GenerationDeadLetter).where(GenerationDeadLetter.job_id == source.id))
    if dead is None or source.status not in {"FAILED", "CANCELLED"}:
        raise DomainError(
            "GENERATION_REPLAY_UNSAFE",
            "Replay requires a diagnosed definite outcome; reconcile unknown execution first",
            409,
        )
    manifest = db.get(DependencyManifest, source.input_manifest_id)
    assert manifest is not None
    check_media(db, project_id, source.id, manifest.id)
    snapshot = manifest.snapshot_json
    lane = db.scalar(select(GenerationLane.lane).where(GenerationLane.job_id == source.id))
    request = GenerationCreate.model_validate(
        {
            "batch_id": snapshot["context"].get("budget_batch_id"),
            "scheduling_mode": lane or "BATCH",
            "target_type": source.target_type,
            "target_asset_id": source.target_asset_id,
            "episode_id": source.episode_id,
            "expected_asset": snapshot["target"]["expected_asset"],
            "task_type": source.task_type,
            "spec": snapshot["context"]["generation"],
            "inputs": [
                {
                    "source_asset_version_id": item["source_asset_version_id"],
                    "dependency_type": item["dependency_type"],
                    "selectors": item.get("selectors", []),
                }
                for item in snapshot["inputs"]
            ],
        }
    )
    service = selection_for_job(db, source)
    job = create_job(
        db,
        project_id,
        actor,
        "replay:" + str(uuid4()),
        request,
        execution_policy=policy,
        frozen_prompt_composition=snapshot["context"].get("prompt_composition"),
        frozen_service=service.private_snapshot() if service else None,
    )
    row = GenerationReplay(
        project_id=project_id,
        source_job_id=source.id,
        dead_letter_id=dead.id,
        new_job_id=job.id,
        creation_key=key,
        request_fingerprint=fingerprint,
        actor=actor,
        reason=payload.reason,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "generation.replayed",
        job.id,
        {"source_job_id": str(source.id), "dead_letter_id": str(dead.id), "replay_id": str(row.id)},
    )
    return job
