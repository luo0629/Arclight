"""Advice uses the original art Runtime in the director's existing run and budget."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_limits import delegation_count, validate_agent_execution
from arclight.application.agent_tools import AgentToolContext, AgentToolDefinition
from arclight.application.director_checks import check_seed
from arclight.application.director_validation import revision_input
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.director_collaboration import (
    OBSERVE_OPINION,
    REQUEST_OPINION,
    ArtOpinionObserved,
    ArtOpinionRequested,
    CollaborativeDirectorDecision,
    ObserveArtOpinion,
    RequestArtOpinion,
)
from arclight.domain.director_creation import COLLABORATIVE_DIRECTOR_VERSIONS
from arclight.domain.director_revision import DirectorRevisionDecision
from arclight.domain.versions import digest
from arclight.persistence.agent_limits import AgentRunLimit
from arclight.persistence.agent_loops import AgentLoop, AgentLoopAction
from arclight.persistence.models import AgentToolInvocation, WorkflowRun


def requested_opinion(db: Session, run_id: UUID) -> ArtOpinionRequested | None:
    rows = db.scalars(
        select(AgentToolInvocation).where(
            AgentToolInvocation.run_id == run_id,
            AgentToolInvocation.tool_name == REQUEST_OPINION,
            AgentToolInvocation.status == "SUCCEEDED",
        )
    )
    values = [
        ArtOpinionRequested.model_validate((row.feedback_json or {})["result"]) for row in rows
    ]
    if not values:
        return None
    if any(value != values[0] for value in values):
        raise DomainError("ART_OPINION_CONFLICT", "同一导演运行的意见委派不一致", 409)
    return values[0]


def opinion_scope(
    db: Session,
    run: WorkflowRun,
    request: RequestArtOpinion | ObserveArtOpinion,
    settings: Settings,
) -> None:
    if run.workflow_version not in COLLABORATIVE_DIRECTOR_VERSIONS:
        raise DomainError("ART_OPINION_FORBIDDEN", "此运行未授权导演美术协作", 403)
    _, seed = check_seed(db, run, request.operation_key)
    if seed.source_sha256 != request.source_sha256:
        raise DomainError("ART_OPINION_STALE", "意见须绑定原精确工作中镜头方案", 409)
    revision_input(db, run, seed.plan, settings)


def _request(context: AgentToolContext, request: RequestArtOpinion) -> dict:
    db = context.db
    run = db.get(WorkflowRun, context.run_id)
    assert run is not None
    previous = requested_opinion(db, run.id)
    if previous is not None:
        if RequestArtOpinion.model_validate(request.model_dump(mode="json")) != (
            RequestArtOpinion.model_validate(
                previous.model_dump(include={"operation_key", "source_sha256", "task"})
            )
        ):
            raise DomainError("ART_OPINION_CONFLICT", "一次导演运行最多征询一份冻结意见", 409)
        return previous.model_dump(mode="json")
    limits = validate_agent_execution(db, run.id, "storyboard")
    control = db.get(AgentRunLimit, run.id)
    assert control is not None
    if control.depth + 1 > limits.max_delegation_depth:
        raise DomainError("AGENT_DELEGATION_DEPTH", "Agent 委派深度已达上限", 409)
    if delegation_count(db, control.root_run_id) >= limits.max_delegations:
        raise DomainError("AGENT_DELEGATION_LIMIT", "Agent 委派次数已达上限", 409)
    _, seed = check_seed(db, run, request.operation_key)
    return ArtOpinionRequested(
        **request.model_dump(),
        delegation_id=uuid5(run.id, "director-art-opinion"),
        script_version_id=seed.plan.script_version_id,
        script_sha256=seed.plan.script_sha256,
        working_storyboard=seed.source,
    ).model_dump(mode="json")


def _requested(context: AgentToolContext, request: ObserveArtOpinion) -> ArtOpinionRequested:
    saved = requested_opinion(context.db, context.run_id)
    if saved is None or any(
        getattr(saved, field) != getattr(request, field)
        for field in ("delegation_id", "operation_key", "source_sha256")
    ):
        raise DomainError("ART_OPINION_NOT_FOUND", "原精确意见委派不存在", 404)
    return saved


def _opinion_loop(db: Session, run_id: UUID) -> AgentLoop | None:
    from arclight.application.art_direction_evidence import direction_loop

    if requested_opinion(db, run_id) is None:
        return None
    try:
        return direction_loop(db, run_id)[0]
    except DomainError as exc:
        if exc.code != "ART_DIRECTION_INVALID":
            raise
        return None


def collaboration_tools(settings: Settings) -> list[AgentToolDefinition]:
    def scope(context: AgentToolContext, parameters: AgentToolContract) -> None:
        assert isinstance(parameters, RequestArtOpinion | ObserveArtOpinion)
        run = context.db.get(WorkflowRun, context.run_id)
        assert run is not None
        opinion_scope(context.db, run, parameters, settings)
        if isinstance(parameters, RequestArtOpinion):
            from arclight.application.generation_leases import clock
            from arclight.application.model_gateway import utc

            loop, _ = check_seed(context.db, run, parameters.operation_key)
            if (
                loop.lease_token is None
                or loop.lease_expires_at is None
                or utc(loop.lease_expires_at) <= clock(context.db)
                or loop.status not in {"RUNNING", "WAITING_HUMAN", "RECOVERY_REQUIRED"}
            ):
                raise DomainError("ART_OPINION_STALE", "只能委派当前活动的导演工作稿", 409)

    def request(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, RequestArtOpinion)
        return _request(context, parameters)

    def ready(context: AgentToolContext, parameters: AgentToolContract) -> bool:
        assert isinstance(parameters, ObserveArtOpinion)
        _requested(context, parameters)
        loop = _opinion_loop(context.db, context.run_id)
        return loop is not None and loop.status in {"COMPLETED", "STOPPED"}

    def observe(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, ObserveArtOpinion)
        saved = _requested(context, parameters)
        loop = _opinion_loop(context.db, context.run_id)
        if loop is None or loop.status not in {"COMPLETED", "STOPPED"}:
            raise DomainError("ART_OPINION_PENDING", "美术意见尚未完成", 409)
        outcome = loop.outcome_json or {}
        if loop.status == "COMPLETED":
            from arclight.application.art_direction import validate_art_direction
            from arclight.domain.art_direction import ArtDirectionResult

            validate_art_direction(
                context.db,
                settings,
                context.run_id,
                ArtDirectionResult.model_validate(outcome["result"]),
            )
        code = outcome.get("stop_code")
        value = {
            "delegation_id": str(saved.delegation_id),
            "source_sha256": saved.source_sha256,
            "status": "ADVICE"
            if loop.status == "COMPLETED"
            else ("TIMED_OUT" if code and ("TIMEOUT" in code or "DEADLINE" in code) else "REFUSED"),
            "advice": outcome.get("result") if loop.status == "COMPLETED" else None,
            "stop_code": code,
        }
        return value | {"observation_sha256": digest(value)}

    return [
        AgentToolDefinition(
            name=REQUEST_OPINION,
            allowed_agents=frozenset({"director"}),
            allowed_steps=frozenset({"storyboard"}),
            parameters_model=RequestArtOpinion,
            result_model=ArtOpinionRequested,
            scope_validator=scope,
            handler=request,
            requires_write=True,
        ),
        AgentToolDefinition(
            name=OBSERVE_OPINION,
            allowed_agents=frozenset({"director"}),
            allowed_steps=frozenset({"storyboard"}),
            parameters_model=ObserveArtOpinion,
            result_model=ArtOpinionObserved,
            scope_validator=scope,
            handler=observe,
            is_ready=ready,
        ),
    ]


def validate_opinion_decision(
    db: Session, loop_id: UUID, report: DirectorRevisionDecision, settings: Settings
) -> None:
    if not isinstance(report, CollaborativeDirectorDecision):
        return
    actions = list(db.scalars(select(AgentLoopAction).where(AgentLoopAction.loop_id == loop_id)))
    requested = any(
        row.kind == "TOOL"
        and row.request_json.get("tool_name") == REQUEST_OPINION
        and (row.result_json or {}).get("status") == "SUCCEEDED"
        for row in actions
    )
    observed = [
        ArtOpinionObserved.model_validate((row.result_json or {})["result"])
        for row in actions
        if row.kind == "TOOL"
        and row.request_json.get("tool_name") == OBSERVE_OPINION
        and (row.result_json or {}).get("status") == "SUCCEEDED"
    ]
    choice = report.opinion_decision
    if not observed:
        if requested or choice is not None:
            raise DomainError("ART_OPINION_UNVERIFIED", "须先读取真实意见并记录采纳决定", 422)
        return
    if choice is None or not any(
        item.delegation_id == choice.delegation_id
        and item.observation_sha256 == choice.observation_sha256
        and (choice.decision == "DECLINE" or item.status == "ADVICE")
        for item in observed
    ):
        raise DomainError("ART_OPINION_UNVERIFIED", "采纳决定须绑定真实意见回执", 422)
    selected = next(
        item for item in observed if item.observation_sha256 == choice.observation_sha256
    )
    if selected.advice is not None:
        from arclight.application.art_direction import validate_art_direction
        from arclight.persistence.agent_loops import AgentLoop

        owner = db.get(AgentLoop, loop_id)
        assert owner is not None
        validate_art_direction(db, settings, owner.run_id, selected.advice)


def drive_opinion(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    outcome: AgentLoopResult[DirectorRevisionDecision],
) -> AgentLoopResult[DirectorRevisionDecision] | None:
    """Run art only after the director's tool intent and wait checkpoint have committed."""
    if outcome.status != "WAITING_EXTERNAL" or outcome.waiting_for is None:
        return outcome
    if outcome.waiting_for.tool_name != OBSERVE_OPINION:
        return outcome
    from arclight.application.art_direction import propose_art
    from arclight.application.gateway_text import execution_actor

    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        actor = execution_actor(db, run)
        saved = requested_opinion(db, run_id)
        assert saved is not None
        request = ObserveArtOpinion.model_validate(outcome.waiting_for.arguments)
        opinion_scope(db, run, request, settings)
        if request.delegation_id != saved.delegation_id:
            raise DomainError("ART_OPINION_NOT_FOUND", "原精确意见委派不存在", 404)
    advice = propose_art(factory, settings, run_id, actor, saved.task)
    if advice.stop_code == "GATEWAY_RECONCILIATION_REQUIRED":
        raise DomainError("GATEWAY_RECONCILIATION_REQUIRED", "原美术调用结果未知，须先核账", 409)
    if advice.status in {"COMPLETED", "STOPPED"}:
        return None  # The parent resumes the original observe tool and records its own choice.
    return AgentLoopResult[DirectorRevisionDecision].model_validate(advice.model_dump(mode="json"))
