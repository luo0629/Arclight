"""Art-domain integration over the existing loop journal, leases and review checkpoint."""

import logging
from uuid import UUID

from langgraph.types import Command
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import AgentLoopStore, claim_loop, keep_loop_alive
from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.agent_workflows import (
    WorkflowExecutionDeferred,
    active_workflow,
    creation_loops,
    fail_workflow,
    wait_for_agent,
)
from arclight.application.art_direction import resume_art_direction
from arclight.application.art_generation import generate_art_proposal, publish_art_direction
from arclight.application.art_workflow import delivered, review_target, saved_review
from arclight.application.commands import emit
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.production_versions import text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_state import AgentStepStatus, restore_agent_state, transition_agent_step
from arclight.domain.art_direction import ArtDirectionTask
from arclight.domain.art_generation import ArtDelivery
from arclight.domain.art_workflow import ART_WORKFLOW_VERSION
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, WorkflowRun
from arclight.workflows.art import build_art_graph
from arclight.workflows.checkpoints import open_checkpointer

logger = logging.getLogger(__name__)


def _review_graph(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    revision: int,
    delivery: ArtDelivery,
) -> None:
    with factory.begin() as db:
        run = active_workflow(db, run_id, revision)
        loop = db.get(AgentLoop, delivery.loop_id, populate_existing=True)
        if loop is None or loop.status != "COMPLETED" or delivered(db, run.id) != delivery:
            raise DomainError("ART_DELIVERY_INVALID", "原美术候选交付不存在", 409)
        token = claim_loop(db, loop)
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        scope = scope_for_run(run, episode.project_id)
        resume = run.resume_json
    store = AgentLoopStore(factory, delivery.loop_id, token)
    with keep_loop_alive(store), open_checkpointer(settings) as saver:

        def current(db: Session) -> WorkflowRun:
            store.guard(db, execution=False)
            return active_workflow(db, run_id, revision)

        def proposal() -> dict:
            with factory.begin() as db:
                run = current(db)
                if delivered(db, run.id) != delivery:
                    raise DomainError("ART_DELIVERY_INVALID", "提案节点须保留原不可变交付", 409)
                state = restore_agent_state(run.agent_state_json, None, scope)
                if next(s for s in state.steps if s.step_id == "storyboard").status != "SUCCEEDED":
                    state = transition_agent_step(
                        state,
                        scope,
                        expected_revision=state.revision,
                        step_id="storyboard",
                        target="SUCCEEDED",
                        draft_id=delivery.version_id,
                    )
                elif not any(
                    d.artifact_id == delivery.version_id and d.step_id == "storyboard"
                    for d in state.working_drafts
                ):
                    raise DomainError("ART_DELIVERY_INVALID", "已完成节点不能更换提案版本", 409)
                review = next(s for s in state.steps if s.step_id == "storyboard_review")
                if review.status == "PENDING":
                    state = transition_agent_step(
                        state,
                        scope,
                        expected_revision=state.revision,
                        step_id="storyboard_review",
                        target="WAITING_REVIEW",
                    )
                run.agent_state_json = state.model_dump(mode="json")
            return delivery.model_dump(mode="json")

        def review(value: dict) -> bool:
            with factory.begin() as db:
                run = current(db)
                approved = saved_review(db, delivery, value)
                state = restore_agent_state(run.agent_state_json, None, scope)
                target: AgentStepStatus = "SUCCEEDED" if approved else "REJECTED"
                if (
                    next(s for s in state.steps if s.step_id == "storyboard_review").status
                    != target
                ):
                    state = transition_agent_step(
                        state,
                        scope,
                        expected_revision=state.revision,
                        step_id="storyboard_review",
                        target=target,
                        termination_reason="COMPLETED" if approved else "REVIEW_REJECTED",
                    )
                    run.agent_state_json = state.model_dump(mode="json")
            return approved

        graph = build_art_graph(saver, proposal, review)
        config = {"configurable": {"thread_id": str(run_id)}}
        snapshot = graph.get_state(config)
        frozen = {"run_id": str(run_id), "input_sha256": scope.input_sha256}
        if any(snapshot.values.get(k, v) != v for k, v in frozen.items()) or (
            "delivery" in snapshot.values
            and snapshot.values["delivery"] != delivery.model_dump(mode="json")
        ):
            raise DomainError("ART_INPUT_CHANGED", "审核图与原运行或精确提案不一致", 409)
        graph.invoke(frozen if not snapshot.values else None, config, durability="sync")
        snapshot = graph.get_state(config)
        if snapshot.interrupts and resume:
            if snapshot.interrupts[0].value != review_target(delivery):
                raise DomainError("ART_REVIEW_STALE", "审核节点与原精确候选不一致", 409)
            graph.invoke(Command(resume=resume), config, durability="sync")
            snapshot = graph.get_state(config)
        with factory.begin() as db:
            run = current(db)
            if snapshot.interrupts:
                if snapshot.interrupts[0].value != review_target(delivery):
                    raise DomainError("ART_REVIEW_STALE", "审核节点与原精确候选不一致", 409)
                run.status = "WAITING_REVIEW"
                run.pending_review = review_target(delivery)
                emit(db, run, "workflow.waiting_review", run.pending_review)
            else:
                approved = saved_review(db, delivery, resume or {})
                if snapshot.values.get("approved") is not approved:
                    raise DomainError("ART_REVIEW_STALE", "审核图不能替代业务决定", 409)
                run.status = "SUCCEEDED" if approved else "REJECTED"
                run.pending_review = None
                emit(
                    db,
                    run,
                    "workflow.finished",
                    {"status": run.status, "proposal_version_id": str(delivery.version_id)},
                )
            run.resume_json, run.error_code = None, None
            run.revision += 1


