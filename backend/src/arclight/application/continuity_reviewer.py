"""Run and resume the reviewer using existing tools and the persistent Agent runtime."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_loop_store import checked_loop
from arclight.application.agent_tools import (
    AgentToolRegistry,
    available_agent_tools,
    build_agent_tool_registry,
)
from arclight.application.commands import require_run
from arclight.application.continuity_reviewer_evidence import validate_reviewer_delivery
from arclight.application.continuity_reviews import require_review_target, shot_sha256
from arclight.application.dependencies import exact_version, require_manifest
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.application.model_services import run_settings
from arclight.application.production_versions import component, text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.continuity_escalations import LIMIT_STOP_CODES
from arclight.domain.continuity_research import RESEARCH_TOOL
from arclight.domain.continuity_reviewer import (
    REVIEWER_PROMPT,
    REVIEWER_VERSION,
    ReviewLoopSeed,
    review_operation,
)
from arclight.domain.continuity_reviews import (
    REVIEW_SCHEMA_VERSION,
    ContinuityReview,
    ReviewReference,
    ReviewRerun,
    ReviewTarget,
    review_schema_registration,
)
from arclight.domain.continuity_rules import RULES_TOOL, RULES_VERSION
from arclight.domain.versions import digest
from arclight.domain.workflow_contracts import ProductionComponent
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, WorkflowRun


def reviewer_registry(settings: Settings) -> AgentToolRegistry:
    registry = build_agent_tool_registry(settings=settings)
    tools = [registry.get(name) for name in (RESEARCH_TOOL, RULES_TOOL)]
    assert all(tool is not None for tool in tools)
    return AgentToolRegistry([tool for tool in tools if tool is not None])


def authorize_reviewer(db: Session, settings: Settings, run: WorkflowRun, actor: str) -> Episode:
    validate_publication(db, run.id)
    if actor != execution_actor(db, run):
        raise DomainError("FORBIDDEN", "审查须使用原运行发起者", 403)
    tools = available_agent_tools(
        db, reviewer_registry(settings), run.id, "continuity_reviewer", actor, "storyboard"
    )
    if {tool.name for tool in tools} != {RESEARCH_TOOL, RULES_TOOL}:
        raise DomainError("USER_NOT_AUTHORIZED", "审查需要原取证及比较工具的显式授权", 403)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    return episode


def reviewer_seed(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    actor: str,
    target: ReviewTarget,
    rerun: ReviewRerun | None,
) -> ReviewLoopSeed:
    episode = authorize_reviewer(db, settings, run, actor)
    version, board, shot = require_review_target(db, episode.project_id, episode.id, target, actor)
    identity = uuid5(run.id, review_operation(target))
    references = [
        ReviewReference.model_validate(
            {
                "reference_id": uuid5(identity, f"{role}:{resource}"),
                "role": role,
                "resource_id": resource,
                "content_sha256": checksum,
            }
        )
        for role, resource, checksum in (
            ("CANDIDATE", target.candidate_id, version.checksum),
            ("TARGET_SHOT", shot.id, shot_sha256(shot)),
            ("STORYBOARD", board.id, digest(board.content_json)),
        )
    ]
    manifest = require_manifest(db, episode.project_id, target.input_manifest_id)
    source_ids = {
        UUID(item["source_asset_version_id"]) for item in manifest.snapshot_json["inputs"]
    }
    source_ids.add(board.script_version_id)
    for source_id in sorted(source_ids):
        if source_id == board.id:
            continue
        source, asset = exact_version(db, episode.project_id, source_id)
        references.append(
            ReviewReference.model_validate(
                {
                    "reference_id": uuid5(identity, f"REFERENCE:{source.id}"),
                    "role": "REFERENCE",
                    "resource_id": source.id,
                    "content_sha256": source.checksum,
                    "asset_id": asset.id,
                    "asset_type": asset.asset_type,
                }
            )
        )
    actual = run_settings(
        text_settings(db, episode.project_id, run, settings),
        run.input_json.get("_text_service"),
        db,
    )
    capabilities = actual.llm_agent_capabilities
    if capabilities is None:
        raise DomainError("GATEWAY_CAPABILITY_REJECTED", "审查需要已声明的多图及工具能力", 422)
    definitions: list[tuple[str, str, dict]] = [
        ("AGENT", REVIEWER_VERSION, {"tools": [RESEARCH_TOOL, RULES_TOOL], "rules": RULES_VERSION}),
        ("PROMPT", REVIEWER_VERSION, {"instruction": REVIEWER_PROMPT}),
        ("SCHEMA", REVIEW_SCHEMA_VERSION, review_schema_registration(costume=True).definition),
        (
            "MODEL",
            capabilities.version,
            {
                "provider": actual.llm_supplier,
                "model": actual.llm_model,
                "config_sha256": digest(
                    {
                        "base_url": actual.llm_base_url,
                        "supplier": actual.llm_supplier,
                        "model": actual.llm_model,
                        "response_format": actual.llm_response_format,
                        "agent": capabilities.model_dump(mode="json"),
                    }
                ),
            },
        ),
    ]
    schema = review_schema_registration()
    versions = [
        component(
            db,
            episode.project_id,
            "REVIEW",
            kind,
            schema.name if kind == "SCHEMA" else "continuity_reviewer." + kind.lower(),
            revision,
            definition,
        )
        for kind, revision, definition in definitions
    ]
    return ReviewLoopSeed(
        project_id=episode.project_id,
        episode_id=episode.id,
        result_id=identity,
        target=target,
        versions=[ProductionComponent.model_validate(item) for item in versions],
        required_references=references,
        rerun=rerun,
    )


def review_candidate(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    target: ReviewTarget,
    *,
    rerun: ReviewRerun | None = None,
    limits: AgentRunLimits | None = None,
) -> AgentLoopResult[ContinuityReview]:
    target = ReviewTarget.model_validate(target.model_dump(mode="json"))
    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        operation = review_operation(target)
        previous = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id,
                AgentLoop.operation_sha256
                == digest({"run": str(run.id), "stage": "storyboard", "operation": operation}),
            )
        )
        if previous is not None:
            request = AgentModelRequest.model_validate(previous.seed_json)
            first = request.messages[0]
            assert isinstance(first, AgentUserMessage)
            seed = ReviewLoopSeed.model_validate(first.data)
            if seed.target != target or seed.rerun != rerun:
                raise DomainError("REVIEW_TASK_CONFLICT", "重入不能更换原审查目标或重跑链", 409)
            stopped = (
                AgentLoopResult[ContinuityReview].model_validate(previous.outcome_json)
                if (previous.status == "STOPPED")
                else None
            )
            if stopped is not None and stopped.stop_code in LIMIT_STOP_CODES:
                from arclight.application.continuity_escalations import request_escalation
                from arclight.persistence.agent_limits import AgentRunLimit

                control = db.get(AgentRunLimit, run_id)
                if limits is not None and (
                    control is None or AgentRunLimits.model_validate(control.limits_json) != limits
                ):
                    raise DomainError("AGENT_LIMITS_CONFLICT", "重入不能更换原审查运行限制", 409)

                # Repair a committed stop's missing human wait after a crash, even
                # after its execution deadline. This authorizes no new execution.
                request_escalation(db, run_id, actor, seed.target)
                return stopped
        episode = authorize_reviewer(db, settings, run, actor)
        require_review_target(db, episode.project_id, episode.id, target, actor)
        if previous is None:
            seed = reviewer_seed(db, settings, run, actor, target, rerun)
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="continuity_reviewer",
                stage="storyboard",
                operation_key=operation,
                agent_version=REVIEWER_VERSION,
                prompt_version=REVIEWER_VERSION,
                result_schema_version=REVIEW_SCHEMA_VERSION,
                system_instruction=REVIEWER_PROMPT,
                max_tokens=16000,
                messages=[AgentUserMessage(data=seed.model_dump(mode="json"))],
            )
    outcome = run_persistent_agent_loop(
        factory,
        settings,
        request,
        reviewer_registry(settings),
        ContinuityReview,
        limits=limits,
        validate_result=lambda db, result: validate_reviewer_result(db, settings, run_id, result),
    )
    if outcome.status == "STOPPED" and outcome.stop_code in LIMIT_STOP_CODES:
        from arclight.application.continuity_escalations import request_escalation

        with factory.begin() as db:
            request_escalation(db, run_id, actor, seed.target)
    return outcome


def validate_reviewer_result(
    db: Session, settings: Settings, run_id: UUID, result: ContinuityReview
):
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    actor = execution_actor(db, run)
    authorize_reviewer(db, settings, run, actor)
    validate_reviewer_delivery(db, settings, run, actor, result)


def resume_reviewer(
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[ContinuityReview]:
    with factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        request = AgentModelRequest.model_validate(loop.seed_json)
        first = request.messages[0]
        if request.agent_name != "continuity_reviewer" or not isinstance(first, AgentUserMessage):
            raise DomainError("REVIEW_LOOP_INVALID", "只能恢复原连续性审查循环", 409)
        seed = ReviewLoopSeed.model_validate(first.data)
        if request.operation_key != review_operation(seed.target):
            raise DomainError("REVIEW_LOOP_INVALID", "审查循环操作身份不匹配", 409)
        run = db.get(WorkflowRun, loop.run_id)
        assert run is not None
        episode = authorize_reviewer(db, settings, run, loop.actor_id)
        require_review_target(db, episode.project_id, episode.id, seed.target, loop.actor_id)
        run_id = loop.run_id
        initiator = loop.actor_id
    outcome = resume_agent_loop(
        factory,
        settings,
        loop_id,
        actor,
        reviewer_registry(settings),
        ContinuityReview,
        decision=decision,
        validate_result=lambda db, result: validate_reviewer_result(db, settings, run_id, result),
    )
    if outcome.status == "STOPPED" and outcome.stop_code in LIMIT_STOP_CODES:
        from arclight.application.continuity_escalations import request_escalation

        with factory.begin() as db:
            request_escalation(db, run_id, initiator, seed.target)
    return outcome
