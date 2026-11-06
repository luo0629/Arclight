"""Short business transactions around professional creation and the existing review graph."""

import logging
from uuid import UUID

from langgraph.types import Command
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import AgentLoopStore, claim_loop, keep_loop_alive
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.artifacts import ArtifactStore, save_script_candidate
from arclight.application.commands import emit
from arclight.application.context_consumption import consume_context
from arclight.application.context_snapshots import read_contexts
from arclight.application.context_sources import public_startup
from arclight.application.dependencies import lock_dependency_graph
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import run_settings
from arclight.application.production_versions import text_settings
from arclight.application.screenwriter_creation import (
    PROFESSIONAL_VERSIONS,
    assemble_script,
    create_fragment,
    create_plan,
)
from arclight.application.story_outline import save_outline_candidate
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_state import (
    AgentRunState,
    AgentStateError,
    AgentStepId,
    restore_agent_state,
    terminate_agent_state,
    transition_agent_step,
)
from arclight.domain.screenwriter_creation import SCREENWRITER_OUTLINE_VERSION, ScriptFragment
from arclight.domain.screenwriter_planning import EpisodePlan, SeasonPlan
from arclight.domain.screenwriter_revision import REVISION_WORKFLOWS
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, Script, WorkflowEvent, WorkflowRun
from arclight.providers.context import bind_contexts
from arclight.providers.text import build_provider
from arclight.workflows.checkpoints import open_checkpointer
from arclight.workflows.production import build_graph

logger = logging.getLogger(__name__)


class ScreenwriterExecutionDeferred(RuntimeError):
    """Keep a broker delivery recoverable while an existing worker lease is alive."""


def _active(db: Session, run_id: UUID, revision: int) -> WorkflowRun:
    lock_gate(db)
    run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
    if run is None or run.status != "RUNNING" or run.revision != revision:
        raise DomainError("SCREENWRITER_DELIVERY_STALE", "此执行投递已失效", 409)
    validate_publication(db, run_id)
    return run


def _waiting(
    factory: sessionmaker[Session], run_id: UUID, revision: int, outcome: AgentLoopResult
) -> bool:
    if outcome.status == "COMPLETED":
        return False
    if outcome.status == "STOPPED":
        raise DomainError(
            outcome.stop_code or "SCREENWRITER_STOPPED", "专业编剧已停止，请查看问题或显式重试", 409
        )
    with factory.begin() as db:
        run = _active(db, run_id, revision)
        previous = db.scalars(
            select(WorkflowEvent).where(
                WorkflowEvent.run_id == run.id, WorkflowEvent.event_type == "screenwriter.question"
            )
        )
        if not any(
            event.payload_json.get("question_id") == str(outcome.question_id) for event in previous
        ):
            emit(
                db,
                run,
                "screenwriter.question",
                {"loop_id": str(outcome.loop_id), "question_id": str(outcome.question_id)},
            )
    return True


def _finish_outline(factory: sessionmaker[Session], run_id: UUID, revision: int, outcome) -> None:
    assert outcome.result is not None and isinstance(outcome.result.plan, SeasonPlan)
    with factory.begin() as db:
        run = _active(db, run_id, revision)
        artifact_id, version = save_outline_candidate(db, run, outcome.result.plan.outline)
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        scope = scope_for_run(run, episode.project_id)
        state = restore_agent_state(run.agent_state_json, None, scope)
        run.agent_state_json = transition_agent_step(
            state,
            scope,
            expected_revision=state.revision,
            step_id="outline",
            target="SUCCEEDED",
            draft_id=UUID(artifact_id),
            termination_reason="COMPLETED",
        ).model_dump(mode="json")
        run.status, run.error_code, run.pending_review = "SUCCEEDED", None, None
        run.revision += 1
        emit(
            db,
            run,
            "workflow.finished",
            {"status": run.status, "outline_id": artifact_id, "version_no": version},
        )


