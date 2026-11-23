from datetime import timedelta
from uuid import UUID

from sqlalchemy import String, cast, func, select
from sqlalchemy.orm import Session, aliased

from arclight.application import generation_artifacts
from arclight.application.agent_workflows import WorkflowExecutionDeferred
from arclight.application.generation_candidates import import_attempt, pending_attempts
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_events import publish_dispatches
from arclight.application.generation_leases import LeasePolicy
from arclight.application.generation_queues import QUEUES, claim_recovery_scan
from arclight.application.generation_recovery import recover_job, recovery_candidates
from arclight.application.media_services import connection_for_job
from arclight.application.production_events import consume_stage_event, publish_stage_events
from arclight.application.render_dispatch import candidates as render_candidates
from arclight.application.render_execution import execute_render
from arclight.application.rendering import execute_preview
from arclight.application.runtime import execute_run
from arclight.core.config import Settings, get_settings
from arclight.domain.enums import RunStatus
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.base import utcnow
from arclight.persistence.models import (
    GenerationAttempt,
    GenerationJob,
    OutboxEvent,
    RenderExecution,
    RenderJob,
    WorkflowRun,
)
from arclight.persistence.session import build_engine, session_factory
from arclight.persistence.versions import VersionBinding
from arclight.providers.media_cancel import get_media_cancel_provider
from arclight.providers.media_outputs import get_media_output_provider
from arclight.providers.media_query import get_media_query_provider
from arclight.providers.media_submission import get_media_submission_provider
from arclight.workers.celery_app import celery_app


@celery_app.task(
    name="arclight.execute_workflow",
    autoretry_for=(WorkflowExecutionDeferred,),
    max_retries=None,
    retry_kwargs={"countdown": 10},
)
def execute_workflow(run_id: str, revision: int) -> None:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        execute_run(session_factory(engine, settings), settings, UUID(run_id), revision)
    finally:
        engine.dispose()


@celery_app.task(name="arclight.execute_generation")
def execute_generation(
    job_id: str, event_id: str, permit_id: str | None = None, permit_epoch: int | None = None
) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        factory = session_factory(engine, settings)
        with factory() as db:
            job = db.get(GenerationJob, UUID(job_id))
            if job is None:
                return "NOT_FOUND"
            connection = connection_for_job(db, job, settings)
            provider = (
                get_media_submission_provider(job.provider, settings, connection=connection)
                if connection
                else get_media_submission_provider(job.provider, settings)
            )
        return consume_dispatch(
            factory,
            UUID(job_id),
            UUID(event_id),
            provider,
            policy=media_lease_policy(settings),
            execution_policy=settings.media_execution_policy,
            gateway_policy=settings.model_gateway_policy,
            permit_id=UUID(permit_id) if permit_id is not None else None,
            permit_epoch=permit_epoch,
            require_permit=True,
        )
    finally:
        engine.dispose()


def media_lease_policy(settings: Settings) -> LeasePolicy:
    return LeasePolicy(
        settings.media_lease_seconds, settings.media_heartbeat_seconds, settings.media_poll_seconds
    )


