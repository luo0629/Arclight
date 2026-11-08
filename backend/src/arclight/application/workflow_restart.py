"""Explicit new runs preserve business inputs and capture the caller's current services."""

from copy import deepcopy
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.commands import emit, require_episode, require_run
from arclight.application.gateway_accounting import latest_receipt
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import text_snapshot
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.versions import digest
from arclight.persistence.models import (
    AuditEvent,
    GatewayCall,
    GenerationAttempt,
    OutboxEvent,
    StorySourceBinding,
    WorkflowRun,
)


def restart_run(
    db: Session, run_id: UUID, actor: str, key: str, request_id: str, settings: Settings
) -> WorkflowRun:
    lock_gate(db)
    original = require_run(db, run_id, actor, lock=True)
    episode = require_episode(db, original.episode_id, actor, write=True)
    db.refresh(episode, with_for_update=True)
    fingerprint = digest({"restart_from_run_id": str(original.id), "actor": actor})
    existing = db.scalar(
        select(WorkflowRun).where(
            WorkflowRun.episode_id == episode.id, WorkflowRun.idempotency_key == key
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "此幂等键已用于其他任务", 409)
        return existing
    if original.status != "FAILED":
        raise DomainError("NOT_RETRYABLE", "只有失败的工作流可以使用当前配置重新发起", 409)
    if db.scalar(
        select(WorkflowRun.id).where(
            WorkflowRun.episode_id == episode.id,
            WorkflowRun.status.in_({"QUEUED", "RUNNING", "WAITING_REVIEW"}),
        )
    ):
        raise DomainError("EPISODE_BUSY", "该剧集已有未完成工作流", 409)
    _require_resolved(db, original)
    from arclight.application.agent_runtime_state import initialize_run_state
    from arclight.application.context_snapshots import read_contexts
    from arclight.application.production_versions import capture_text

    # Revalidate the original exact sources without reading its retired API key.
    read_contexts(db, episode.project_id, original)
    inputs = deepcopy(original.input_json)
    inputs.pop("_text_service", None)
    inputs.pop("_image_service", None)
    inputs["created_by"] = actor
    inputs["_restart_from_run_id"] = str(original.id)
    service = text_snapshot(db, actor)
    if service:
        inputs["_text_service"] = service
    _refresh_concepts(db, episode.project_id, episode.id, inputs, actor)
    run = WorkflowRun(
        episode_id=episode.id,
        trace_id=request_id,
        idempotency_key=key,
        request_fingerprint=fingerprint,
        workflow_type=original.workflow_type,
        workflow_version=original.workflow_version,
        input_json=inputs,
    )
    db.add(run)
    db.flush()
    initialize_run_state(run, episode.project_id)
    capture_text(db, episode.project_id, run, settings, actor=actor)
    if "screenwriter" in inputs:
        from arclight.application.screenwriter_creation import authorize_screenwriter

        authorize_screenwriter(db, run, actor, settings)
    if "director" in inputs:
        from arclight.application.director_creation import authorize_director

        authorize_director(db, run, actor, settings)
    if "art_direction" in inputs:
        from arclight.application.art_workflow import authorize_art

        authorize_art(db, run, actor, settings)
    for binding in db.scalars(
        select(StorySourceBinding).where(StorySourceBinding.run_id == original.id)
    ):
        db.add(StorySourceBinding(run_id=run.id, source_id=binding.source_id))
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="workflow.restarted_with_current_services",
            resource_id=run.id,
            details_json={"source_run_id": str(original.id)},
        )
    )
    db.add(OutboxEvent(run_id=run.id, revision=0))
    emit(db, run, "workflow.queued", {"source_run_id": str(original.id)})
    return run


def _require_resolved(db: Session, original: WorkflowRun) -> None:
    from arclight.application.art_concept_jobs import concept_jobs

    jobs = concept_jobs(db, run_id=original.id)
    if any(job.status not in {"SUCCEEDED", "FAILED", "CANCELLED"} for job in jobs):
        raise DomainError("RESTART_RECONCILIATION_REQUIRED", "请先核验原概念图任务的执行结果", 409)
    attempt_ids = select(GenerationAttempt.id).where(
        GenerationAttempt.job_id.in_([job.id for job in jobs])
    )
    calls = db.scalars(
        select(GatewayCall).where(
            (GatewayCall.workflow_id == original.id)
            | GatewayCall.generation_attempt_id.in_(attempt_ids)
        )
    )
    for call in calls:
        receipt = latest_receipt(db, call.id)
        if receipt is None or receipt.status in {"RESULT_UNKNOWN", "SUBMITTED", "RUNNING"}:
            raise DomainError(
                "RESTART_RECONCILIATION_REQUIRED", "原调用结果尚未确定，请先核验再重新发起", 409
            )


def _refresh_concepts(
    db: Session, project_id: UUID, episode_id: UUID, inputs: dict, actor: str
) -> None:
    frozen = inputs.get("art_direction", {}).get("concept")
    if not frozen:
        return
    from arclight.application.art_concepts import freeze_concept_plan
    from arclight.application.media_services import account_selection
    from arclight.domain.art_concepts import ArtConceptPlan
    from arclight.providers.native_media_protocols import KINDS

    plan = ArtConceptPlan.model_validate(frozen["plan"])
    selection = account_selection(db, actor, "IMAGE")
    spec = plan.spec.model_dump(mode="json")
    if selection:
        config = selection.configuration
        spec.update(
            provider=config["provider"],
            model=config["model"],
            model_config_version=config["configuration_version"],
        )
        inputs["_image_service"] = selection.private_snapshot()
    elif spec["provider"] in KINDS or spec["provider"] == "openai":
        config = db.info["settings"].image_service
        if not config.enabled:
            raise DomainError("SERVICE_INCOMPLETE", "请先配置当前账号或服务器图片服务", 422)
        spec.update(
            provider="openai",
            model=config.model,
            model_config_version=config.public_configuration()["configuration_version"],
        )
    plan = ArtConceptPlan.model_validate(plan.model_dump(mode="json") | {"spec": spec})
    inputs["art_direction"]["concept"] = freeze_concept_plan(
        db,
        project_id,
        episode_id,
        plan,
        actor=actor,
        frozen_service=inputs.get("_image_service"),
    )
