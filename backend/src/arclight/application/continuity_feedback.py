"""Observe a revision and execute bounded, journaled Reviewer feedback."""

from datetime import UTC, datetime
from functools import partial
from typing import Literal
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_tools import AgentToolRegistry
from arclight.application.audit import audit
from arclight.application.candidate_checks import require_candidate_media_check
from arclight.application.commands import require_run
from arclight.application.context_evidence import read_trace
from arclight.application.continuity_escalations import request_escalation
from arclight.application.continuity_feedback_store import (
    completed_feedback,
    feedback_history,
    feedback_record,
    feedback_snapshot,
    require_feedback_gate,
    validate_feedback_record,
)
from arclight.application.continuity_review_store import require_review_record
from arclight.application.continuity_reviewer import authorize_reviewer, review_candidate
from arclight.application.continuity_revision_store import candidate_revision, revision_record
from arclight.application.continuity_revisions import request_revision
from arclight.application.continuity_routing import read_routed_review, route_reviewer_result
from arclight.application.human_reviews import request_review, require_review_actor
from arclight.application.model_gateway import lock_gate
from arclight.application.production_versions import bind, checked_snapshot, component, find_binding
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision
from arclight.domain.continuity_escalations import LIMIT_STOP_CODES
from arclight.domain.continuity_feedback import (
    FEEDBACK_HANDLER,
    FEEDBACK_PROMPT,
    FEEDBACK_RESOURCE,
    FEEDBACK_VERSION,
    MAX_FEEDBACK_DECISIONS,
    FeedbackAction,
    FeedbackDecision,
    FeedbackProgress,
    FeedbackRecord,
    FeedbackSeed,
    FeedbackStatus,
)
from arclight.domain.continuity_reviews import ReviewContractRecord, ReviewRerun, ReviewTarget
from arclight.domain.continuity_revisions import RevisionRecord
from arclight.domain.continuity_routing import ReviewRoute
from arclight.domain.continuity_selection import ADMISSION_RESOURCE
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import ProductionComponent
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import GenerationCandidate, GenerationJob


def feedback_context(
    db: Session, settings: Settings, run_id: UUID, actor: str, revision_id: UUID
) -> RevisionRecord:
    lock_gate(db)
    run = require_run(db, run_id, actor, lock=True)
    episode = authorize_reviewer(db, settings, run, actor)
    revision = revision_record(db, episode.project_id, revision_id)
    require_review_actor(db, episode.project_id, revision.created_by, owner=False)
    source = find_binding(db, episode.project_id, ADMISSION_RESOURCE, revision.request.review_id)
    if source is None:
        raise DomainError("FEEDBACK_SOURCE_INVALID", "修订缺少原权威审查来源", 409)
    route = ReviewRoute.model_validate(checked_snapshot(db, source)["review_route"])
    if route.run_id != run.id or route.routed_by != actor:
        raise DomainError("FEEDBACK_RUN_INVALID", "反馈必须沿用原审查运行及发起者", 409)
    return revision


def revision_target(db: Session, revision: RevisionRecord) -> ReviewTarget | None:
    if revision.job_id is None:
        return revision.target
    job = db.get(GenerationJob, revision.job_id)
    if job is None or job.project_id != revision.project_id:
        raise DomainError("FEEDBACK_SOURCE_INVALID", "修订任务来源不一致", 409)
    candidates = list(
        db.scalars(
            select(GenerationCandidate).where(
                GenerationCandidate.job_id == job.id,
                GenerationCandidate.source == "GENERATED",
            )
        )
    )
    if not candidates:
        if job.status in {"FAILED", "CANCELLED", "MANUAL_REVIEW"}:
            raise DomainError("FEEDBACK_MEDIA_FAILED", "请先处理原修订任务，不能另建生成路径", 409)
        return None
    if job.status != "SUCCEEDED" or len(candidates) != 1 or candidates[0].asset_version_id is None:
        raise DomainError("FEEDBACK_MEDIA_INVALID", "反馈须观察原任务唯一成功候选", 409)
    candidate = candidates[0]
    checked = require_candidate_media_check(db, revision.project_id, candidate.id)
    return ReviewTarget.model_validate(
        revision.target.model_dump(mode="json")
        | {
            "candidate_id": candidate.id,
            "asset_version_id": candidate.asset_version_id,
            "input_manifest_id": candidate.input_manifest_id,
            "media_check_sha256": digest(checked.model_dump(mode="json")),
        }
    )


