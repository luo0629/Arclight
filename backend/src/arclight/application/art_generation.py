"""Atomic delivery from the real art loop to existing proposal versions and reviews."""

from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_store import checked_loop
from arclight.application.art_direction import propose_art, validate_art_direction
from arclight.application.art_direction_evidence import direction_loop, operation_sha256
from arclight.application.art_proposal_store import save_proposal
from arclight.application.art_reviews import selection, submit_art
from arclight.application.audit import audit
from arclight.application.commands import require_run
from arclight.application.director_research import approved_input
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.art_direction import ArtDirectionDraft, ArtDirectionResult, ArtDirectionTask
from arclight.domain.art_generation import (
    DELIVERY_KEY,
    DELIVERY_KIND,
    ArtDelivery,
    ArtDeliveryInput,
    ArtGenerationResult,
)
from arclight.domain.art_proposals import ArtProposal
from arclight.domain.art_review import ArtSave, ArtSubmit
from arclight.domain.human_reviews import ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.art_proposals import ArtProposalRecord, ArtProposalVersion
from arclight.persistence.human_reviews import HumanReviewRequest
from arclight.persistence.models import Episode


def _saved_delivery(db: Session, loop: AgentLoop, action: AgentLoopAction) -> ArtDelivery:
    try:
        delivery = ArtDelivery.model_validate(action.result_json)
        provenance = ArtDeliveryInput.model_validate(action.request_json)
        row = db.get(ArtProposalRecord, delivery.proposal_id)
        version = db.get(ArtProposalVersion, delivery.version_id)
        review = db.get(HumanReviewRequest, delivery.review_request_id)
        if row is None or version is None or review is None:
            raise ValueError("Missing delivered business records")
        frozen = ReviewSnapshot.model_validate(review.snapshot_json)
        chosen = selection(db, version.id)
        _, seed = direction_loop(db, loop.run_id)
        direction = ArtDirectionResult(
            draft=ArtDirectionDraft(
                proposal=ArtProposal.model_validate(version.proposal_json),
                evidence=provenance.evidence,
                assessments=provenance.assessments,
            ),
            comparison_sha256=provenance.comparison_sha256,
        )
        if (
            action.kind != DELIVERY_KIND
            or action.request_sha256 != digest(action.request_json)
            or delivery.loop_id != loop.id
            or delivery.run_id != loop.run_id
            or delivery.project_id != loop.project_id
            or provenance.input_sha256 != loop.input_sha256
            or provenance.agent_request_sha256 != loop.request_sha256
            or provenance.seed.project_id != loop.project_id
            or provenance.seed.episode_id != delivery.episode_id
            or row.project_id != loop.project_id
            or version.proposal_id != row.id
            or version.creation_key != loop.id
            or frozen.version_id != version.id
            or frozen.object_id != row.id
            or frozen.project_id != loop.project_id
            or frozen.episode_id != delivery.episode_id
            or review.handler != "ART_PROPOSAL"
            or review.project_id != loop.project_id
            or review.episode_id != delivery.episode_id
            or chosen.option_id != delivery.option_id
            or chosen.comparison_sha256 != provenance.comparison_sha256
            or provenance.seed != seed
            or provenance.direction_sha256 != delivery.direction_sha256
            or digest(direction.model_dump(mode="json")) != delivery.direction_sha256
        ):
            raise ValueError("Inconsistent art delivery receipt")
    except (ValidationError, ValueError) as error:
        raise DomainError("ART_DELIVERY_INVALID", "美术交付回执与原精确候选不一致", 409) from error
    return delivery


