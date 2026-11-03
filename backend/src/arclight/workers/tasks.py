from uuid import UUID

from sqlalchemy import select

from arclight.application.generation_candidates import import_attempt, pending_attempts
from arclight.application.generation_delivery import consume_dispatch
from arclight.application.generation_events import publish_dispatches
from arclight.application.generation_leases import LeasePolicy
from arclight.application.generation_queues import QUEUES, claim_recovery_scan
from arclight.application.generation_recovery import recover_job, recovery_candidates
from arclight.application.rendering import execute_preview
from arclight.application.runtime import execute_run
from arclight.core.config import Settings, get_settings
from arclight.domain.enums import RunStatus
from arclight.persistence.base import utcnow
from arclight.persistence.models import OutboxEvent, RenderExecution, WorkflowRun
from arclight.persistence.session import build_engine, session_factory
from arclight.providers.media_cancel import get_media_cancel_provider
from arclight.providers.media_outputs import get_media_output_provider
from arclight.providers.media_query import get_media_query_provider
from arclight.providers.media_submission import get_media_submission_provider
from arclight.workers.celery_app import celery_app


@celery_app.task(name="arclight.execute_workflow")
def execute_workflow(run_id: str, revision: int) -> None:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        execute_run(session_factory(engine), settings, UUID(run_id), revision)
    finally:
        engine.dispose()


@celery_app.task(name="arclight.execute_generation")
def execute_generation(
    job_id: str, event_id: str, permit_id: str | None = None, permit_epoch: int | None = None
) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return consume_dispatch(
            session_factory(engine),
            UUID(job_id),
            UUID(event_id),
            get_media_submission_provider(),
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
        return recover_job(
            session_factory(engine),
            UUID(job_id),
            get_media_query_provider(),
            policy=media_lease_policy(settings),
            execution_policy=settings.media_execution_policy,
            cancel_provider=get_media_cancel_provider(),
        )
    finally:
        engine.dispose()


@celery_app.task(name="arclight.execute_preview")
def execute_render_preview(run_id: str) -> str:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return execute_preview(session_factory(engine), settings, UUID(run_id))
    finally:
        engine.dispose()


def publish_generation(
    job_id: UUID, event_id: UUID, permit_id: UUID, epoch: int, lane: str
) -> None:
    execute_generation.apply_async(
        args=[str(job_id), str(event_id), str(permit_id), epoch], queue=QUEUES[lane]
    )


@celery_app.task(name="arclight.import_generation_candidates")
def import_generation_candidates(attempt_id: str) -> int:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        return import_attempt(
            session_factory(engine), settings, UUID(attempt_id), get_media_output_provider()
        )
    finally:
        engine.dispose()


@celery_app.task(name="arclight.dispatch_outbox")
def dispatch_outbox() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        with session_factory(engine).begin() as db:
            # QUEUED 是业务事实；即使 Broker 丢失已发布消息，也会周期性重新投递。
            # 重复投递由 revision + 状态 + 运行行锁消除，不能宣称外部模型 exactly-once。
            events = db.scalars(
                select(OutboxEvent)
                .join(WorkflowRun, OutboxEvent.run_id == WorkflowRun.id)
                .where(
                    WorkflowRun.status == RunStatus.QUEUED,
                    OutboxEvent.revision == WorkflowRun.revision,
                )
                .order_by(OutboxEvent.created_at)
                .limit(100)
                .with_for_update(skip_locked=True)
            )
            for event in events:
                execute_workflow.apply_async(args=[str(event.run_id), event.revision])
                event.published_at = utcnow()
                event.attempt_count += 1
        publish_dispatches(
            session_factory(engine), publish_generation, settings.media_execution_policy
        )
        for attempt_id in pending_attempts(session_factory(engine)):
            import_generation_candidates.apply_async(args=[str(attempt_id)])
        if claim_recovery_scan(session_factory(engine)):
            for job_id in recovery_candidates(
                session_factory(engine), media_lease_policy(settings)
            ):
                recover_generation.apply_async(args=[str(job_id)])
        with session_factory(engine)() as db:
            previews = list(
                db.scalars(
                    select(RenderExecution.id)
                    .where(RenderExecution.status == "QUEUED")
                    .order_by(RenderExecution.created_at)
                    .limit(100)
                )
            )
        # QUEUED rows survive broker failure; duplicate deliveries use a database CAS.
        for run_id in previews:
            execute_render_preview.apply_async(args=[str(run_id)])
    finally:
        engine.dispose()