def feedback_versions(db: Session, record: ReviewContractRecord) -> list[ProductionComponent]:
    entries = [item.model_dump(mode="json") for item in record.review.versions]
    for kind, name, definition in (
        ("AGENT", "continuity-feedback", {"maximum_decisions": MAX_FEEDBACK_DECISIONS}),
        ("PROMPT", "continuity-feedback", {"instruction": FEEDBACK_PROMPT}),
        ("SCHEMA", "continuity-feedback-record", FeedbackRecord.model_json_schema()),
    ):
        entries.append(
            component(db, record.review.project_id, "REVIEW", kind, name, "1", definition)
        )
    return [ProductionComponent.model_validate(item) for item in entries]


def validate_feedback_decision(
    db: Session,
    decision: FeedbackDecision,
    *,
    settings: Settings,
    run_id: UUID,
    actor: str,
    seed: FeedbackSeed,
) -> None:
    revision = feedback_context(db, settings, run_id, actor, seed.revision.revision_id)
    current, _ = read_routed_review(db, seed.review.review.result_id)
    if (
        revision != seed.revision
        or current != seed.review
        or decision.decision_id != seed.decision_id
        or decision.revision_id != revision.revision_id
        or decision.review_id != current.review.result_id
        or decision.review_sha256 != seed.review_sha256
        or revision_target(db, revision) != current.review.target
    ):
        raise DomainError("FEEDBACK_INPUT_CHANGED", "反馈不能更换修订、新图或精确审查", 409)
    if decision.action == "READY_FOR_REVIEW" and current.review.conclusion != "PASS":
        raise DomainError("FEEDBACK_ACTION_INVALID", "只有PASS可以等待人工选用", 422)
    if decision.action == "REVISE":
        command = decision.revision_request
        assert command is not None
        issues = {item.issue_id: item for item in current.review.issues}
        if (
            current.review.conclusion != "REVISION_REQUIRED"
            or not set(command.issue_ids) <= issues.keys()
            or any(
                issues[identity].suggested_action not in {"REVISE_PROMPT", "REGENERATE"}
                for identity in command.issue_ids
            )
        ):
            raise DomainError("FEEDBACK_ACTION_INVALID", "修订须引用本次权威问题", 422)
    if current.review.conclusion == "HUMAN_REVIEW" and decision.action != "HUMAN_REVIEW":
        raise DomainError("HUMAN_DECISION_REQUIRED", "不能通过反馈绕过原人工升级", 409)


