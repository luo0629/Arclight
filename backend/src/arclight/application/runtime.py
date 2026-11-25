import logging
from uuid import UUID

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.artifacts import ArtifactStore
from arclight.application.commands import emit
from arclight.application.context_consumption import consume_context
from arclight.application.context_snapshots import read_contexts
from arclight.application.context_sources import public_startup
from arclight.application.model_services import run_settings
from arclight.application.production_versions import text_settings
from arclight.application.script_rewrite import generate_rewrite
from arclight.application.story_analysis import ANALYSIS_VERSION, extract
from arclight.application.story_outline import OUTLINE_VERSION, generate_outline
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_state import (
    AgentRunScope,
    AgentRunState,
    AgentStateError,
    AgentStepId,
    restore_agent_state,
    terminate_agent_state,
    transition_agent_step,
)
from arclight.domain.enums import RunStatus
from arclight.domain.script_drafts import (
    DRAFT_WORKFLOWS,
    REWRITE_WORKFLOWS,
    SCRIPT_DRAFT,
    SCRIPT_REWRITE_DRAFT,
)
from arclight.persistence.models import Episode, WorkflowRun
from arclight.providers.context import bind_contexts
from arclight.providers.text import build_provider
from arclight.workflows.checkpoints import open_checkpointer
from arclight.workflows.production import DIRECTOR_WORKFLOW_VERSION, WORKFLOW_VERSION, build_graph
from arclight.workflows.rewrite import REWRITE_VERSION, build_rewrite_graph

logger = logging.getLogger(__name__)


def execute_run(factory: sessionmaker, settings: Settings, run_id: UUID, revision: int) -> None:
    """Execute on a synchronous worker thread, including the run's MCP queries."""
    from arclight.application.gateway_text import runtime_scope

    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None:
            return
        kind, inputs = run.workflow_type, run.input_json
        if "series_scope" in inputs or "producer_series_context" in inputs:
            from arclight.application.agent_workflows import WorkflowExecutionDeferred
            from arclight.application.model_gateway import lock_gate
            from arclight.application.producer_series_delivery import paused
            from arclight.persistence.agent_limits import AgentRunLimit

            lock_gate(db)
            control = db.get(AgentRunLimit, run.id)
            root = db.get(WorkflowRun, control.root_run_id) if control else None
            if root is not None and root.status == "RUNNING" and paused(db, root):
                raise WorkflowExecutionDeferred("Series Producer is paused; retain delivery")
    with runtime_scope(factory, settings, run_id, revision):
        # A delivery executes only its admitted business operation. A rejected
        # frozen contract must never send it through a different graph.
        if kind == "producer":
            from arclight.application.producer_execution import execute_producer as execute
        elif kind == "continuity_review":
            from arclight.application.continuity_execution import execute_continuity as execute
        elif kind == "art_proposal":
            from arclight.application.art_execution import execute_art as execute
        elif kind == "storyboard" and "director" in inputs:
            from arclight.application.director_execution import execute_director as execute
        elif (
            kind in {"script_storyboard", "story_outline", "script_rewrite", *DRAFT_WORKFLOWS}
            and "screenwriter" in inputs
        ):
            from arclight.application.screenwriter_execution import execute_screenwriter as execute
        else:
            _execute_run(factory, settings, run_id, revision)
            return
        if not execute(factory, settings, run_id, revision):
            from arclight.application.agent_workflows import fail_workflow

            fail_workflow(factory, run_id, revision, "PRODUCTION_VERSION_INCOMPATIBLE")


def _store_agent_state(run: WorkflowRun, state: AgentRunState) -> None:
    run.agent_state_json = state.model_dump(mode="json")


