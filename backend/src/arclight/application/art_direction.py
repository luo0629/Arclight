"""Art-domain adapter over the existing durable runtime; it publishes no asset."""

from uuid import UUID, uuid5

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_recovery import resume_agent_loop, run_persistent_agent_loop
from arclight.application.agent_loop_store import checked_loop
from arclight.application.agent_tools import (
    AgentToolContext,
    AgentToolDefinition,
    AgentToolRegistry,
    available_agent_tools,
    build_agent_tool_registry,
)
from arclight.application.art_direction_evidence import (
    operation_sha256,
    validate_delivery,
    validate_draft,
)
from arclight.application.commands import require_run
from arclight.application.director_research import approved_input
from arclight.application.gateway_text import execution_actor, validate_publication
from arclight.application.model_gateway import lock_gate
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentHumanDecision, AgentLoopResult
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_direction import (
    COMPARISON_TOOL,
    DIRECTION_PROMPT,
    DIRECTION_VERSION,
    ArtDirectionResult,
    ArtDirectionSeed,
    ArtDirectionTask,
    CompareArtParameters,
    CompareArtResult,
)
from arclight.domain.art_proposals import VersionArtSource
from arclight.domain.versions import digest
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, WorkflowRun


def comparison_tool(settings: Settings) -> AgentToolDefinition:
    def compare(context: AgentToolContext, parameters: AgentToolContract) -> dict:
        assert isinstance(parameters, CompareArtParameters)
        run = context.db.get(WorkflowRun, context.run_id)
        assert run is not None
        comparison = validate_draft(context.db, settings, run, context.actor_id, parameters.draft)
        return CompareArtResult(
            draft_sha256=digest(parameters.draft.model_dump(mode="json")), comparison=comparison
        ).model_dump(mode="json")

    return AgentToolDefinition(
        name=COMPARISON_TOOL,
        allowed_agents=frozenset({"art_director"}),
        allowed_steps=frozenset({"storyboard"}),
        parameters_model=CompareArtParameters,
        result_model=CompareArtResult,
        scope_validator=lambda context, parameters: None,
        handler=compare,
        requires_write=True,
    )


def art_direction_registry(settings: Settings) -> AgentToolRegistry:
    registry = build_agent_tool_registry(settings=settings)
    investigate, compare = registry.get("investigate_art"), registry.get(COMPARISON_TOOL)
    assert investigate is not None and compare is not None
    from arclight.application.art_concepts import concept_tools

    return AgentToolRegistry([investigate, compare, *concept_tools(settings)])


def validate_art_direction(
    db: Session, settings: Settings, run_id: UUID, result: ArtDirectionResult
) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    validate_publication(db, run.id)
    actor = execution_actor(db, run)
    permitted = available_agent_tools(
        db, art_direction_registry(settings), run.id, "art_director", actor, "storyboard"
    )
    required = {"investigate_art", COMPARISON_TOOL}
    if run.input_json.get("art_direction", {}).get("concept"):
        required |= {"request_art_concepts", "observe_art_concepts"}
    if {tool.name for tool in permitted} != required:
        raise DomainError("USER_NOT_AUTHORIZED", "美术决策工具授权已撤销", 403)
    validate_delivery(db, settings, run, actor, result)