def seal_feedback(
    db: Session,
    settings: Settings,
    run_id: UUID,
    actor: str,
    revision: RevisionRecord,
    reviewed: ReviewContractRecord,
    history: list[FeedbackRecord],
    *,
    decision: FeedbackDecision | None = None,
    loop: AgentLoop | None = None,
    stop_code: str | None = None,
) -> FeedbackRecord:
    feedback_context(db, settings, run_id, actor, revision.revision_id)
    current, _ = read_routed_review(db, reviewed.review.result_id)
    if current != reviewed:
        raise DomainError("FEEDBACK_INPUT_CHANGED", "新审查已变化，不能执行旧反馈", 409)
    identity = (
        decision.decision_id
        if decision
        else uuid5(
            run_id,
            f"{FEEDBACK_VERSION}:{revision.revision_id}:{reviewed.review.result_id}:"
            + (stop_code or "human"),
        )
    )
    if find_binding(db, revision.project_id, FEEDBACK_RESOURCE, identity) is not None:
        return feedback_record(db, revision.project_id, identity)
    current_history = feedback_history(db, revision.project_id, revision.revision_id)
    if current_history != history:
        raise DomainError("FEEDBACK_HISTORY_CHANGED", "反馈历史已推进，请重入读取原回执", 409)
    child = None
    request = None
    action = decision.action if decision else "HUMAN_REVIEW"
    statuses: dict[FeedbackAction, FeedbackStatus] = {
        "INVESTIGATE": "INVESTIGATING",
        "REVISE": "REVISION_QUEUED",
        "READY_FOR_REVIEW": "READY_FOR_REVIEW",
        "HUMAN_REVIEW": "HUMAN_REVIEW",
    }
    status = statuses[action]
    if decision is not None and action == "REVISE":
        assert decision.revision_request is not None
        existing = candidate_revision(db, revision.project_id, reviewed.review.target.candidate_id)
        if existing is not None:
            child = revision_record(db, revision.project_id, existing.resource_id)
            require_review_actor(db, revision.project_id, child.created_by, owner=False)
            if child.request != decision.revision_request:
                status, stop_code = "HUMAN_REVIEW", "REVISION_ALREADY_REQUESTED"
        else:
            try:
                # A refused reservation must leave no partial job or manifest.
                with db.begin_nested():
                    child, _ = request_revision(
                        db,
                        settings,
                        revision.project_id,
                        actor,
                        "continuity-feedback:" + str(identity),
                        decision.revision_request,
                    )
            except DomainError as exc:
                if exc.code not in {
                    "BUDGET_EXCEEDED",
                    "BUDGET_EPISODE_EXCEEDED",
                    "BUDGET_TASK_EXCEEDED",
                    "BUDGET_BATCH_EXCEEDED",
                    "BUDGET_CANDIDATE_LIMIT",
                    "BUDGET_QUOTE_UNAVAILABLE",
                }:
                    raise
                status, stop_code = "HUMAN_REVIEW", exc.code
        if child is not None and child.status == "NEEDS_REVIEW":
            status, stop_code = "HUMAN_REVIEW", "REVISION_ROUND_LIMIT"
    if reviewed.review.conclusion == "HUMAN_REVIEW":
        request = request_escalation(db, run_id, actor, reviewed.review.target)
        status = "HUMAN_REVIEW"
    human_key = "continuity-feedback:" + str(identity)
    human_id = (
        request.id
        if request
        else (
            uuid5(revision.project_id, "human-review/1:" + human_key)
            if status == "HUMAN_REVIEW"
            else None
        )
    )
    source: Literal["MODEL", "LIMIT", "REVIEWER"]
    source = "MODEL" if decision else ("LIMIT" if stop_code else "REVIEWER")
    record = FeedbackRecord(
        feedback_id=identity,
        project_id=revision.project_id,
        episode_id=reviewed.review.episode_id,
        run_id=run_id,
        revision_id=revision.revision_id,
        revision_sha256=digest(revision.model_dump(mode="json")),
        target=reviewed.review.target,
        review_id=reviewed.review.result_id,
        review_sha256=digest(reviewed.model_dump(mode="json")),
        sequence=len(history) + 1,
        action=action,
        reason=decision.reason if decision else (stop_code or "新图审查要求人工处理"),
        source=source,
        decision=decision,
        decision_loop_id=loop.id if loop else None,
        outcome_sha256=digest(loop.outcome_json) if loop else None,
        stop_code=stop_code,
        child_revision_id=child.revision_id if child else None,
        human_review_request_id=human_id,
        status=status,
        created_by=actor,
        created_at=datetime.now(UTC),
    )
    validate_feedback_record(db, record)
    value = record.model_dump(mode="json")
    bind(
        db,
        revision.project_id,
        FEEDBACK_RESOURCE,
        identity,
        {
            "schema_version": 1,
            "compatibility": "EXACT_DEFINITIONS",
            "components": [
                item.model_dump(mode="json") for item in feedback_versions(db, reviewed)
            ],
            "parameters": value,
            "parameter_sha256": digest(value),
            "feedback": value,
        },
    )
    if status == "HUMAN_REVIEW" and request is None:
        request = request_review(
            db, actor, human_key, FEEDBACK_HANDLER, feedback_snapshot(db, record)
        )
        assert request.id == record.human_review_request_id
    audit(db, revision.project_id, actor, "continuity_feedback.decided", identity, value)
    db.flush()
    return record


