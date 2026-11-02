import logging
from uuid import UUID

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from arclight.application.artifacts import ArtifactStore
from arclight.application.commands import emit
from arclight.application.context_snapshots import read_contexts
from arclight.application.model_services import run_settings
from arclight.application.production_versions import text_settings
from arclight.application.script_rewrite import generate_rewrite
from arclight.application.story_analysis import ANALYSIS_VERSION, extract
from arclight.application.story_outline import OUTLINE_VERSION, generate_outline
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.enums import RunStatus
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.context import bind_contexts
from arclight.providers.text import build_provider
from arclight.workflows.checkpoints import open_checkpointer
from arclight.workflows.production import DIRECTOR_WORKFLOW_VERSION, WORKFLOW_VERSION, build_graph
from arclight.workflows.rewrite import REWRITE_VERSION, build_rewrite_graph

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
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            settings = text_settings(db, episode.project_id, run, settings)
            contexts = read_contexts(db, episode.project_id, run)
            if run.workflow_type in {"story_analysis", "story_outline"}:
                expected_version = (
                    ANALYSIS_VERSION if run.workflow_type == "story_analysis" else OUTLINE_VERSION
                )
                if run.workflow_version != expected_version:
                    raise ValueError("不兼容的素材工作流版本")
                run.status = RunStatus.RUNNING
                emit(db, run, "workflow.started", {"revision": revision})
                generate = extract if run.workflow_type == "story_analysis" else generate_outline
                artifact_id, version = generate(factory, settings, run)
                run.status = RunStatus.SUCCEEDED
                run.error_code = None
                run.pending_review = None
                run.revision += 1
                emit(
                    db,
                    run,
                    "workflow.finished",
                    {
                        "status": run.status,
                        "analysis_id"
                        if run.workflow_type == "story_analysis"
                        else "outline_id": artifact_id,
                        "version_no": version,
                    },
                )
                return
            if run.workflow_version not in {
                WORKFLOW_VERSION,
                DIRECTOR_WORKFLOW_VERSION,
                REWRITE_VERSION,
            } or (
                run.workflow_version == REWRITE_VERSION and run.workflow_type != "script_rewrite"
            ):
                raise ValueError("不兼容的工作流版本")
            run.status = RunStatus.RUNNING
            # 不提前 flush：SQLite 的测试模式不允许跨连接同时写业务数据库。
            emit(db, run, "workflow.started", {"revision": revision})
            with open_checkpointer(settings) as saver:
                artifacts = ArtifactStore(
                    factory,
                    run.id,
                    run.episode_id,
                    run.input_json["target_duration_ms"],
                    settings.text_duration_tolerance_percent,
                )
                if run.workflow_version == REWRITE_VERSION:
                    graph = build_rewrite_graph(
                        artifacts,
                        saver,
                        lambda inputs: generate_rewrite(
                            inputs,
                            run_settings(settings, run.input_json.get("_text_service")),
                            contexts,
                        ),
                    )
                else:
                    graph = build_graph(
                        bind_contexts(
                            build_provider(
                                run_settings(settings, run.input_json.get("_text_service"))
                            ),
                            contexts,
                        ),
                        artifacts,
                        saver,
                        director_only=run.workflow_version == DIRECTOR_WORKFLOW_VERSION,
                    )
                config = {"configurable": {"thread_id": str(run.id)}}
                snapshot = graph.get_state(config)
                value: dict | Command | None = None
                if not snapshot.values:
                    inputs = {k: v for k, v in run.input_json.items() if not k.startswith("_")}
                    value = {"run_id": str(run.id), **inputs}
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
                    approved = snapshot.values.get(
                        "script_approved"
                        if run.workflow_version == REWRITE_VERSION
                        else "storyboard_approved",
                        False,
                    )
                    run.status = RunStatus.SUCCEEDED if approved else RunStatus.REJECTED
                    run.pending_review = None
                    emit(db, run, "workflow.finished", {"status": run.status})
                run.revision += 1
        except Exception as exc:
            # 不把供应商响应、凭证、输入正文或内部路径写到业务错误响应。
            logger.error("workflow_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
            run.status = RunStatus.FAILED
            run.error_code = (
                exc.code
                if isinstance(exc, DomainError)
                and exc.code
                in {
                    "PRODUCTION_VERSION_INCOMPATIBLE",
                    "CONTEXT_SNAPSHOT_INVALID",
                    "CONTEXT_TOO_LARGE",
                    "CONTEXT_INPUT_UNSAFE",
                    "SCRIPT_DURATION_REQUIRED",
                    "SCRIPT_DURATION_OUT_OF_RANGE",
                    "STORYBOARD_DURATION_OUT_OF_RANGE",
                }
                else "WORKFLOW_EXECUTION_FAILED"
            )
            run.revision += 1
            emit(db, run, "workflow.failed", {"code": run.error_code})
