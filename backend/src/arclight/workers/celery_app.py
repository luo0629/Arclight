from celery import Celery

from arclight.core.config import get_settings

settings = get_settings()
celery_app = Celery("arclight", broker=settings.redis_url, include=["arclight.workers.tasks"])
celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    task_ignore_result=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    worker_prefetch_multiplier=1,
    task_default_queue="workflow",
    broker_connection_retry_on_startup=True,
    beat_schedule={"dispatch-outbox": {"task": "arclight.dispatch_outbox", "schedule": 3.0}},
)