def _review_graph(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> None:
    # The completed plan journal supplies a fenced lease for the original review graph.
    # No transaction or row lock spans graph execution / provider I/O.
    with factory.begin() as db:
        run = _active(db, run_id, revision)
        assert run.agent_state_json is not None
        plan_ref = run.agent_state_json["extensions"]["screenwriter.planning"]
        loop = db.get(AgentLoop, UUID(plan_ref["loop_id"]))
        assert loop is not None and loop.status == "COMPLETED"
        token = claim_loop(db, loop)
        loop_id = loop.id
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        project_id = episode.project_id
        scope = scope_for_run(run, project_id)
        contexts = read_contexts(db, project_id, run)
        settings = text_settings(db, project_id, run, settings)
        provider_settings = run_settings(settings, run.input_json.get("_text_service"), db)
        inputs, resume = public_startup(run.input_json), run.resume_json
        state = restore_agent_state(run.agent_state_json, None, scope)
    store = AgentLoopStore(factory, loop_id, token)
    with keep_loop_alive(store), open_checkpointer(settings) as saver:

        def save_state(updated: AgentRunState) -> None:
            with factory.begin() as db:
                store.guard(db, execution=False)
                current = _active(db, run_id, revision)
                current.agent_state_json = updated.model_dump(mode="json")

        artifacts = ArtifactStore(
            factory,
            run_id,
            run.episode_id,
            inputs["target_duration_ms"],
            settings.text_duration_tolerance_percent,
        )
        if run.workflow_type == "script_rewrite":
            from arclight.workflows.rewrite import build_rewrite_graph

            def already_saved(_inputs: dict) -> dict:
                raise DomainError("SCREENWRITER_INCOMPLETE", "终稿尚未保存，不能进入审核", 409)

            graph = build_rewrite_graph(
                artifacts,
                saver,
                already_saved,
                contexts=contexts,
                agent_scope=scope,
                agent_state_seed=state,
                agent_state_updated=save_state,
            )
        else:
            graph = build_graph(
                bind_contexts(build_provider(provider_settings), contexts),
                artifacts,
                saver,
                contexts=contexts,
                agent_scope=scope,
                agent_state_seed=state,
                agent_state_updated=save_state,
            )
        config = {"configurable": {"thread_id": str(run_id)}}
        snapshot = graph.get_state(config)
        for name, frozen in inputs.items():
            if name in snapshot.values and snapshot.values[name] != frozen:
                raise DomainError("AGENT_INPUT_CHANGED", "审核图冻结输入已变化", 409)
        if (
            run.workflow_type != "script_rewrite"
            and contexts
            and ("director" in snapshot.next or (resume and resume.get("decision") == "APPROVE"))
        ):
            with factory.begin() as db:
                current = _active(db, run_id, revision)
                selected = consume_context(db, project_id, current, "storyboard", settings)
                contexts["storyboard"] = selected["storyboard"]
        value: dict | Command | None = None
        if not snapshot.values:
            value = {"run_id": str(run_id), "agent_state": state.model_dump(mode="json"), **inputs}
        elif snapshot.interrupts and resume:
            pending = snapshot.interrupts[0].value
            if (pending["version_id"], pending["resource_type"]) != (
                resume["version_id"],
                resume["resource_type"],
            ):
                raise DomainError("AGENT_STALE_DECISION", "审核对象与原图不一致", 409)
            value = Command(resume=resume)
        graph.invoke(value, config, durability="sync")
        snapshot = graph.get_state(config)
        with factory.begin() as db:
            store.guard(db, execution=False)
            current = _active(db, run_id, revision)
            current.agent_state_json = restore_agent_state(
                current.agent_state_json, snapshot.values, scope
            ).model_dump(mode="json")
            current.resume_json, current.error_code = None, None
            current.revision += 1
            if snapshot.interrupts:
                from arclight.application.text_reviews import pause_for_run

                current.status = "WAITING_REVIEW"
                current.pending_review = snapshot.interrupts[0].value
                pause_for_run(db, current)
                emit(db, current, "workflow.waiting_review", current.pending_review)
            else:
                current.status = (
                    "SUCCEEDED"
                    if snapshot.values.get(
                        "script_approved"
                        if run.workflow_type == "script_rewrite"
                        else "storyboard_approved"
                    )
                    else "REJECTED"
                )
                current.pending_review = None
                emit(db, current, "workflow.finished", {"status": current.status})


def execute_screenwriter(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> bool:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.workflow_version not in PROFESSIONAL_VERSIONS:
            return False
    try:
        with factory.begin() as db:
            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            assert run is not None
            if run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return True
            validate_publication(db, run_id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            settings = text_settings(db, episode.project_id, run, settings)
            scope = scope_for_run(run, episode.project_id)
            state = restore_agent_state(run.agent_state_json, None, scope)
            stage: AgentStepId = (
                "outline"
                if run.workflow_version == SCREENWRITER_OUTLINE_VERSION
                else "rewrite"
                if run.workflow_type == "script_rewrite"
                else "script"
            )
            if next(step for step in state.steps if step.step_id == stage).status == "PENDING":
                state = transition_agent_step(
                    state, scope, expected_revision=state.revision, step_id=stage, target="RUNNING"
                )
                run.agent_state_json = state.model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
            run.status = "RUNNING"
            actor = execution_actor(db, run)
            candidate = (
                db.scalar(select(Script.id).where(Script.run_id == run.id))
                if stage in {"script", "rewrite"}
                else None
            )
        if candidate is None:
            outcome = create_plan(factory, settings, run_id, actor)
            if _waiting(factory, run_id, revision, outcome):
                return True
            if stage == "outline":
                _finish_outline(factory, run_id, revision, outcome)
                return True
            assert outcome.loop_id is not None and outcome.result is not None
            if stage == "rewrite":
                from arclight.agents.contracts import ScriptContent

                content = ScriptContent.model_validate(
                    run.input_json["rewrite_input"]["source_script"]
                )
            else:
                assert isinstance(outcome.result.plan, EpisodePlan)
                fragments: list[ScriptFragment] = []
                for scene_no in range(1, len(outcome.result.plan.scenes) + 1):
                    fragment = create_fragment(
                        factory,
                        settings,
                        run_id,
                        outcome.loop_id,
                        outcome.result,
                        scene_no,
                        fragments,
                    )
                    if _waiting(factory, run_id, revision, fragment):
                        return True
                    assert fragment.result is not None
                    fragments.append(fragment.result)
                content = assemble_script(outcome.loop_id, outcome.result, fragments)
            if run.workflow_version in REVISION_WORKFLOWS:
                from arclight.application.screenwriter_revision import revise_script

                evaluated, final_content = revise_script(
                    factory, settings, run_id, outcome.result, content
                )
                if _waiting(factory, run_id, revision, evaluated):
                    return True
                assert final_content is not None
                content = final_content
            with factory.begin() as db:
                run = _active(db, run_id, revision)
                lock_dependency_graph(db, scope.project_id)
                existing = db.scalar(select(Script.id).where(Script.run_id == run.id))
                if existing is None:
                    save_script_candidate(
                        db,
                        run.id,
                        run.episode_id,
                        content,
                        run.input_json["target_duration_ms"],
                        settings.text_duration_tolerance_percent,
                    )
        _review_graph(factory, settings, run_id, revision)
    except DomainError as exc:
        if exc.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST"}:
            raise ScreenwriterExecutionDeferred(exc.code) from None
        if exc.code == "SCREENWRITER_DELIVERY_STALE":
            return True
        if exc.code == "AGENT_NOT_ACTIVE":
            with factory() as db:
                # Another delivery may have handed the completed script to review.
                if db.scalar(select(Script.id).where(Script.run_id == run_id)) is not None:
                    return True
        _fail(factory, run_id, revision, exc.code)
    except Exception as exc:
        logger.error("screenwriter_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        _fail(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
    return True


def _fail(factory: sessionmaker[Session], run_id: UUID, revision: int, code: str) -> None:
    with factory.begin() as db:
        lock_gate(db)
        run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
        if run is None or run.status not in {"QUEUED", "RUNNING"} or run.revision != revision:
            return
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        stopped = code == "AGENT_HUMAN_STOPPED"
        if run.agent_state_json is not None:
            try:
                state = AgentRunState.model_validate(run.agent_state_json)
                failed_step = next(
                    (
                        step.step_id
                        for step in state.steps
                        if step.status in {"RUNNING", "WAITING_REVIEW"}
                    ),
                    None,
                )
                run.agent_state_json = terminate_agent_state(
                    state,
                    scope_for_run(run, episode.project_id),
                    reason="CANCELLED" if stopped else "EXECUTION_FAILED",
                    step_id=failed_step,
                ).model_dump(mode="json")
            except AgentStateError:
                logger.error("screenwriter_state_invalid run_id=%s", run_id)
        run.status, run.error_code = ("CANCELLED", None) if stopped else ("FAILED", code)
        run.revision += 1
        emit(db, run, "workflow.cancelled" if stopped else "workflow.failed", {"code": code})
