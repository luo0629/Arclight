"""Professional creation uses the existing journal, candidates and review graph."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import CharacterAsset, ScriptContent
from arclight.application import screenwriter_planning as planning
from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.application.agent_tools import (
    build_agent_tool_registry,
    grant_agent_tools,
)
from arclight.application.commands import require_run
from arclight.application.content_references import validate_script_assets
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.screenwriter_creation import (
    FRAGMENT_PROMPT,
    FRAGMENT_VERSION,
    SCREENWRITER_OUTLINE_VERSION,
    SCREENWRITER_VERSION,
    FragmentSeed,
    ScriptFragment,
)
from arclight.domain.screenwriter_planning import PLAN_VERSION, EpisodePlan, ScreenwriterPlan
from arclight.persistence.agent_loops import AgentLoop, AgentLoopDecision, AgentLoopQuestion
from arclight.persistence.models import Episode, WorkflowRun

PROFESSIONAL_VERSIONS = {SCREENWRITER_VERSION, SCREENWRITER_OUTLINE_VERSION}


def authorize_screenwriter(db: Session, run: WorkflowRun, actor: str, settings: Settings) -> None:
    # Choosing professional creation explicitly authorizes this read-only investigation.
    grant_agent_tools(
        db,
        AgentToolGrantRequest(
            run_id=run.id, agent_name="screenwriter", allowed_tools=["investigate_story"]
        ),
        actor,
        build_agent_tool_registry(settings=settings),
    )


def creation_attempt(run: WorkflowRun) -> int:
    return int(
        (run.agent_state_json or {})
        .get("extensions", {})
        .get("screenwriter.creation", {})
        .get("attempt", 0)
    )


def creation_loops(db: Session, run_id: UUID) -> list[AgentLoop]:
    return list(
        db.scalars(
            select(AgentLoop)
            .where(AgentLoop.run_id == run_id)
            .order_by(AgentLoop.created_at, AgentLoop.id)
        )
    )


def waiting_for_answer(db: Session, run: WorkflowRun) -> bool:
    """Cancellation needs no model admission, but cannot interrupt an answered question."""
    if run.workflow_version not in PROFESSIONAL_VERSIONS or run.status != "RUNNING":
        return False
    for loop in creation_loops(db, run.id):
        identity = (loop.outcome_json or {}).get("question_id")
        if loop.status != "WAITING_HUMAN" or identity is None:
            continue
        question = db.get(AgentLoopQuestion, UUID(identity))
        if (
            question is not None
            and question.loop_id == loop.id
            and not db.scalar(
                select(AgentLoopDecision.id).where(AgentLoopDecision.question_id == question.id)
            )
        ):
            return True
    return False


def operation(loop: AgentLoop) -> str:
    return str(loop.seed_json.get("operation_key", ""))


def _is_plan(loop: AgentLoop) -> bool:
    return operation(loop) == PLAN_VERSION or operation(loop).startswith(PLAN_VERSION + ":attempt:")


def create_plan(factory: sessionmaker[Session], settings: Settings, run_id: UUID, actor: str):
    with factory() as db:
        run = require_run(db, run_id, actor)
        loops = [loop for loop in creation_loops(db, run_id) if _is_plan(loop)]
        # Completed plans and ambiguous pending actions keep their original operation.
        selected = next((loop for loop in loops if loop.status == "COMPLETED"), None)
        selected = selected or (loops[-1] if loops else None)
        attempt = creation_attempt(run)
        if selected:
            saved_attempt = (
                int(operation(selected).rsplit(":", 1)[-1])
                if ":attempt:" in operation(selected)
                else 0
            )
            if selected.status != "STOPPED" or saved_attempt == attempt:
                attempt = saved_attempt
        limits = AgentRunLimits.model_validate(run.input_json["screenwriter"]["agent_limits"])
    return planning.plan_screenwriter(
        factory, settings, run_id, actor, limits=limits, attempt=attempt
    )


def _fragment_key(scene_no: int, attempt: int) -> str:
    return f"{FRAGMENT_VERSION}:scene:{scene_no}:attempt:{attempt}"


def _validate_fragment(
    db: Session,
    run_id: UUID,
    plan_loop_id: UUID,
    plan: ScreenwriterPlan,
    scene_no: int,
    fragment: ScriptFragment,
) -> None:
    assert isinstance(plan.plan, EpisodePlan)
    scene = plan.plan.scenes[scene_no - 1]
    if (
        fragment.plan_loop_id != plan_loop_id
        or fragment.scene.scene_id != scene.scene_id
        or fragment.scene.scene_no != scene_no
        or fragment.estimated_duration_ms != scene.estimated_duration_ms
    ):
        raise DomainError(
            "SCREENWRITER_FRAGMENT_SCOPE", "片段不能更改原计划、场景身份、顺序或估时", 422
        )
    if not {item.model_dump_json() for item in fragment.evidence} <= {
        item.model_dump_json() for item in plan.evidence
    }:
        raise DomainError(
            "SCREENWRITER_FRAGMENT_EVIDENCE", "片段引用不属于已保存计划的精确证据", 422
        )
    episode_id = db.scalar(select(WorkflowRun.episode_id).where(WorkflowRun.id == run_id))
    episode = db.get(Episode, episode_id)
    assert episode is not None
    content = ScriptContent(
        title="片段",
        summary="片段",
        characters=fragment.characters,
        character_assets=fragment.character_assets,
        estimated_duration_ms=fragment.estimated_duration_ms,
        scenes=[fragment.scene.model_copy(update={"scene_no": 1})],
    )
    validate_script_assets(db, episode.project_id, content)


def create_fragment(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan_loop_id: UUID,
    plan: ScreenwriterPlan,
    scene_no: int,
    previous: list[ScriptFragment],
) -> AgentLoopResult[ScriptFragment]:
    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        prefix = f"{FRAGMENT_VERSION}:scene:{scene_no}:attempt:"
        loops = [loop for loop in creation_loops(db, run_id) if operation(loop).startswith(prefix)]
        selected = next((loop for loop in loops if loop.status == "COMPLETED"), None)
        selected = selected or (loops[-1] if loops else None)
        key = _fragment_key(scene_no, creation_attempt(run))
        if selected and (selected.status != "STOPPED" or operation(selected) == key):
            request = AgentModelRequest.model_validate(selected.seed_json)
        else:
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="screenwriter",
                stage="script",
                operation_key=key,
                agent_version=SCREENWRITER_VERSION,
                prompt_version=FRAGMENT_VERSION,
                result_schema_version=FRAGMENT_VERSION,
                system_instruction=FRAGMENT_PROMPT,
                messages=[
                    AgentUserMessage(
                        data=FragmentSeed(
                            plan_loop_id=plan_loop_id,
                            plan=plan,
                            scene_no=scene_no,
                            previous=previous[-2:],
                            prior_scene_ids=[UUID(item.scene.scene_id) for item in previous],
                        ).model_dump(mode="json")
                    )
                ],
                max_tokens=6000,
            )
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        planning._registry(settings),
        ScriptFragment,
        validate_result=lambda db, fragment: _validate_fragment(
            db, run_id, plan_loop_id, plan, scene_no, fragment
        ),
    )


def assemble_script(
    plan_loop_id: UUID, plan: ScreenwriterPlan, fragments: list[ScriptFragment]
) -> ScriptContent:
    assert isinstance(plan.plan, EpisodePlan)
    if len(fragments) != len(plan.plan.scenes):
        raise DomainError("SCREENWRITER_INCOMPLETE", "未齐备全部场景，不能保存完整候选", 422)
    characters: list[str] = []
    bindings: dict[str, CharacterAsset] = {}
    for index, fragment in enumerate(fragments, 1):
        expected = plan.plan.scenes[index - 1]
        if (
            fragment.plan_loop_id != plan_loop_id
            or fragment.scene.scene_id != expected.scene_id
            or fragment.scene.scene_no != index
            or fragment.estimated_duration_ms != expected.estimated_duration_ms
        ):
            raise DomainError("SCREENWRITER_FRAGMENT_SCOPE", "组装须保留计划顺序和精确片段", 422)
        for character in fragment.characters:
            if character not in characters:
                characters.append(character)
        for binding in fragment.character_assets:
            old = bindings.get(binding.character)
            if old is not None and old != binding:
                raise DomainError(
                    "SCREENWRITER_CHARACTER_CONFLICT", "各场景角色资产绑定不一致", 422
                )
            bindings[binding.character] = binding
    try:
        return ScriptContent.model_validate(
            {
                "schema_version": "2.0",
                "title": plan.plan.scenes[0].title[:160],
                "summary": plan.objective,
                "characters": characters,
                "character_assets": [item.model_dump(mode="json") for item in bindings.values()],
                "scenes": [item.scene.model_dump(mode="json") for item in fragments],
                "estimated_duration_ms": sum(item.estimated_duration_ms for item in fragments),
            }
        )
    except ValidationError:
        raise DomainError(
            "SCREENWRITER_ASSEMBLY_INVALID", "完整剧本含重复身份或非法引用", 422
        ) from None
