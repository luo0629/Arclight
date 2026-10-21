import logging
from uuid import UUID

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from arclight.application.artifacts import ArtifactStore
from arclight.application.commands import emit
from arclight.core.config import Settings
from arclight.domain.enums import RunStatus
from arclight.persistence.models import WorkflowRun
from arclight.providers.text import build_provider
from arclight.workflows.checkpoints import open_checkpointer
from arclight.workflows.production import WORKFLOW_VERSION, build_graph

logger = logging.getLogger(__name__)


def execute_run(factory: sessionmaker, settings: Settings, run_id: UUID, revision: int) -> None:
    # PostgreSQL 行锁覆盖整次图推进，重复消息不会并发执行相同 thread。
    # SQLite 仅用于单进程开发/测试，不提供生产级多 Worker 互斥保障。
    with factory.begin() as db:
        run = db.scalar(
            select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(key_share=True)
        )
        if run is None or run.status != RunStatus.QUEUED or run.revision != revision:
            return
        try:
            if run.workflow_version != WORKFLOW_VERSION:
                raise ValueError("不兼容的工作流版本")
            run.status = RunStatus.RUNNING
            # 不提前 flush：SQLite 的测试模式不允许跨连接同时写业务数据库。
            emit(db, run, "workflow.started", {"revision": revision})
            with open_checkpointer(settings) as saver:
                graph = build_graph(
                    build_provider(settings), ArtifactStore(factory, run.id, run.episode_id), saver
                )
                config = {"configurable": {"thread_id": str(run.id)}}
                snapshot = graph.get_state(config)
                value: dict | Command | None = None
                if not snapshot.values:
                    value = {"run_id": str(run.id), **run.input_json}
                elif snapshot.interrupts and run.resume_json:
                    pending = snapshot.interrupts[0].value
                    if (
                        pending["version_id"] == run.resume_json["version_id"]
                        and pending["resource_type"] == run.resume_json["resource_type"]
                    ):
                        value = Command(resume=run.resume_json)
                graph.invoke(value, config, durability="sync")
                snapshot = graph.get_state(config)
                run.resume_json = None
                if snapshot.interrupts:
                    run.status = RunStatus.WAITING_REVIEW
                    run.pending_review = snapshot.interrupts[0].value
                    emit(db, run, "workflow.waiting_review", run.pending_review)
                else:
                    approved = snapshot.values.get("storyboard_approved", False)
                    run.status = RunStatus.SUCCEEDED if approved else RunStatus.REJECTED
                    run.pending_review = None
                    emit(db, run, "workflow.finished", {"status": run.status})
                run.revision += 1
        except Exception as exc:
            # 不把供应商响应、凭证、输入正文或内部路径写到业务错误响应。
            logger.error("workflow_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
            run.status = RunStatus.FAILED
            run.error_code = "WORKFLOW_EXECUTION_FAILED"
            run.revision += 1
            emit(db, run, "workflow.failed", {"code": run.error_code})