def publish_art_direction(
    factory: sessionmaker[Session], settings: Settings, loop_id: UUID, actor: str
) -> ArtDelivery:
    """Deliver a server-held final result once, without another model or formal asset write."""
    with factory.begin() as db:
        lock_gate(db)
        loop = db.get(AgentLoop, loop_id, populate_existing=True)
        if loop is None:
            raise DomainError("NOT_FOUND", "美术决策循环不存在", 404)
        run = require_run(db, loop.run_id, actor, lock=True)
        validate_publication(db, run.id)
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        if (
            actor != loop.actor_id
            or actor != execution_actor(db, run)
            or episode.project_id != loop.project_id
            or loop.operation_sha256 != operation_sha256(run.id)
        ):
            raise DomainError("ART_DIRECTION_SCOPE", "只能交付原操作者和范围内的美术决策", 403)
        prior = db.scalar(
            select(AgentLoopAction).where(
                AgentLoopAction.loop_id == loop.id, AgentLoopAction.action_key == DELIVERY_KEY
            )
        )
        if prior is not None:
            return _saved_delivery(db, loop, prior)
        if loop.status != "COMPLETED" or loop.outcome_json is None:
            raise DomainError("ART_DELIVERY_NOT_READY", "美术决策尚未完整交付，不能创建候选", 409)
        loop = checked_loop(db, loop.id, actor=actor)
        try:
            outcome = AgentLoopResult[ArtDirectionResult].model_validate(loop.outcome_json)
        except ValidationError as error:
            raise DomainError("ART_DELIVERY_INVALID", "美术决策结果不符合原合同", 409) from error
        if (
            outcome.status != "COMPLETED"
            or outcome.loop_id != loop.id
            or outcome.run_id != run.id
            or outcome.project_id != loop.project_id
            or outcome.agent_name != "art_director"
            or outcome.stage != "storyboard"
            or outcome.input_sha256 != loop.input_sha256
            or outcome.result is None
        ):
            raise DomainError("ART_DELIVERY_INVALID", "美术决策结果与运行范围不一致", 409)
        result = outcome.result
        validate_art_direction(db, settings, run.id, result)
        _, seed = direction_loop(db, run.id)
        _, trace = approved_input(db, run, actor, settings)
        row, version, _ = save_proposal(
            db,
            loop.project_id,
            actor,
            ArtSave(creation_key=loop.id, proposal=result.draft.proposal),
        )
        option_id = (
            result.draft.proposal.preferred_option_id or result.draft.proposal.options[0].option_id
        )
        review = submit_art(
            db,
            loop.project_id,
            row.id,
            actor,
            ArtSubmit(
                version_id=version.id,
                expected_revision=row.revision,
                option_id=option_id,
                comparison_sha256=result.comparison_sha256,
            ),
        )
        checksum = digest(result.model_dump(mode="json"))
        provenance = ArtDeliveryInput(
            seed=seed,
            input_sha256=loop.input_sha256,
            agent_request_sha256=loop.request_sha256,
            startup_manifest_id=UUID(trace["startup_manifest_id"]),
            direction_sha256=checksum,
            evidence=result.draft.evidence,
            assessments=result.draft.assessments,
            comparison_sha256=result.comparison_sha256,
        ).model_dump(mode="json")
        delivery = ArtDelivery(
            project_id=loop.project_id,
            episode_id=episode.id,
            run_id=run.id,
            loop_id=loop.id,
            proposal_id=row.id,
            version_id=version.id,
            review_request_id=review.id,
            option_id=option_id,
            direction_sha256=checksum,
        )
        db.add(
            AgentLoopAction(
                id=uuid5(loop.id, DELIVERY_KEY),
                loop_id=loop.id,
                action_key=DELIVERY_KEY,
                kind=DELIVERY_KIND,
                request_sha256=digest(provenance),
                request_json=provenance,
                gateway_call_id=None,
                result_json=delivery.model_dump(mode="json"),
            )
        )
        db.flush()
        audit(
            db,
            loop.project_id,
            actor,
            "art_proposal.generated",
            row.id,
            delivery.model_dump(mode="json"),
        )
        return delivery


def generate_art_proposal(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    task: ArtDirectionTask,
    *,
    limits: AgentRunLimits | None = None,
) -> ArtGenerationResult:
    """Reuse autonomous investigation; publish only its fully validated complete outcome."""
    task = ArtDirectionTask.model_validate(task.model_dump(mode="json"))
    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        validate_publication(db, run.id)
        if actor != execution_actor(db, run):
            raise DomainError("FORBIDDEN", "美术决策须使用原运行发起者", 403)
        loop = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id,
                AgentLoop.operation_sha256 == operation_sha256(run.id),
            )
        )
        prior = (
            db.scalar(
                select(AgentLoopAction).where(
                    AgentLoopAction.loop_id == loop.id, AgentLoopAction.action_key == DELIVERY_KEY
                )
            )
            if loop
            else None
        )
        if loop is not None and prior is not None:
            replayed = _saved_delivery(db, loop, prior)
            _, seed = direction_loop(db, run.id)
            if actor != loop.actor_id or seed.task != task:
                raise DomainError("ART_TASK_CONFLICT", "同一美术交付不能更换操作者或冻结目标", 409)
            direction = AgentLoopResult[ArtDirectionResult].model_validate(loop.outcome_json)
            return ArtGenerationResult(direction=direction, delivery=replayed)
    direction = propose_art(factory, settings, run_id, actor, task, limits=limits)
    delivery = None
    if direction.status == "COMPLETED":
        assert direction.loop_id is not None
        delivery = publish_art_direction(factory, settings, direction.loop_id, actor)
    return ArtGenerationResult(direction=direction, delivery=delivery)