def propose_art(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    actor: str,
    task: ArtDirectionTask,
    *,
    limits: AgentRunLimits | None = None,
) -> AgentLoopResult[ArtDirectionResult]:
    """Investigate and compare an active step; caller grants the two read-only tools."""
    task = ArtDirectionTask.model_validate(task.model_dump(mode="json"))
    with factory.begin() as db:
        lock_gate(db)
        run = require_run(db, run_id, actor, lock=True)
        validate_publication(db, run.id)
        if actor != execution_actor(db, run):
            raise DomainError("FORBIDDEN", "美术决策须使用原运行发起者", 403)
        script, trace = approved_input(db, run, actor, settings)
        visual = trace["task_context"]["required"]["global"]["style"]
        episode = db.get(Episode, run.episode_id)
        assert episode is not None
        seed = ArtDirectionSeed(
            project_id=episode.project_id,
            episode_id=episode.id,
            script_version_id=script.id,
            script_sha256=digest(script.content_json),
            visual_bible_source=VersionArtSource(
                kind="VERSION",
                source_id=uuid5(run.id, "art-direction-visual"),
                resource_type="VISUAL_BIBLE",
                resource_id=UUID(visual["version_id"]),
                content_sha256=digest(visual),
            )
            if visual["version_id"]
            else None,
            task=task,
        )
        previous = db.scalar(
            select(AgentLoop).where(
                AgentLoop.run_id == run.id,
                AgentLoop.operation_sha256 == operation_sha256(run.id, db=db),
            )
        )
        from arclight.domain.art_concepts import CONCEPT_DIRECTION_VERSION, CONCEPT_PROMPT

        concept = run.input_json.get("art_direction", {}).get("concept")
        direction_version = CONCEPT_DIRECTION_VERSION if concept else DIRECTION_VERSION
        from arclight.application.director_collaboration import opinion_scope, requested_opinion
        from arclight.domain.director_collaboration import (
            OPINION_PROMPT,
            OPINION_VERSION,
            RequestArtOpinion,
        )
        from arclight.domain.director_creation import COLLABORATIVE_DIRECTOR_VERSIONS

        opinion = (
            requested_opinion(db, run.id)
            if run.workflow_version in COLLABORATIVE_DIRECTOR_VERSIONS
            else None
        )
        if run.workflow_version in COLLABORATIVE_DIRECTOR_VERSIONS:
            if opinion is None or opinion.task != task:
                raise DomainError("ART_OPINION_NOT_FOUND", "须先保存原精确导演意见委派", 409)
            opinion_scope(
                db,
                run,
                RequestArtOpinion.model_validate(
                    opinion.model_dump(include={"operation_key", "source_sha256", "task"})
                ),
                settings,
            )
            if (
                opinion.script_version_id != script.id
                or opinion.script_sha256 != seed.script_sha256
            ):
                raise DomainError("ART_OPINION_STALE", "意见不能更换原精确批准剧本", 409)
            direction_version = OPINION_VERSION
        request = AgentModelRequest(
            run_id=run.id,
            agent_name="art_director",
            stage="storyboard",
            operation_key=direction_version,
            agent_version=direction_version,
            prompt_version=direction_version,
            result_schema_version=DIRECTION_VERSION,
            system_instruction=DIRECTION_PROMPT
            + (CONCEPT_PROMPT if concept else "")
            + (OPINION_PROMPT if opinion is not None else ""),
            max_tokens=16000,
            messages=[
                AgentUserMessage(data=seed.model_dump(mode="json")),
                *([AgentUserMessage(data={"concept_plan": concept})] if concept else []),
                *(
                    [AgentUserMessage(data={"director_opinion": opinion.model_dump(mode="json")})]
                    if opinion is not None
                    else []
                ),
            ],
        )
        if previous:
            saved = AgentModelRequest.model_validate(previous.seed_json)
            if saved.messages != request.messages:
                raise DomainError("ART_TASK_CONFLICT", "同一美术决策不能更换冻结目标或输入", 409)
            request = saved
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        art_direction_registry(settings),
        ArtDirectionResult,
        limits=limits,
        validate_result=lambda db, result: validate_art_direction(db, settings, run_id, result),
    )


def resume_art_direction(
    factory: sessionmaker[Session],
    settings: Settings,
    loop_id: UUID,
    actor: str,
    *,
    decision: AgentHumanDecision | None = None,
) -> AgentLoopResult[ArtDirectionResult]:
    with factory.begin() as db:
        loop = checked_loop(db, loop_id, actor=actor)
        run_id = loop.run_id
        if loop.operation_sha256 != operation_sha256(run_id, db=db):
            raise DomainError("ART_DIRECTION_INVALID", "只能恢复原美术决策循环", 409)
    return resume_agent_loop(
        factory,
        settings,
        loop_id,
        actor,
        art_direction_registry(settings),
        ArtDirectionResult,
        decision=decision,
        validate_result=lambda db, result: validate_art_direction(db, settings, run_id, result),
    )