def feedback_progress(record: FeedbackRecord) -> FeedbackProgress:
    return FeedbackProgress(
        run_id=record.run_id,
        revision_id=record.revision_id,
        status=record.status,
        record=record,
        loop_id=record.decision_loop_id,
    )


def prepare_feedback(
    db: Session,
    run_id: UUID,
    actor: str,
    revision: RevisionRecord,
    reviewed: ReviewContractRecord,
    history: list[FeedbackRecord],
) -> tuple[AgentModelRequest, FeedbackSeed]:
    operation = f"{FEEDBACK_VERSION}:{revision.revision_id}:{reviewed.review.result_id}"
    previous = db.scalar(
        select(AgentLoop).where(
            AgentLoop.run_id == run_id,
            AgentLoop.operation_sha256
            == digest(
                {
                    "run": str(run_id),
                    "stage": "storyboard",
                    "operation": operation,
                }
            ),
        )
    )
    if previous is not None:
        request = AgentModelRequest.model_validate(previous.seed_json)
        first = request.messages[0]
        assert isinstance(first, AgentUserMessage)
        seed = FeedbackSeed.model_validate(first.data)
        if seed.revision != revision or seed.review != reviewed or seed.history != history:
            raise DomainError("FEEDBACK_INPUT_CHANGED", "重入不能更换原反馈输入或历史", 409)
        return request, seed
    run = require_run(db, run_id, actor, lock=True)
    seed = FeedbackSeed(
        decision_id=uuid5(run_id, operation),
        revision=revision,
        previous_review=require_review_record(
            db, revision.project_id, revision.request.review_id, actor
        ),
        review=reviewed,
        review_sha256=digest(reviewed.model_dump(mode="json")),
        history=history,
        remaining_decisions=MAX_FEEDBACK_DECISIONS - len(history),
        versions=feedback_versions(db, reviewed),
        context_evidence_start=len(read_trace(db, run, "storyboard", actor)["evidence"]),
    )
    return AgentModelRequest(
        run_id=run_id,
        agent_name="continuity_reviewer",
        stage="storyboard",
        operation_key=operation,
        agent_version=FEEDBACK_VERSION,
        prompt_version=FEEDBACK_VERSION,
        result_schema_version=FEEDBACK_VERSION,
        system_instruction=FEEDBACK_PROMPT,
        max_tokens=8000,
        messages=[AgentUserMessage(data=seed.model_dump(mode="json"))],
    ), seed


