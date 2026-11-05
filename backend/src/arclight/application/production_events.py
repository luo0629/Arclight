"""Transactional stage Outbox, authoritative rechecks and atomic handler receipts."""

from collections.abc import Callable, Mapping
from decimal import Decimal
from uuid import UUID, uuid5

from sqlalchemy import exists, select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.commands import require_episode
from arclight.application.dependencies import exact_version
from arclight.application.gateway_accounting import require_priced_history
from arclight.application.generation_accounting import costs
from arclight.application.production_event_handlers import HANDLERS, StageHandler, StagePlan
from arclight.application.production_event_sources import StageSource, current_text, read_source
from arclight.application.production_state import read_production_state
from arclight.core.errors import DomainError
from arclight.domain.production_events import StageEvent, StageReceipt
from arclight.domain.production_state import EpisodeProductionState
from arclight.domain.versions import digest
from arclight.persistence.base import utcnow
from arclight.persistence.models import AuditEvent, OutboxEvent, Project, WorkflowRun
from arclight.persistence.production_events import ProductionEvent, ProductionEventReceipt


def begin_serialized(db: Session) -> None:
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        if driver is not None and not driver.in_transaction:
            connection.exec_driver_sql("BEGIN IMMEDIATE")


def source_lock(db: Session, kind: str, source_id: UUID) -> StageSource:
    begin_serialized(db)
    source = read_source(db, kind, source_id)
    # Follow each existing writer's lock order: text run -> project; media project -> job.
    if kind == "TEXT":
        source = read_source(db, kind, source_id, lock=True)
    db.get(Project, source.project_id, populate_existing=True, with_for_update=True)
    return read_source(db, kind, source_id, lock=True) if kind == "MEDIA" else source


def enqueue_source(db: Session, kind: str, source_id: UUID, actor: str) -> ProductionEvent:
    source = source_lock(db, kind, source_id)
    require_episode(db, source.episode_id, actor, write=True)
    return _record_source(db, kind, source_id, actor, source)


def _record_source(
    db: Session, kind: str, source_id: UUID, actor: str, source: StageSource
) -> ProductionEvent:
    script, board = current_text(db, source.episode_id)
    payload = StageEvent.model_validate(
        {
            "event_id": uuid5(source_id, f"production-event:1:{kind}:{source.revision}"),
            "project_id": source.project_id,
            "episode_id": source.episode_id,
            "source_kind": kind,
            "source_id": source_id,
            "source_revision": source.revision,
            "actor_id": actor,
            "input_sha256": source.input_sha256,
            "script_version_id": script,
            "storyboard_version_id": board,
            "input_version_ids": list(source.input_version_ids),
        }
    )
    value = payload.model_dump(mode="json")
    prior = db.get(ProductionEvent, payload.event_id)
    if prior is not None:
        if prior.request_sha256 != digest(value):
            raise DomainError("STAGE_EVENT_CONFLICT", "同一阶段通知的精确输入不能改变", 409)
        return prior
    row = ProductionEvent(
        id=payload.event_id,
        project_id=source.project_id,
        episode_id=source.episode_id,
        source_kind=kind,
        source_id=source_id,
        source_revision=source.revision,
        request_sha256=digest(value),
        event_json=value,
    )
    db.add(row)
    db.flush()
    db.add(
        OutboxEvent(
            run_id=None,
            production_event_id=row.id,
            revision=source.revision,
            event_type="production.coordinate",
        )
    )
    db.flush()
    return row


def enqueue_text(db: Session, run: WorkflowRun) -> None:
    if run.workflow_type not in {"script_storyboard", "storyboard", "script_rewrite"}:
        return
    actor = db.scalar(
        select(AuditEvent.actor_id)
        .where(AuditEvent.resource_id == run.id, AuditEvent.action == "workflow.started")
        .order_by(AuditEvent.created_at, AuditEvent.id)
        .limit(1)
    )
    # Legacy runs without an initiator cannot acquire a new background grant.
    if actor is not None:
        # Recording an already authorized business change must not acquire a new
        # grant or prevent an Owner decision after the original initiator is revoked.
        # The consumer independently rechecks that initiator before any advancement.
        source = source_lock(db, "TEXT", run.id)
        _record_source(db, "TEXT", run.id, actor, source)


