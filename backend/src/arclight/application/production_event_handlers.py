"""Stage adapters perform bounded database work; paid or external execution stays in workers."""

from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.production_versions import find_binding
from arclight.core.errors import DomainError
from arclight.domain.production_events import StageAction, StageEvent
from arclight.domain.production_state import ConditionName, EpisodeProductionState
from arclight.persistence.models import AuditEvent, OutboxEvent, Review, WorkflowEvent, WorkflowRun


@dataclass(frozen=True)
class StagePlan:
    action: StageAction
    required_conditions: tuple[ConditionName, ...] = ()
    maximum_cost: Decimal = Decimal("0")


class StageHandler(Protocol):
    def prepare(
        self, db: Session, event: StageEvent, state: EpisodeProductionState
    ) -> StagePlan: ...

    def advance(
        self, db: Session, event: StageEvent, plan: StagePlan, state: EpisodeProductionState
    ) -> None: ...


class TextStageHandler:
    def prepare(self, db: Session, event: StageEvent, state: EpisodeProductionState) -> StagePlan:
        run = db.get(WorkflowRun, event.source_id)
        assert run is not None
        if run.status == "QUEUED" and run.resume_json:
            if find_binding(db, event.project_id, "WORKFLOW", run.id) is None:
                raise DomainError("STAGE_INPUT_UNBOUND", "旧运行缺少精确输入绑定", 409)
            decision = run.resume_json
            reviewed = db.scalar(
                select(Review.id).where(
                    Review.run_id == run.id,
                    Review.version_id == UUID(decision["version_id"]),
                    Review.resource_type == decision["resource_type"],
                    Review.decision == decision["decision"],
                )
            )
            if reviewed is None:
                raise DomainError("STAGE_REVIEW_MISSING", "阶段缺少精确审核记录", 409)
            exact = (
                state.script_version_id
                if decision["resource_type"] == "script"
                else state.storyboard_version_id
            )
            if str(exact) != str(decision["version_id"]):
                raise DomainError("STAGE_REVIEW_STALE", "阶段审核对象已变化", 409)
            conditions: tuple[ConditionName, ...] = ()
            if decision["decision"] == "APPROVE":
                conditions = (
                    "ASSETS_USABLE",
                    "SCRIPT_APPROVED"
                    if decision["resource_type"] == "script"
                    else "STORYBOARD_APPROVED",
                )
            return StagePlan("CONTINUE_TEXT", required_conditions=conditions)
        if run.status == "WAITING_REVIEW":
            return StagePlan("WAIT_REVIEW")
        if run.status in {"FAILED", "REJECTED", "CANCELLED"}:
            return StagePlan("PARTIAL_FAILURE" if state.has_partial_failure else "OBSERVED")
        return StagePlan("OBSERVED")

    def advance(
        self, db: Session, event: StageEvent, plan: StagePlan, state: EpisodeProductionState
    ) -> None:
        if plan.action == "CONTINUE_TEXT":
            # Reuse the original logical execution message. Models run only in the
            # existing fenced runtime, which rechecks its own exact inputs and budget.
            message = db.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.run_id == event.source_id,
                    OutboxEvent.revision == event.source_revision,
                )
            )
            if message is None:
                db.add(OutboxEvent(run_id=event.source_id, revision=event.source_revision))
        db.add(
            WorkflowEvent(
                run_id=event.source_id,
                event_type="production.reconciled",
                payload_json={
                    "event_id": str(event.event_id),
                    "phase": state.phase,
                    "action": plan.action,
                },
            )
        )


class MediaStageHandler:
    def prepare(self, db: Session, event: StageEvent, state: EpisodeProductionState) -> StagePlan:
        from arclight.persistence.generation import GenerationJob

        job = db.get(GenerationJob, event.source_id)
        assert job is not None
        action: StageAction = "PARTIAL_FAILURE" if state.has_partial_failure else "WAIT_MEDIA"
        if job.status in {"RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW", "CANCEL_REQUESTED"}:
            action = "WAIT_RECOVERY"
        elif job.status in {"FAILED", "CANCELLED"} and not state.has_partial_failure:
            action = "OBSERVED"
        return StagePlan(action)

    def advance(
        self, db: Session, event: StageEvent, plan: StagePlan, state: EpisodeProductionState
    ) -> None:
        # File import and recovery remain on GEN-12 / GEN-06's durable paths.
        # Success cannot invent a shot association, continuity decision or selection.
        db.add(
            AuditEvent(
                project_id=event.project_id,
                actor_id=event.actor_id,
                action="production.media_reconciled",
                resource_id=event.source_id,
                details_json={
                    "event_id": str(event.event_id),
                    "phase": state.phase,
                    "action": plan.action,
                },
            )
        )


class ReviewStageHandler:
    """Publish the route destination; domain executors retain their own entry points."""

    def prepare(self, db: Session, event: StageEvent, state: EpisodeProductionState) -> StagePlan:
        from arclight.application.continuity_routing import read_routed_review

        record, route = read_routed_review(db, event.source_id)
        if (route.event_id, route.routed_by, record.review.episode_id) != (
            event.event_id,
            event.actor_id,
            event.episode_id,
        ):
            raise DomainError("REVIEW_ROUTE_INVALID", "事件不能更换原审查路由或发起者", 409)
        return StagePlan(route.destination)

    def advance(
        self, db: Session, event: StageEvent, plan: StagePlan, state: EpisodeProductionState
    ) -> None:
        if plan.action == "HUMAN_REVIEW":
            from arclight.application.continuity_escalations import request_escalation
            from arclight.application.continuity_routing import read_routed_review

            record, route = read_routed_review(db, event.source_id)
            request_escalation(db, route.run_id, route.routed_by, record.review.target)
        db.add(
            AuditEvent(
                project_id=event.project_id,
                actor_id=event.actor_id,
                action="production.review_routed",
                resource_id=event.source_id,
                details_json={"event_id": str(event.event_id), "destination": plan.action},
            )
        )


HANDLERS: dict[str, StageHandler] = {
    "TEXT": TextStageHandler(),
    "MEDIA": MediaStageHandler(),
    "REVIEW": ReviewStageHandler(),
}