def execute_art(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> bool:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.workflow_version != ART_WORKFLOW_VERSION:
            return False
    try:
        with factory.begin() as db:
            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            assert run is not None
            if run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return True
            validate_publication(db, run.id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            settings = text_settings(db, episode.project_id, run, settings)
            scope = scope_for_run(run, episode.project_id)
            state = restore_agent_state(run.agent_state_json, None, scope)
            if next(s for s in state.steps if s.step_id == "storyboard").status == "PENDING":
                state = transition_agent_step(
                    state,
                    scope,
                    expected_revision=state.revision,
                    step_id="storyboard",
                    target="RUNNING",
                )
                run.agent_state_json = state.model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
            run.status = "RUNNING"
            actor = execution_actor(db, run)
            task = ArtDirectionTask.model_validate(run.input_json["art_direction"]["task"])
            limits = AgentRunLimits.model_validate(run.input_json["art_direction"]["agent_limits"])
            delivery = delivered(db, run.id)
            loops = creation_loops(db, run.id, "art_director")
        if delivery is None:
            if loops:
                outcome = resume_art_direction(factory, settings, loops[0].id, actor)
                if wait_for_agent(factory, run_id, revision, outcome, "art_director"):
                    return True
                delivery = publish_art_direction(factory, settings, loops[0].id, actor)
            else:
                generated = generate_art_proposal(
                    factory, settings, run_id, actor, task, limits=limits
                )
                if wait_for_agent(factory, run_id, revision, generated.direction, "art_director"):
                    return True
                delivery = generated.delivery
        assert delivery is not None
        _review_graph(factory, settings, run_id, revision, delivery)
    except DomainError as error:
        if error.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST"}:
            raise WorkflowExecutionDeferred(error.code) from None
        if error.code == "SCREENWRITER_DELIVERY_STALE":
            return True
        fail_workflow(factory, run_id, revision, error.code)
    except Exception as error:
        logger.error("art_failed run_id=%s error_type=%s", run_id, type(error).__name__)
        fail_workflow(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
    return True