def _check_inputs(db: Session, event: StageEvent, source: StageSource) -> None:
    if (source.project_id, source.episode_id) != (event.project_id, event.episode_id):
        raise DomainError("STAGE_SCOPE_MISMATCH", "阶段来源不属于指定项目或剧集", 409)
    if source.revision != event.source_revision or source.input_sha256 != event.input_sha256:
        raise DomainError("STAGE_SOURCE_STALE", "阶段执行或输入已经变化", 409)
    if list(source.input_version_ids) != event.input_version_ids:
        raise DomainError("STAGE_SOURCE_STALE", "阶段精确输入已经变化", 409)
    if current_text(db, event.episode_id) != (event.script_version_id, event.storyboard_version_id):
        raise DomainError("STAGE_VERSION_STALE", "剧集文本版本已经变化", 409)
    for identity in event.input_version_ids:
        version, asset = exact_version(db, event.project_id, identity)
        if (
            version.stale
            or version.status not in {"APPROVED", "LOCKED"}
            or asset.status in {"STALE", "REJECTED", "ARCHIVED"}
        ):
            raise DomainError("STAGE_INPUT_STALE", "阶段精确资产未批准或已失效", 409)


def _budget(db: Session, event: StageEvent) -> dict[str, str]:
    actual, held = costs(db, event.project_id)
    episode_actual, episode_held = costs(db, event.project_id, episode_id=event.episode_id)
    return {
        "actual": str(actual),
        "reserved": str(held),
        "episode_actual": str(episode_actual),
        "episode_reserved": str(episode_held),
    }


def _check_plan(
    db: Session,
    event: StageEvent,
    plan: StagePlan,
    state: EpisodeProductionState,
    budget: dict[str, str],
) -> None:
    if not plan.maximum_cost.is_finite() or plan.maximum_cost < 0:
        raise DomainError("STAGE_COST_INVALID", "阶段报价无效", 409)
    satisfied = {row.condition for row in state.conditions if row.satisfied}
    if not set(plan.required_conditions) <= satisfied:
        raise DomainError("STAGE_DEPENDENCY_BLOCKED", "阶段依赖或审核尚未满足", 409)
    if plan.maximum_cost == 0:
        return
    require_priced_history(db, event.project_id)
    from arclight.application.generation_budget import policy_for

    policy = policy_for(db, event.project_id)
    project = db.get(Project, event.project_id)
    assert project is not None
    if (
        Decimal(budget["actual"]) + Decimal(budget["reserved"]) + plan.maximum_cost
        > project.budget_limit
    ):
        raise DomainError("BUDGET_EXCEEDED", "阶段将超过项目预算", 409)
    if policy.episode_limit is not None and (
        Decimal(budget["episode_actual"]) + Decimal(budget["episode_reserved"]) + plan.maximum_cost
        > policy.episode_limit
    ):
        raise DomainError("BUDGET_EPISODE_EXCEEDED", "阶段将超过剧集预算", 409)
    if policy.task_limit is not None and plan.maximum_cost > policy.task_limit:
        raise DomainError("BUDGET_TASK_EXCEEDED", "阶段将超过单任务预算", 409)


def consume_stage_event(
    factory: sessionmaker[Session],
    event_id: UUID,
    *,
    handlers: Mapping[str, StageHandler] = HANDLERS,
) -> StageReceipt:
    return _consume_stage_event(factory, event_id, handlers=handlers)


def recheck_stage_event(
    factory: sessionmaker[Session],
    event_id: UUID,
    actor: str,
    request_id: UUID,
    *,
    handlers: Mapping[str, StageHandler] = HANDLERS,
) -> StageReceipt:
    """Explicitly resume a blocked evaluation; immutable earlier evidence is retained.

    Rechecks do not grant another actor's authority or reinterpret changed inputs.
    External execution still performs the original runtime's admission checks.
    """
    if request_id == event_id:
        raise DomainError("STAGE_RECHECK_KEY_INVALID", "重新检查必须使用独立请求标识", 422)
    return _consume_stage_event(
        factory, event_id, handlers=handlers, evaluation_key=request_id, actor=actor
    )


