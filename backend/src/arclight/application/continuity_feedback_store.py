"""Immutable feedback history and the shared human/selection handoff."""

from uuid import UUID, uuid5

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.continuity_escalations import apply_decision, check_decision
from arclight.application.continuity_review_store import require_review_record
from arclight.application.continuity_reviews import require_review_target
from arclight.application.continuity_revision_store import job_revision, revision_record
from arclight.application.continuity_routing import completed_review, read_routed_review
from arclight.application.human_reviews import ReviewHandler, require_review_actor
from arclight.application.model_gateway import utc
from arclight.application.production_versions import checked_snapshot, find_binding
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentModelResponse, AgentUserMessage
from arclight.domain.agent_runtime import AgentDecision, AgentLoopResult
from arclight.domain.continuity_escalations import LIMIT_STOP_CODES
from arclight.domain.continuity_feedback import (
    FEEDBACK_HANDLER,
    FEEDBACK_PROMPT,
    FEEDBACK_RESOURCE,
    FEEDBACK_VERSION,
    MAX_FEEDBACK_DECISIONS,
    FeedbackDecision,
    FeedbackRecord,
    FeedbackSeed,
)
from arclight.domain.human_reviews import HumanDecision, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import GenerationCandidate, GenerationJob, VersionBinding


def feedback_record(db: Session, project_id: UUID, identity: UUID) -> FeedbackRecord:
    binding = find_binding(db, project_id, FEEDBACK_RESOURCE, identity)
    if binding is None:
        raise DomainError("NOT_FOUND", "反馈记录不存在", 404)
    try:
        snapshot = checked_snapshot(db, binding)
        record = FeedbackRecord.model_validate(snapshot["feedback"])
        if (
            record.project_id != project_id
            or record.feedback_id != identity
            or snapshot["parameters"] != record.model_dump(mode="json")
            or not any(
                item["kind"] == "SCHEMA"
                and item["name"] == "continuity-feedback-record"
                and item["digest"] == digest(FeedbackRecord.model_json_schema())
                for item in snapshot["components"]
            )
        ):
            raise ValueError("Feedback must retain its exact schema and immutable parameters")
    except (KeyError, TypeError, ValueError, ValidationError) as exc:
        raise DomainError("FEEDBACK_RECORD_INVALID", "反馈凭据与原决定或版本不一致", 409) from exc
    return record


def feedback_history(db: Session, project_id: UUID, revision_id: UUID) -> list[FeedbackRecord]:
    bindings = db.scalars(
        select(VersionBinding)
        .where(
            VersionBinding.project_id == project_id,
            VersionBinding.resource_type == FEEDBACK_RESOURCE,
            VersionBinding.snapshot_json["feedback"]["revision_id"].as_string() == str(revision_id),
        )
        .order_by(VersionBinding.snapshot_json["feedback"]["sequence"].as_integer())
    )
    return [feedback_record(db, project_id, row.resource_id) for row in bindings]


def completed_feedback(db: Session, loop: AgentLoop) -> FeedbackDecision:
    """Use the committed gateway delivery, rather than trusting a mutable checkpoint."""
    try:
        outcome = AgentLoopResult[FeedbackDecision].model_validate(loop.outcome_json)
        action = db.scalar(
            select(AgentLoopAction)
            .where(AgentLoopAction.loop_id == loop.id, AgentLoopAction.kind == "MODEL")
            .order_by(AgentLoopAction.created_at.desc(), AgentLoopAction.id.desc())
            .limit(1)
        )
        if action is None or action.result_json is None or action.gateway_call_id is None:
            raise ValueError("Missing original feedback model delivery")
        response = AgentModelResponse[AgentDecision[FeedbackDecision]].model_validate(
            action.result_json
        )
        original = AgentModelRequest.model_validate(loop.seed_json)
        delivered = AgentModelRequest.model_validate(action.request_json["request"])
        if (
            delivered.model_dump(exclude={"messages", "evidence_round", "operation_key"})
            != original.model_dump(exclude={"messages", "evidence_round", "operation_key"})
            or delivered.operation_key != action.action_key
            or not action.action_key.startswith(f"loop:{loop.operation_sha256}:")
            or delivered.messages[0] != original.messages[0]
        ):
            raise ValueError("Model delivery must retain the original feedback request")
        if (
            loop.status != "COMPLETED"
            or outcome.status != "COMPLETED"
            or outcome.loop_id != loop.id
            or outcome.run_id != loop.run_id
            or outcome.project_id != loop.project_id
            or outcome.agent_name != "continuity_reviewer"
            or outcome.stage != "storyboard"
            or outcome.input_sha256 != loop.input_sha256
            or response.status != "FINAL"
            or response.final is None
            or response.final.decision != "COMPLETE"
            or response.final.result != outcome.result
            or outcome.result is None
            or action.request_sha256 != digest(action.request_json)
        ):
            raise ValueError("Feedback must equal the original complete model receipt")
        return outcome.result
    except (ValidationError, ValueError, TypeError, KeyError, IndexError) as exc:
        raise DomainError("FEEDBACK_DELIVERY_INVALID", "反馈与原模型回执不一致", 409) from exc