def review_revision_feedback(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    revision_id: UUID,
    *,
    rerun: ReviewRerun | None = None,
) -> FeedbackProgress:
    """Trusted orchestration; reentry repairs a completed model/action handoff."""
    while True:
        with factory.begin() as db:
            revision = feedback_context(db, settings, run_id, actor, revision_id)
            target = revision_target(db, revision)
            if target is None:
                return FeedbackProgress(
                    run_id=run_id,
                    revision_id=revision_id,
                    status="WAITING_MEDIA",
                    job_id=revision.job_id,
                )
            history = feedback_history(db, revision.project_id, revision_id)
            if history and history[-1].action != "INVESTIGATE" and rerun is None:
                validate_feedback_record(db, history[-1])
                return feedback_progress(history[-1])
            if history and history[-1].status == "HUMAN_REVIEW":
                require_feedback_gate(
                    db, revision.project_id, target.candidate_id, history[-1].review_id
                )
            if revision.status == "NEEDS_REVIEW" or len(history) >= MAX_FEEDBACK_DECISIONS:
                reviewed, _ = read_routed_review(
                    db, history[-1].review_id if history else revision.request.review_id
                )
                record = seal_feedback(
                    db,
                    settings,
                    run_id,
                    actor,
                    revision,
                    reviewed,
                    history,
                    stop_code="REVISION_ROUND_LIMIT"
                    if revision.status == "NEEDS_REVIEW"
                    else "FEEDBACK_ROUND_LIMIT",
                )
                return feedback_progress(record)
            if history and history[-1].action == "INVESTIGATE":
                rerun = ReviewRerun(
                    previous_review_id=history[-1].review_id,
                    previous_sha256=history[-1].review_sha256,
                    reason=history[-1].reason,
                )
        outcome = review_candidate(factory, settings, run_id, actor, target, rerun=rerun)
        if outcome.status != "COMPLETED":
            return FeedbackProgress(
                run_id=run_id,
                revision_id=revision_id,
                status="STOPPED" if outcome.status == "STOPPED" else "REVIEWING",
                job_id=revision.job_id,
                loop_id=outcome.loop_id,
                question_id=outcome.question_id,
            )
        assert outcome.result is not None
        with factory.begin() as db:
            feedback_context(db, settings, run_id, actor, revision_id)
            route_reviewer_result(db, settings, run_id, actor, target)
            reviewed, _ = read_routed_review(db, outcome.result.result_id)
            if reviewed.review.conclusion == "HUMAN_REVIEW":
                record = seal_feedback(
                    db,
                    settings,
                    run_id,
                    actor,
                    revision,
                    reviewed,
                    history,
                    loop=db.get(AgentLoop, outcome.loop_id),
                )
                return feedback_progress(record)
            request, seed = prepare_feedback(db, run_id, actor, revision, reviewed, history)
        decided = run_persistent_agent_loop(
            factory,
            settings,
            request,
            AgentToolRegistry([]),
            FeedbackDecision,
            validate_result=partial(
                validate_feedback_decision, settings=settings, run_id=run_id, actor=actor, seed=seed
            ),
        )
        if decided.status not in {"COMPLETED", "STOPPED"}:
            return FeedbackProgress(
                run_id=run_id,
                revision_id=revision_id,
                status="REVIEWING",
                loop_id=decided.loop_id,
                question_id=decided.question_id,
            )
        if decided.status == "STOPPED" and decided.stop_code not in LIMIT_STOP_CODES:
            return FeedbackProgress(
                run_id=run_id, revision_id=revision_id, status="STOPPED", loop_id=decided.loop_id
            )
        with factory.begin() as db:
            assert decided.loop_id is not None
            loop = db.get(AgentLoop, decided.loop_id)
            assert loop is not None
            decision = completed_feedback(db, loop) if decided.status == "COMPLETED" else None
            record = seal_feedback(
                db,
                settings,
                run_id,
                actor,
                revision,
                reviewed,
                history,
                decision=decision,
                loop=loop,
                stop_code=decided.stop_code,
            )
        if record.action != "INVESTIGATE":
            return feedback_progress(record)
        rerun = None


def resume_revision_feedback(
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> FeedbackProgress:
    with factory.begin() as db:
        loop = db.get(AgentLoop, loop_id)
        if loop is None:
            raise DomainError("NOT_FOUND", "反馈循环不存在", 404)
        request = AgentModelRequest.model_validate(loop.seed_json)
        first = request.messages[0]
        if not request.operation_key.startswith(FEEDBACK_VERSION + ":") or not isinstance(
            first, AgentUserMessage
        ):
            raise DomainError("FEEDBACK_LOOP_INVALID", "只能恢复原反馈循环", 409)
        seed = FeedbackSeed.model_validate(first.data)
        initiator, run_id = loop.actor_id, loop.run_id
        feedback_context(db, settings, run_id, initiator, seed.revision.revision_id)
    resume_agent_loop(
        factory,
        settings,
        loop_id,
        actor,
        AgentToolRegistry([]),
        FeedbackDecision,
        decision=decision,
        validate_result=partial(
            validate_feedback_decision, settings=settings, run_id=run_id, actor=initiator, seed=seed
        ),
    )
    return review_revision_feedback(factory, settings, run_id, initiator, seed.revision.revision_id)
