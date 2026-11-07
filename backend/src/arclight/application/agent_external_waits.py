"""Bounded readiness scans reuse the existing outbox dispatcher and execution fencing."""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import checked_loop
from arclight.application.agent_tools import agent_tool_is_pending, build_agent_tool_registry
from arclight.application.commands import emit
from arclight.application.model_gateway import lock_gate
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest
from arclight.domain.agent_tools import AgentToolCall
from arclight.domain.art_workflow import ART_WORKFLOW_VERSIONS
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.models import OutboxEvent, WorkflowRun


def waiting_for_external(db: Session, run: WorkflowRun) -> bool:
    return (
        run.status == "RUNNING"
        and run.workflow_version in ART_WORKFLOW_VERSIONS
        and db.scalar(
            select(AgentLoop.id).where(
                AgentLoop.run_id == run.id, AgentLoop.status == "WAITING_EXTERNAL"
            )
        )
        is not None
    )


def queue_ready_external(
    factory: sessionmaker[Session], settings: Settings, limit: int = 100
) -> int:
    from arclight.application.art_concepts import concept_tools
    from arclight.application.generation_leases import utc

    with factory() as db:
        ids = list(
            db.scalars(
                select(AgentLoop.id)
                .join(WorkflowRun, WorkflowRun.id == AgentLoop.run_id)
                .where(AgentLoop.status == "WAITING_EXTERNAL", WorkflowRun.status == "RUNNING")
                .order_by(AgentLoop.updated_at, AgentLoop.id)
                .limit(limit)
            )
        )
    registry = build_agent_tool_registry(settings=settings)
    for tool in concept_tools(settings):
        registry.register(tool)
    queued = 0
    for identity in ids:
        with factory.begin() as db:
            lock_gate(db)
            loop = db.get(AgentLoop, identity, populate_existing=True)
            run = (
                db.get(WorkflowRun, loop.run_id, populate_existing=True, with_for_update=True)
                if loop
                else None
            )
            if (
                loop is None
                or run is None
                or loop.status != "WAITING_EXTERNAL"
                or run.status != "RUNNING"
            ):
                continue
            # Rotate checked waits so an early pending page cannot starve ready runs.
            loop.updated_at = utcnow()
            if (
                loop.lease_token is not None
                and loop.lease_expires_at
                and utc(loop.lease_expires_at) > utcnow()
            ):
                continue
            try:
                checked_loop(db, loop.id)
                seed = AgentModelRequest.model_validate(loop.seed_json)
                request = AgentToolCall.model_validate((loop.outcome_json or {})["waiting_for"])
                # A director awaiting art must use its own admitted readiness rule.
                pending = agent_tool_is_pending(
                    db, registry, seed.agent_name, request, loop.actor_id, seed.stage
                )
            except (DomainError, ValueError, KeyError):
                pending = False  # Original executor records revocation, stale inputs or expiry.
            if pending:
                continue
            run.status = "QUEUED"
            run.revision += 1
            db.add(OutboxEvent(run_id=run.id, revision=run.revision))
            emit(db, run, "art_director.external_ready", {"loop_id": str(loop.id)})
            queued += 1
    return queued


def cancel_concepts(db: Session, run_id: UUID) -> None:
    from arclight.application.art_concept_jobs import concept_jobs
    from arclight.application.generation_state import request_cancel

    for job in concept_jobs(db, run_id=run_id):
        if job.status not in {"SUCCEEDED", "CANCELLED"}:
            request_cancel(db, job.id, job.revision, job.created_by)