@celery_app.task(name="arclight.recover_generation")
def recover_generation(job_id: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        factory = session_factory(engine, settings)
        with factory() as db:
            job = db.get(GenerationJob, UUID(job_id))
            if job is None:
                return "NOT_FOUND"
            connection = connection_for_job(db, job, settings)
        query_provider = get_media_query_provider()
        cancel_provider = get_media_cancel_provider()
        if connection is not None:
            from arclight.providers.native_media import NativeMediaProvider

            native = NativeMediaProvider(settings, connection)
            query_provider = native
            cancel_provider = native
        return recover_job(
            factory,
            UUID(job_id),
            query_provider,
            policy=media_lease_policy(settings),
            execution_policy=settings.media_execution_policy,
            cancel_provider=cancel_provider,
        )
    finally:
        engine.dispose()


@celery_app.task(name="arclight.execute_preview")
def execute_render_preview(run_id: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return execute_preview(session_factory(engine, settings), settings, UUID(run_id))
    finally:
        engine.dispose()


@celery_app.task(name="arclight.execute_render")
def execute_manifest_render(run_id: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return execute_render(session_factory(engine, settings), settings, UUID(run_id))
    finally:
        engine.dispose()


def publish_generation(
    job_id: UUID, event_id: UUID, permit_id: UUID, epoch: int, lane: str
) -> None:
    execute_generation.apply_async(
        args=[str(job_id), str(event_id), str(permit_id), epoch], queue=QUEUES[lane]
    )


@celery_app.task(name="arclight.coordinate_production")
def coordinate_production(event_id: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        result = consume_stage_event(session_factory(engine, settings), UUID(event_id))
        return result.disposition
    finally:
        engine.dispose()


def publish_production(event_id: UUID) -> None:
    coordinate_production.apply_async(args=[str(event_id)], queue="scheduler.control")


@celery_app.task(name="arclight.import_generation_candidates")
def import_generation_candidates(attempt_id: str) -> int:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        factory = session_factory(engine, settings)
        with factory() as db:
            attempt = db.get(GenerationAttempt, UUID(attempt_id))
            job = db.get(GenerationJob, attempt.job_id) if attempt is not None else None
            if job is None:
                return 0
            connection = connection_for_job(db, job, settings, for_output=True)
        importer = (
            import_attempt if job.task_type == "IMAGE" else generation_artifacts.import_attempt
        )
        provider = (
            get_media_output_provider(job.provider, settings, connection=connection)
            if connection
            else get_media_output_provider(job.provider, settings)
        )
        return importer(factory, settings, UUID(attempt_id), provider)
    finally:
        engine.dispose()


def pending_workflow_events(db: Session) -> list[OutboxEvent]:
    from arclight.application.producer_interaction import COMMAND, RECEIPT

    stamp = utcnow()
    due = OutboxEvent.published_at.is_(None) | (
        OutboxEvent.published_at < stamp - timedelta(seconds=30)
    )
    command, receipt = aliased(VersionBinding), aliased(VersionBinding)
    producer_pending = (
        select(command.id)
        .where(
            command.resource_type == COMMAND,
            func.replace(command.snapshot_json["run_id"].as_string(), "-", "")
            == func.replace(cast(WorkflowRun.id, String), "-", ""),
            command.snapshot_json["revision"].as_integer() == OutboxEvent.revision,
            ~select(receipt.id)
            .where(
                receipt.resource_type == RECEIPT,
                receipt.project_id == command.project_id,
                receipt.resource_id == command.resource_id,
            )
            .exists(),
        )
        .exists()
    )
    root = aliased(WorkflowRun)
    automatic_specialist = (
        select(AgentRunLimit.run_id)
        .join(root, root.id == AgentRunLimit.root_run_id)
        .where(
            AgentRunLimit.run_id == WorkflowRun.id,
            AgentRunLimit.parent_run_id == root.id,
            root.workflow_type == "producer",
            root.status == RunStatus.RUNNING,
            root.input_json["short_drama"]["script_version_id"].as_string().is_not(None),
        )
        .exists()
    )
    # External waits have their own readiness scan. Live leases keep their current delivery.
    specialist_busy = (
        select(AgentLoop.id)
        .where(
            AgentLoop.run_id == WorkflowRun.id,
            AgentLoop.status.in_(("WAITING_HUMAN", "WAITING_EXTERNAL"))
            | (AgentLoop.lease_token.is_not(None) & (AgentLoop.lease_expires_at > stamp)),
        )
        .exists()
    )
    # 任务可在 Broker 丢失消息后重新投递。Worker 以精确 revision 和状态领用。
    return list(
        db.scalars(
            select(OutboxEvent)
            .join(WorkflowRun, OutboxEvent.run_id == WorkflowRun.id)
            .where(
                (
                    (WorkflowRun.status == RunStatus.QUEUED)
                    | (
                        (WorkflowRun.workflow_type == "continuity_review")
                        & (WorkflowRun.status == RunStatus.RUNNING)
                        & WorkflowRun.pending_review["resource_type"].as_string().is_(None)
                    )
                    | (
                        (WorkflowRun.workflow_type == "producer")
                        & (WorkflowRun.status == RunStatus.RUNNING)
                        & WorkflowRun.input_json["short_drama"]["script_version_id"]
                        .as_string()
                        .is_not(None)
                        & producer_pending
                        & due
                    )
                    | (
                        WorkflowRun.workflow_type.in_(("art_proposal", "storyboard"))
                        & (WorkflowRun.status == RunStatus.RUNNING)
                        & automatic_specialist
                        & ~specialist_busy
                        & due
                    )
                ),
                OutboxEvent.revision == WorkflowRun.revision,
            )
            .order_by(OutboxEvent.created_at)
            .limit(100)
            .with_for_update(skip_locked=True, of=OutboxEvent)
        )
    )


@celery_app.task(name="arclight.dispatch_outbox")
def dispatch_outbox() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        from arclight.application.agent_external_waits import queue_ready_external

        queue_ready_external(session_factory(engine, settings), settings)
        from arclight.application.continuity_workflow import queue_pending_reviews

        queue_pending_reviews(session_factory(engine, settings), settings)
        from arclight.application.short_drama_production import advance_pending

        advance_pending(session_factory(engine, settings), settings)
        with session_factory(engine, settings).begin() as db:
            for event in pending_workflow_events(db):
                execute_workflow.apply_async(args=[str(event.run_id), event.revision])
                event.published_at = utcnow()
                event.attempt_count += 1
        publish_dispatches(
            session_factory(engine, settings), publish_generation, settings.media_execution_policy
        )
        publish_stage_events(session_factory(engine, settings), publish_production)
        for attempt_id in [
            *pending_attempts(session_factory(engine, settings)),
            *generation_artifacts.pending_attempts(session_factory(engine, settings)),
        ]:
            import_generation_candidates.apply_async(args=[str(attempt_id)])
        if claim_recovery_scan(session_factory(engine, settings)):
            for job_id in recovery_candidates(
                session_factory(engine, settings), media_lease_policy(settings)
            ):
                recover_generation.apply_async(args=[str(job_id)])
        with session_factory(engine, settings)() as db:
            previews = list(
                db.scalars(
                    select(RenderExecution.id)
                    .where(
                        RenderExecution.status == "QUEUED",
                        ~select(RenderJob.id).where(RenderJob.id == RenderExecution.id).exists(),
                    )
                    .order_by(RenderExecution.created_at)
                    .limit(100)
                )
            )
        # QUEUED rows survive broker failure; duplicate deliveries use a database CAS.
        for run_id in previews:
            execute_render_preview.apply_async(args=[str(run_id)])
        for run_id in render_candidates(session_factory(engine, settings)):
            execute_manifest_render.apply_async(args=[str(run_id)])
    finally:
        engine.dispose()