def _consume_stage_event(
    factory: sessionmaker[Session],
    event_id: UUID,
    *,
    handlers: Mapping[str, StageHandler],
    evaluation_key: UUID | None = None,
    actor: str | None = None,
) -> StageReceipt:
    with factory.begin() as db:
        begin_serialized(db)
        row = db.get(ProductionEvent, event_id)
        if row is None:
            raise DomainError("NOT_FOUND", "阶段通知不存在", 404)
        event = StageEvent.model_validate(row.event_json)
        if (
            event.event_id,
            event.project_id,
            event.episode_id,
            event.source_kind,
            event.source_id,
            event.source_revision,
            digest(row.event_json),
        ) != (
            row.id,
            row.project_id,
            row.episode_id,
            row.source_kind,
            row.source_id,
            row.source_revision,
            row.request_sha256,
        ):
            raise DomainError("STAGE_EVENT_INVALID", "阶段通知身份或指纹无效", 409)
        source_error = None
        try:
            source = source_lock(db, event.source_kind, event.source_id)
        except DomainError as error:
            # No source row remains to lock. The project gate still serializes
            # duplicate observations, and the missing source can never advance.
            db.get(Project, event.project_id, populate_existing=True, with_for_update=True)
            source_error = error
        statement = select(ProductionEventReceipt).where(
            ProductionEventReceipt.event_id == event_id
        )
        latest = db.scalar(
            statement.order_by(
                ProductionEventReceipt.created_at.desc(), ProductionEventReceipt.id.desc()
            ).limit(1)
        )
        terminal = db.scalar(
            statement.where(
                ProductionEventReceipt.result_json["disposition"]
                .as_string()
                .in_(("APPLIED", "STALE"))
            )
        )
        # A terminal evaluation wins independently of clock resolution or skew.
        # Blocked observations can be retried, but no request can repeat an effect.
        latest = terminal or latest
        if evaluation_key is not None:
            if actor != event.actor_id:
                raise DomainError("STAGE_ACTOR_MISMATCH", "重新检查不能转授原发起人的权限", 403)
            require_episode(db, event.episode_id, actor, write=True)
            prior = db.scalar(
                statement.where(ProductionEventReceipt.evaluation_key == evaluation_key)
            )
            if prior is not None:
                return StageReceipt.model_validate(prior.result_json)
            if latest is None:
                raise DomainError("STAGE_NOT_EVALUATED", "请先处理原阶段通知", 409)
        if latest is not None and (
            evaluation_key is None or latest.result_json["disposition"] != "BLOCKED"
        ):
            return StageReceipt.model_validate(latest.result_json)
        key = evaluation_key or event_id
        state = None
        budget: dict[str, str] = {}
        try:
            require_episode(db, event.episode_id, event.actor_id, write=True)
            if source_error is not None:
                raise source_error
            _check_inputs(db, event, source)
            state = read_production_state(db, event.episode_id, event.actor_id)
            budget = _budget(db, event)
            handler = handlers.get(event.source_kind)
            if handler is None:
                raise DomainError("STAGE_HANDLER_UNAVAILABLE", "阶段处理器尚未接入", 409)
            plan = handler.prepare(db, event, state)
            _check_plan(db, event, plan, state, budget)
        except DomainError as error:
            receipt = StageReceipt(
                event_id=event_id,
                evaluation_key=key,
                evaluated_by=event.actor_id,
                disposition="STALE"
                if error.code in {"STAGE_SOURCE_STALE", "STAGE_VERSION_STALE", "STAGE_INPUT_STALE"}
                else "BLOCKED",
                reason=error.code,
                action="OBSERVED",
                state=state,
                budget=budget,
            )
        else:
            # Handler writes and the receipt commit together. Exceptions roll back both;
            # broker redelivery safely retries without an external call under the lock.
            handler.advance(db, event, plan, state)
            receipt = StageReceipt(
                event_id=event_id,
                evaluation_key=key,
                evaluated_by=event.actor_id,
                disposition="APPLIED",
                reason="RECONCILED",
                action=plan.action,
                state=state,
                budget=budget,
            )
        db.add(
            ProductionEventReceipt(
                event_id=event_id, evaluation_key=key, result_json=receipt.model_dump(mode="json")
            )
        )
        db.flush()
        return receipt


def publish_stage_events(factory: sessionmaker[Session], publish: Callable[[UUID], None]) -> int:
    with factory.begin() as db:
        pending = list(
            db.scalars(
                select(OutboxEvent.production_event_id)
                .where(
                    OutboxEvent.production_event_id.is_not(None),
                    ~exists().where(
                        ProductionEventReceipt.event_id == OutboxEvent.production_event_id
                    ),
                )
                .order_by(OutboxEvent.created_at, OutboxEvent.id)
                .limit(100)
            )
        )
    count = 0
    # Publish outside business locks; no receipt means the next scheduler tick retries,
    # including a lost message whose published_at was already saved.
    for identity in pending:
        assert identity is not None
        publish(identity)
        with factory.begin() as db:
            message = db.scalar(
                select(OutboxEvent).where(OutboxEvent.production_event_id == identity)
            )
            if message is not None:
                message.published_at = utcnow()
                message.attempt_count += 1
        count += 1
    return count