def validate_feedback_record(db: Session, record: FeedbackRecord) -> None:
    require_review_actor(db, record.project_id, record.created_by, owner=False)
    revision = revision_record(db, record.project_id, record.revision_id)
    require_review_actor(db, record.project_id, revision.created_by, owner=False)
    reviewed, route = read_routed_review(db, record.review_id, lock_run=False)
    require_review_actor(db, record.project_id, route.routed_by, owner=False)
    if (
        record.revision_sha256 != digest(revision.model_dump(mode="json"))
        or record.review_sha256 != digest(reviewed.model_dump(mode="json"))
        or record.target != reviewed.review.target
        or route.run_id != record.run_id
        or record.episode_id != reviewed.review.episode_id
        or record.created_by != route.routed_by
    ):
        raise DomainError("FEEDBACK_INPUT_CHANGED", "反馈须绑定原修订及最新精确审查", 409)
    require_review_target(
        db, record.project_id, record.episode_id, record.target, record.created_by
    )
    loop = db.get(AgentLoop, record.decision_loop_id) if record.decision_loop_id else None
    if loop is not None and (
        loop.run_id != record.run_id
        or loop.project_id != record.project_id
        or loop.actor_id != record.created_by
        or loop.thread_id != f"agent-loop:{loop.id}"
        or digest(loop.outcome_json) != record.outcome_sha256
        or digest(loop.seed_json) != loop.request_sha256
    ):
        raise DomainError("FEEDBACK_DELIVERY_INVALID", "反馈不能更换原循环或交付", 409)
    if record.source == "REVIEWER":
        if (
            loop is None
            or route.loop_id != loop.id
            or completed_review(db, loop) != reviewed.review
            or reviewed.review.conclusion != "HUMAN_REVIEW"
        ):
            raise DomainError("FEEDBACK_DELIVERY_INVALID", "人工升级缺少原审查交付", 409)
        return
    if loop is not None:
        try:
            request = AgentModelRequest.model_validate(loop.seed_json)
            first = request.messages[0]
            if not isinstance(first, AgentUserMessage):
                raise ValueError("Missing exact feedback seed")
            seed = FeedbackSeed.model_validate(first.data)
            operation = f"{FEEDBACK_VERSION}:{revision.revision_id}:{record.review_id}"
            review_loop = db.get(AgentLoop, route.loop_id)
            if (
                seed.decision_id != uuid5(record.run_id, operation)
                or seed.revision != revision
                or seed.review != reviewed
                or request.run_id != record.run_id
                or request.agent_name != "continuity_reviewer"
                or request.stage != "storyboard"
                or request.operation_key != operation
                or request.agent_version != FEEDBACK_VERSION
                or request.prompt_version != FEEDBACK_VERSION
                or request.result_schema_version != FEEDBACK_VERSION
                or request.system_instruction != FEEDBACK_PROMPT
                or loop.operation_sha256
                != digest(
                    {"run": str(record.run_id), "stage": "storyboard", "operation": operation}
                )
                or loop.result_schema_sha256 != digest(FeedbackDecision.model_json_schema())
                or review_loop is None
                or loop.input_sha256 != review_loop.input_sha256
            ):
                raise ValueError("Feedback loop must retain its frozen scope and review")
            if record.source == "MODEL":
                if completed_feedback(db, loop) != record.decision:
                    raise ValueError("Missing exact original model decision")
            else:
                outcome = AgentLoopResult[FeedbackDecision].model_validate(loop.outcome_json)
                if (
                    loop.status != "STOPPED"
                    or outcome.status != "STOPPED"
                    or outcome.stop_code not in LIMIT_STOP_CODES
                    or outcome.stop_code != record.stop_code
                    or outcome.loop_id != loop.id
                    or outcome.run_id != loop.run_id
                    or outcome.project_id != loop.project_id
                    or outcome.agent_name != "continuity_reviewer"
                    or outcome.stage != "storyboard"
                    or outcome.input_sha256 != loop.input_sha256
                ):
                    raise ValueError("Feedback exhaustion must identify its original stop")
        except (ValidationError, ValueError, TypeError, IndexError) as exc:
            raise DomainError("FEEDBACK_DELIVERY_INVALID", "反馈与原持久循环不一致", 409) from exc
    elif record.source == "MODEL":
        raise DomainError("FEEDBACK_DELIVERY_INVALID", "缺少原持久反馈决定", 409)
    elif record.stop_code == "REVISION_ROUND_LIMIT":
        if revision.status != "NEEDS_REVIEW":
            raise DomainError("FEEDBACK_LIMIT_INVALID", "修订次数尚未耗尽", 409)
    elif (
        record.stop_code != "FEEDBACK_ROUND_LIMIT" or record.sequence != MAX_FEEDBACK_DECISIONS + 1
    ):
        raise DomainError("FEEDBACK_LIMIT_INVALID", "反馈次数尚未耗尽", 409)
    elif (
        len(
            [
                item
                for item in feedback_history(db, record.project_id, record.revision_id)
                if item.sequence < record.sequence
            ]
        )
        != MAX_FEEDBACK_DECISIONS
    ):
        raise DomainError("FEEDBACK_LIMIT_INVALID", "缺少原三轮反馈", 409)