def _execute_run(factory: sessionmaker, settings: Settings, run_id: UUID, revision: int) -> None:
    # PostgreSQL 行锁覆盖整次图推进，重复消息不会并发执行相同 thread。
    # SQLite 仅用于单进程开发/测试，不提供生产级多 Worker 互斥保障。
    agent_scope: AgentRunScope | None = None
    agent_state: AgentRunState | None = None
    active_agent_step: AgentStepId | None = None
    with factory.begin() as db:
        run = db.scalar(
            select(WorkflowRun).where(WorkflowRun.id == run_id).with_for_update(key_share=True)
        )
        if run is None or run.status != RunStatus.QUEUED or run.revision != revision:
            return
        try:
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            agent_scope = scope_for_run(run, episode.project_id)
            agent_state = restore_agent_state(run.agent_state_json, None, agent_scope)
            settings = text_settings(db, episode.project_id, run, settings)
            from arclight.application.gateway_text import frozen_settings

            frozen_settings(settings)
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
                active_agent_step = (
                    "analyze" if run.workflow_type == "story_analysis" else "outline"
                )
                step_succeeded = any(
                    step.step_id == active_agent_step and step.status == "SUCCEEDED"
                    for step in agent_state.steps
                )
                if not step_succeeded and not any(
                    step.step_id == active_agent_step and step.status == "RUNNING"
                    for step in agent_state.steps
                ):
                    agent_state = transition_agent_step(
                        agent_state,
                        agent_scope,
                        expected_revision=agent_state.revision,
                        step_id=active_agent_step,
                        target="RUNNING",
                    )
                    run.agent_state_json = agent_state.model_dump(mode="json")
                artifact_id, version = generate(factory, settings, run)
                assert agent_state is not None and agent_scope is not None
                if step_succeeded:
                    saved_draft_id = next(
                        (
                            draft.artifact_id
                            for draft in agent_state.working_drafts
                            if draft.step_id == active_agent_step
                        ),
                        None,
                    )
                    if saved_draft_id != UUID(artifact_id):
                        raise AgentStateError(
                            "Completed Agent step does not match its saved artifact"
                        )
                else:
                    agent_state = transition_agent_step(
                        agent_state,
                        agent_scope,
                        expected_revision=agent_state.revision,
                        step_id=active_agent_step,
                        target="SUCCEEDED",
                        draft_id=UUID(artifact_id),
                        termination_reason="COMPLETED",
                    )
                    run.agent_state_json = agent_state.model_dump(mode="json")
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
            standard_version = {
                "script_storyboard": WORKFLOW_VERSION,
                SCRIPT_DRAFT: WORKFLOW_VERSION,
                "storyboard": DIRECTOR_WORKFLOW_VERSION,
                "script_rewrite": REWRITE_VERSION,
                SCRIPT_REWRITE_DRAFT: REWRITE_VERSION,
            }.get(run.workflow_type)
            if standard_version is None or run.workflow_version != standard_version:
                raise DomainError("PRODUCTION_VERSION_INCOMPATIBLE", "工作流版本不兼容", 409)
            rewrite = run.workflow_type in REWRITE_WORKFLOWS
            draft_only = run.workflow_type in DRAFT_WORKFLOWS
            director_only = run.workflow_type == "storyboard"
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
                agent_state = restore_agent_state(run.agent_state_json, None, agent_scope)

                def persist_agent_state(state: AgentRunState) -> None:
                    nonlocal agent_state
                    agent_state = state
                    _store_agent_state(run, state)

                if rewrite:
                    graph = build_rewrite_graph(
                        artifacts,
                        saver,
                        lambda inputs: generate_rewrite(
                            inputs,
                            run_settings(settings, run.input_json.get("_text_service"), db),
                            contexts,
                        ),
                        draft_only=draft_only,
                        contexts=contexts,
                        agent_scope=agent_scope,
                        agent_state_seed=agent_state,
                        agent_state_updated=persist_agent_state,
                    )
                else:
                    graph = build_graph(
                        bind_contexts(
                            build_provider(
                                run_settings(settings, run.input_json.get("_text_service"), db)
                            ),
                            contexts,
                        ),
                        artifacts,
                        saver,
                        director_only=director_only,
                        draft_only=draft_only,
                        contexts=contexts,
                        agent_scope=agent_scope,
                        agent_state_seed=agent_state,
                        agent_state_updated=persist_agent_state,
                    )
                config = {"configurable": {"thread_id": str(run.id)}}
                snapshot = graph.get_state(config)
                for name, frozen_value in public_startup(run.input_json).items():
                    if name in snapshot.values and snapshot.values[name] != frozen_value:
                        raise AgentStateError("Checkpoint changed a frozen startup input")
                agent_state = restore_agent_state(
                    run.agent_state_json, snapshot.values, agent_scope
                )
                run.agent_state_json = agent_state.model_dump(mode="json")
                stage = None
                if not snapshot.values:
                    stage = "rewrite" if rewrite else "storyboard" if director_only else "script"
                elif "director" in snapshot.next or (
                    run.resume_json
                    and run.resume_json.get("resource_type") == "script"
                    and run.resume_json.get("decision") == "APPROVE"
                    and not rewrite
                ):
                    stage = "storyboard"
                elif "screenwriter" in snapshot.next:
                    stage = "script"
                elif "rewrite" in snapshot.next:
                    stage = "rewrite"
                if stage and contexts:
                    with factory.begin() as trace_db:
                        selected = consume_context(
                            trace_db, episode.project_id, run, stage, settings
                        )
                    contexts[stage] = selected[stage]
                step_by_stage: dict[str, AgentStepId] = {
                    "script": "script",
                    "storyboard": "storyboard",
                    "rewrite": "rewrite",
                }
                active_agent_step = step_by_stage.get(stage) if stage is not None else None
                value: dict | Command | None = None
                if not snapshot.values:
                    inputs = public_startup(run.input_json)
                    if contexts:
                        inputs["context_refs"] = {
                            name: context["trace_ref"]
                            for name, context in contexts.items()
                            if stage == name and "trace_ref" in context
                        }
                    value = {
                        "run_id": str(run.id),
                        "agent_state": agent_state.model_dump(mode="json"),
                        **inputs,
                    }
                elif snapshot.interrupts and run.resume_json:
                    pending = snapshot.interrupts[0].value
                    if (
                        pending["version_id"] == run.resume_json["version_id"]
                        and pending["resource_type"] == run.resume_json["resource_type"]
                    ):
                        value = Command(resume=run.resume_json)
                graph.invoke(value, config, durability="sync")
                snapshot = graph.get_state(config)
                agent_state = restore_agent_state(
                    run.agent_state_json, snapshot.values, agent_scope
                )
                run.agent_state_json = agent_state.model_dump(mode="json")
                run.resume_json = None
                run.revision += 1
                if snapshot.interrupts:
                    run.status = RunStatus.WAITING_REVIEW
                    run.pending_review = snapshot.interrupts[0].value
                    from arclight.application.text_reviews import pause_for_run

                    pause_for_run(db, run)
                    emit(db, run, "workflow.waiting_review", run.pending_review)
                else:
                    complete = snapshot.values.get(
                        "script_version_id"
                        if draft_only
                        else "script_approved"
                        if rewrite
                        else "storyboard_approved",
                        False,
                    )
                    run.status = RunStatus.SUCCEEDED if complete else RunStatus.REJECTED
                    run.pending_review = None
                    receipt: dict[str, str] = {"status": run.status}
                    if draft_only and complete:
                        receipt["script_version_id"] = str(complete)
                    emit(db, run, "workflow.finished", receipt)
        except Exception as exc:
            if isinstance(exc, DomainError) and exc.code == "PRODUCER_PAUSED":
                from arclight.application.agent_workflows import WorkflowExecutionDeferred

                raise WorkflowExecutionDeferred("Series Producer is paused") from exc
            # 不把供应商响应、凭证、输入正文或内部路径写到业务错误响应。
            logger.error(
                "workflow_failed run_id=%s error_type=%s code=%s agent_state_error=%s",
                run_id,
                type(exc).__name__,
                exc.code if isinstance(exc, DomainError) else "EXECUTION_FAILED",
                str(exc) if isinstance(exc, AgentStateError) else None,
            )
            balance_error = (
                isinstance(exc, DomainError) and exc.code == "ACCOUNT_BALANCE_INSUFFICIENT"
            )
            if not balance_error and agent_state is not None and agent_scope is not None:
                try:
                    agent_state = terminate_agent_state(
                        agent_state,
                        agent_scope,
                        reason="EXECUTION_FAILED",
                        step_id=active_agent_step,
                    )
                    run.agent_state_json = agent_state.model_dump(mode="json")
                except AgentStateError:
                    logger.error("workflow_agent_state_rejected run_id=%s", run_id)
            run.status = RunStatus.FAILED
            run.error_code = (
                exc.code
                if isinstance(exc, DomainError)
                and (
                    exc.code == "ACCOUNT_BALANCE_INSUFFICIENT"
                    or exc.code.startswith(("GATEWAY_", "BUDGET_", "SERVICE_", "KEY_STORAGE_"))
                    or exc.code
                    in {
                        "PRODUCTION_VERSION_INCOMPATIBLE",
                        "CONTEXT_SNAPSHOT_INVALID",
                        "CONTEXT_TOO_LARGE",
                        "CONTEXT_INPUT_UNSAFE",
                        "CONTEXT_REFERENCE_INVALID",
                        "SCRIPT_DURATION_REQUIRED",
                        "SCRIPT_DURATION_OUT_OF_RANGE",
                        "STORYBOARD_DURATION_OUT_OF_RANGE",
                    }
                )
                else "WORKFLOW_EXECUTION_FAILED"
            )
            if not balance_error:
                run.revision += 1
            emit(db, run, "workflow.failed", {"code": run.error_code})
