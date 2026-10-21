from uuid import UUID

from sqlalchemy import select

from arclight.application.runtime import execute_run
from arclight.core.config import get_settings
from arclight.domain.enums import RunStatus
from arclight.persistence.base import utcnow
from arclight.persistence.models import OutboxEvent, WorkflowRun
from arclight.persistence.session import build_engine, session_factory
from arclight.workers.celery_app import celery_app


@celery_app.task(name="arclight.execute_workflow")
def execute_workflow(run_id: str, revision: int) -> None:
    settings = get_settings()
    engine = build_engine(settings)
    try:
        execute_run(session_factory(engine), settings, UUID(run_id), revision)
    finally:
        engine.dispose()


@celery_app.task(name="arclight.dispatch_outbox")
def dispatch_outbox() -> None:
    engine = build_engine(get_settings())
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
    finally:
        engine.dispose()