def feedback_snapshot(db: Session, record: FeedbackRecord) -> ReviewSnapshot:
    version, _, _ = require_review_target(
        db, record.project_id, record.episode_id, record.target, record.created_by
    )
    reviewed = require_review_record(db, record.project_id, record.review_id, record.created_by)
    return ReviewSnapshot(
        project_id=record.project_id,
        episode_id=record.episode_id,
        run_id=record.run_id,
        thread_id=str(record.run_id),
        kind="EXCEPTIONAL_SHOT",
        object_id=record.feedback_id,
        version_id=record.target.asset_version_id,
        content_sha256=version.checksum,
        input_sha256=digest(record.model_dump(mode="json")),
        source_version_ids=sorted(
            {record.target.asset_version_id, record.target.storyboard_id}
            | {
                ref.resource_id
                for ref in reviewed.review.references
                if ref.role in {"REFERENCE", "VISUAL_BIBLE"}
            },
            key=str,
        ),
    )


def read_feedback_pause(db: Session, frozen: ReviewSnapshot) -> ReviewSnapshot:
    record = feedback_record(db, frozen.project_id, frozen.object_id)
    validate_feedback_record(db, record)
    if record.status != "HUMAN_REVIEW":
        raise DomainError("FEEDBACK_HANDLING_INVALID", "反馈没有精确人工处理请求", 409)
    return feedback_snapshot(db, record)


