"""One review journal; domain handlers remain responsible for actual approval gates.

Handlers only read authoritative database facts and apply transactional changes.
They never perform model, file or network I/O. A checkpoint is not approval evidence.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_episode, require_project, require_run
from arclight.core.errors import DomainError
from arclight.domain.human_reviews import HumanDecision, ReviewHandoff, ReviewResume, ReviewSnapshot
from arclight.domain.versions import digest
from arclight.persistence.human_reviews import (
    HumanReviewDecision,
    HumanReviewRequest,
    HumanReviewResume,
)
from arclight.persistence.models import Episode, Project, User


@dataclass(frozen=True)
class ReviewHandler:
    read: Callable[[Session, ReviewSnapshot], ReviewSnapshot]
    apply: Callable[[Session, HumanReviewRequest, HumanReviewDecision, str], dict]


def handlers() -> Mapping[str, ReviewHandler]:
    from arclight.application.text_reviews import TEXT_HANDLERS

    return TEXT_HANDLERS


def require_review(db: Session, request_id: UUID, actor: str) -> HumanReviewRequest:
    row = db.get(HumanReviewRequest, request_id, populate_existing=True)
    if row is None:
        raise DomainError("NOT_FOUND", "审核对象不存在", 404)
    require_project(db, row.project_id, actor)
    return row


def _authority(db: Session, row: HumanReviewRequest, actor: str, *, owner: bool) -> None:
    from arclight.application.production_events import begin_serialized

    begin_serialized(db)
    snapshot = ReviewSnapshot.model_validate(row.snapshot_json)
    if snapshot.loop_id is not None:
        from arclight.application.agent_loop_store import checked_loop

        # Reuse ARUN-05's initiator, frozen-input and root-limit checks, retaining
        # its gateway -> run -> project lock order before taking other locks.
        loop = checked_loop(db, snapshot.loop_id, actor=actor)
        if loop.run_id != row.run_id or loop.thread_id != snapshot.thread_id:
            raise DomainError("REVIEW_SCOPE_INVALID", "审核必须保留原Agent运行及线程", 409)
    # Preserve the existing text writer's run -> project lock order.
    if row.run_id is not None:
        run = require_run(db, row.run_id, actor, lock=True)
        if run.episode_id != row.episode_id:
            raise DomainError("REVIEW_SCOPE_INVALID", "审核运行不属于指定剧集", 409)
    require_project(db, row.project_id, actor, write=True, owner=owner)
    if row.episode_id is not None:
        episode = require_episode(db, row.episode_id, actor, write=True, owner=owner)
        if episode.project_id != row.project_id:
            raise DomainError("REVIEW_SCOPE_INVALID", "审核不属于指定项目", 409)
    if actor != "service-owner":
        user = db.get(User, UUID(actor), populate_existing=True)
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "审核操作者已不可用", 403)


def _handler(row: HumanReviewRequest, registry: Mapping[str, ReviewHandler]) -> ReviewHandler:
    handler = registry.get(row.handler)
    if handler is None:
        raise DomainError("REVIEW_HANDLER_UNAVAILABLE", "该领域审核入口尚未接入", 409)
    return handler


def _current(
    db: Session, row: HumanReviewRequest, registry: Mapping[str, ReviewHandler]
) -> ReviewHandler:
    frozen = ReviewSnapshot.model_validate(row.snapshot_json)
    if (
        frozen.project_id != row.project_id
        or frozen.episode_id != row.episode_id
        or frozen.run_id != row.run_id
        or digest(row.snapshot_json) != row.request_sha256
    ):
        raise DomainError("REVIEW_SCOPE_INVALID", "审核持久化合同无效", 409)
    handler = _handler(row, registry)
    current = handler.read(db, frozen)
    if current != frozen:
        raise DomainError("REVIEW_INPUT_CHANGED", "审核对象或精确输入已经变化，请重新发起审核", 409)
    return handler


def review_blocker(db: Session, row: HumanReviewRequest) -> str | None:
    """Read-only availability hint; command handlers independently repeat every check."""
    project = db.get(Project, row.project_id, populate_existing=True)
    episode = db.get(Episode, row.episode_id, populate_existing=True) if row.episode_id else None
    if project is None or project.deleted_at is not None:
        return "NOT_FOUND"
    if project.archived:
        return "PROJECT_ARCHIVED"
    if episode is not None and episode.status == "ARCHIVED":
        return "EPISODE_ARCHIVED"
    try:
        _current(db, row, handlers())
    except DomainError as exc:
        return exc.code
    return None


def request_review(
    db: Session,
    actor: str,
    creation_key: str,
    handler: str,
    snapshot: ReviewSnapshot,
    *,
    registry: Mapping[str, ReviewHandler] | None = None,
) -> HumanReviewRequest:
    """Trusted domain entry point, deliberately not a client-supplied HTTP creation API."""
    snapshot = ReviewSnapshot.model_validate(snapshot.model_dump(mode="json"))
    if not creation_key or len(creation_key) > 160 or not handler or len(handler) > 64:
        raise DomainError("REVIEW_REQUEST_INVALID", "审核创建键或处理器无效", 422)
    value = snapshot.model_dump(mode="json")
    row = HumanReviewRequest(
        id=uuid5(snapshot.project_id, "human-review/1:" + creation_key),
        project_id=snapshot.project_id,
        episode_id=snapshot.episode_id,
        run_id=snapshot.run_id,
        creation_key=creation_key,
        handler=handler,
        requested_by=actor,
        request_sha256=digest(value),
        snapshot_json=value,
    )
    _authority(db, row, actor, owner=False)
    _current(db, row, registry if registry is not None else handlers())
    previous = db.scalar(
        select(HumanReviewRequest).where(
            HumanReviewRequest.project_id == snapshot.project_id,
            HumanReviewRequest.creation_key == creation_key,
        )
    )
    if previous is not None:
        if (
            previous.handler != handler
            or previous.requested_by != actor
            or previous.snapshot_json != value
        ):
            raise DomainError("REVIEW_REQUEST_CONFLICT", "同一审核创建键不能改变输入", 409)
        return previous
    db.add(row)
    db.flush()
    audit(db, row.project_id, actor, "human_review.requested", row.id, {"kind": snapshot.kind})
    return row


def submit_decision(
    db: Session,
    request_id: UUID,
    actor: str,
    decision: HumanDecision,
    *,
    registry: Mapping[str, ReviewHandler] | None = None,
) -> HumanReviewDecision:
    decision = HumanDecision.model_validate(decision.model_dump(mode="json"))
    row = require_review(db, request_id, actor)
    _authority(db, row, actor, owner=True)
    snapshot = ReviewSnapshot.model_validate(row.snapshot_json)
    if decision.version_id != snapshot.version_id or decision.request_sha256 != row.request_sha256:
        raise DomainError("REVIEW_INPUT_CHANGED", "人工决定不属于当前精确审核对象", 409)
    # A preview can be inspected/rejected but never mint a formal approval.
    if snapshot.purpose == "PREVIEW" and decision.decision == "APPROVE":
        raise DomainError("PREVIEW_APPROVAL_FORBIDDEN", "预览不能取得正式导出批准", 409)
    prior = db.scalar(select(HumanReviewDecision).where(HumanReviewDecision.request_id == row.id))
    resumed = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == row.id))
    if prior is not None:
        if prior.actor_id != actor or prior.decision_json != decision.model_dump(mode="json"):
            raise DomainError("REVIEW_DECISION_CONFLICT", "该对象已有不同人工决定", 409)
        # A handoff already committed cannot be applied again; authority is still checked above.
        if resumed is None:
            _current(db, row, registry if registry is not None else handlers())
        return prior
    _current(db, row, registry if registry is not None else handlers())
    result = HumanReviewDecision(
        request_id=row.id, actor_id=actor, decision_json=decision.model_dump(mode="json")
    )
    db.add(result)
    db.flush()
    audit(
        db,
        row.project_id,
        actor,
        "human_review.decided",
        row.id,
        {"decision_id": str(result.id), "decision": decision.decision},
    )
    return result


def record_resume(
    db: Session,
    row: HumanReviewRequest,
    decision: HumanReviewDecision,
    actor: str,
    result: dict,
) -> HumanReviewResume:
    frozen = ReviewSnapshot.model_validate(row.snapshot_json)
    result = ReviewHandoff.model_validate(
        {
            "version_id": frozen.version_id,
            "run_id": frozen.run_id,
            "thread_id": frozen.thread_id,
            **result,
        }
    ).model_dump(mode="json")
    if (
        result["version_id"] != str(frozen.version_id)
        or result["run_id"] != (str(frozen.run_id) if frozen.run_id else None)
        or result["thread_id"] != frozen.thread_id
        or decision.request_id != row.id
    ):
        raise DomainError("REVIEW_RESUME_CONFLICT", "恢复结果不能更换审核身份或精确版本", 409)
    prior = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == row.id))
    if prior is not None:
        if prior.decision_id != decision.id:
            raise DomainError("REVIEW_RESUME_CONFLICT", "审核恢复已绑定其他决定", 409)
        return prior
    receipt = HumanReviewResume(
        request_id=row.id, decision_id=decision.id, actor_id=actor, result_json=result
    )
    db.add(receipt)
    db.flush()
    audit(
        db, row.project_id, actor, "human_review.resumed", row.id, {"decision_id": str(decision.id)}
    )
    return receipt


def resume_review(
    db: Session,
    request_id: UUID,
    actor: str,
    payload: ReviewResume,
    *,
    registry: Mapping[str, ReviewHandler] | None = None,
) -> HumanReviewResume:
    payload = ReviewResume.model_validate(payload.model_dump(mode="json"))
    row = require_review(db, request_id, actor)
    _authority(db, row, actor, owner=True)
    decision = db.scalar(
        select(HumanReviewDecision).where(HumanReviewDecision.request_id == row.id)
    )
    if (
        decision is None
        or decision.id != payload.decision_id
        or row.request_sha256 != payload.request_sha256
    ):
        raise DomainError("REVIEW_DECISION_REQUIRED", "恢复须引用该审核的已保存人工决定", 409)
    # Persisting a decision does not preserve a revoked reviewer's authority.
    # Both the current resumer and the original approver must still be authorized.
    _authority(db, row, decision.actor_id, owner=True)
    prior = db.scalar(select(HumanReviewResume).where(HumanReviewResume.request_id == row.id))
    if prior is not None:
        return prior
    handler = _current(db, row, registry if registry is not None else handlers())
    result = handler.apply(db, row, decision, actor)
    return record_resume(db, row, decision, actor, result)