def require_feedback_gate(
    db: Session, project_id: UUID, candidate_id: UUID, review_id: UUID
) -> None:
    rows = list(
        db.scalars(
            select(VersionBinding)
            .where(
                VersionBinding.project_id == project_id,
                VersionBinding.resource_type == FEEDBACK_RESOURCE,
                VersionBinding.snapshot_json["feedback"]["target"]["candidate_id"].as_string()
                == str(candidate_id),
            )
            .order_by(VersionBinding.created_at.desc(), VersionBinding.id.desc())
        )
    )
    records = [feedback_record(db, project_id, row.resource_id) for row in rows]
    sealed = {item.decision_loop_id for item in records if item.decision_loop_id}
    loops = db.scalars(
        select(AgentLoop).where(
            AgentLoop.project_id == project_id,
            AgentLoop.seed_json["operation_key"].as_string().like(FEEDBACK_VERSION + ":%"),
            AgentLoop.seed_json["messages"][0]["data"]["review"]["review"]["target"][
                "candidate_id"
            ].as_string()
            == str(candidate_id),
        )
    )
    if any(
        loop.id not in sealed
        and (not records or utc(loop.created_at) >= utc(records[0].created_at))
        for loop in loops
    ):
        raise DomainError("CONTINUITY_FEEDBACK_PENDING", "候选的新反馈尚未完成或交接", 409)
    if not records:
        # A same-run revision review has already committed its route but may
        # crash before creating the feedback loop. Keep that handoff closed.
        candidate = db.get(GenerationCandidate, candidate_id)
        job = db.get(GenerationJob, candidate.job_id) if candidate else None
        revision = job_revision(db, job) if job else None
        if revision is not None:
            from arclight.domain.continuity_selection import ADMISSION_RESOURCE

            source = find_binding(db, project_id, ADMISSION_RESOURCE, revision.request.review_id)
            current = find_binding(db, project_id, ADMISSION_RESOURCE, review_id)
            if (
                source is not None
                and current is not None
                and (
                    checked_snapshot(db, source).get("review_route", {}).get("run_id")
                    == checked_snapshot(db, current).get("review_route", {}).get("run_id")
                )
            ):
                raise DomainError("CONTINUITY_FEEDBACK_PENDING", "修订审查尚未交接反馈决定", 409)
        return
    record = records[0]
    validate_feedback_record(db, record)
    if record.review_id != review_id:
        raise DomainError("FEEDBACK_REVIEW_STALE", "旧反馈或例外不能代替本次新审查", 409)
    if record.status == "INVESTIGATING":
        raise DomainError("CONTINUITY_FEEDBACK_PENDING", "候选正在追加取证与重审", 409)
    if record.status != "HUMAN_REVIEW":
        return
    request = db.get(HumanReviewRequest, record.human_review_request_id)
    decision = db.scalar(
        select(HumanReviewDecision).where(
            HumanReviewDecision.request_id == record.human_review_request_id
        )
    )
    resumed = db.scalar(
        select(HumanReviewResume).where(
            HumanReviewResume.request_id == record.human_review_request_id
        )
    )
    if request is None or decision is None or resumed is None:
        raise DomainError("HUMAN_DECISION_REQUIRED", "反馈须由Owner处理精确版本", 409)
    answer = HumanDecision.model_validate(decision.decision_json)
    snapshot = ReviewSnapshot.model_validate(request.snapshot_json)
    require_review_actor(db, project_id, decision.actor_id, owner=True)
    if (
        request.project_id != project_id
        or request.request_sha256 != digest(snapshot.model_dump(mode="json"))
        or request.episode_id != record.episode_id
        or request.run_id != record.run_id
        or snapshot.project_id != project_id
        or snapshot.episode_id != record.episode_id
        or snapshot.thread_id != str(record.run_id)
        or snapshot.kind != "EXCEPTIONAL_SHOT"
        or snapshot.purpose != "PRODUCTION"
        or snapshot.version_id != record.target.asset_version_id
        or snapshot.run_id != record.run_id
        or answer.version_id != record.target.asset_version_id
        or answer.request_sha256 != request.request_sha256
        or answer.decision != "APPROVE"
        or not answer.comment.strip()
        or resumed.decision_id != decision.id
        or resumed.result_json.get("status") != "APPROVED"
        or (request.handler == FEEDBACK_HANDLER and snapshot != feedback_snapshot(db, record))
        or (
            request.handler == "continuity_shot"
            and (
                record.source != "REVIEWER"
                or snapshot.object_id != record.review_id
                or snapshot.input_sha256 != record.review_sha256
            )
        )
        or request.handler not in {FEEDBACK_HANDLER, "continuity_shot"}
    ):
        raise DomainError("HUMAN_DECISION_REQUIRED", "缺少本次精确反馈的有效Owner处理", 409)


FEEDBACK_HANDLERS = {
    FEEDBACK_HANDLER: ReviewHandler(read_feedback_pause, apply_decision, check_decision),
}
