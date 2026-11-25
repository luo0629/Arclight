"""Professional creation uses the existing journal, candidates and review graph."""

from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import CharacterAsset, ScriptContent, ScriptContentV3
from arclight.application import screenwriter_planning as planning
from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.application.agent_tools import (
    build_agent_tool_registry,
)
from arclight.application.agent_workflows import (
    creation_loops as creation_loops,
)
from arclight.application.agent_workflows import (
    waiting_for_answer as waiting_for_answer,
)
from arclight.application.commands import require_run
from arclight.application.content_references import validate_script_assets
from arclight.application.shared_agent_reads import grant_professional_agent_tools
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.director_creation import PRODUCTION_VERSIONS
from arclight.domain.screenwriter_creation import (
    FRAGMENT_PROMPT,
    FRAGMENT_VERSION,
    SCREENWRITER_OUTLINE_VERSION,
    SCREENWRITER_VERSION,
    SERIES_FRAGMENT_PROMPT,
    SERIES_FRAGMENT_VERSION,
    FragmentSeed,
    ScriptFragment,
    SeriesFragmentSeed,
    SeriesScriptFragment,
)
from arclight.domain.screenwriter_planning import (
    PLAN_VERSION,
    SERIES_PLAN_VERSION,
    EpisodePlan,
    ScreenwriterPlan,
    SeriesScreenwriterPlan,
)
from arclight.domain.screenwriter_research import SERIES_RESEARCH_TOOL
from arclight.domain.screenwriter_revision import (
    PATCH_TOOL,
    REVISION_WORKFLOWS,
    SERIES_PATCH_TOOL,
)
from arclight.persistence.agent_loops import AgentLoop
from arclight.persistence.models import Episode, WorkflowRun

PROFESSIONAL_VERSIONS = (
    {SCREENWRITER_VERSION, SCREENWRITER_OUTLINE_VERSION} | REVISION_WORKFLOWS | PRODUCTION_VERSIONS
)


def authorize_screenwriter(db: Session, run: WorkflowRun, actor: str, settings: Settings) -> None:
    # Choosing professional creation explicitly authorizes this read-only investigation.
    series = "narrative_context" in run.input_json
    revision = run.input_json["screenwriter"].get("revision_rounds") is not None
    grant_professional_agent_tools(
        db,
        AgentToolGrantRequest(
            run_id=run.id,
            agent_name="screenwriter",
            allowed_tools=[SERIES_RESEARCH_TOOL if series else "investigate_story"]
            + ([SERIES_PATCH_TOOL if series else PATCH_TOOL] if revision else []),
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


def operation(loop: AgentLoop) -> str:
    return str(loop.seed_json.get("operation_key", ""))


def _is_plan(loop: AgentLoop) -> bool:
    return any(
        operation(loop) == version or operation(loop).startswith(version + ":attempt:")
        for version in (PLAN_VERSION, SERIES_PLAN_VERSION)
    )


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
    return planning.plan_screenwriter(factory, settings, run_id, actor, attempt=attempt)


def _fragment_key(scene_no: int, attempt: int, *, series: bool = False) -> str:
    version = SERIES_FRAGMENT_VERSION if series else FRAGMENT_VERSION
    return f"{version}:scene:{scene_no}:attempt:{attempt}"


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
    if isinstance(plan, SeriesScreenwriterPlan):
        if (
            not isinstance(fragment, SeriesScriptFragment)
            or fragment.narrative_context != plan.narrative_context
        ):
            raise DomainError("SCREENWRITER_FRAGMENT_SCOPE", "场景不能更换冻结叙事来源", 422)
        expected_scene = plan.plan.scenes[scene_no - 1]
        if fragment.scene.dramatic_beat.story_time != expected_scene.story_time or {
            item.character_id for item in fragment.scene.dramatic_beat.character_objectives
        } != set(expected_scene.character_ids):
            raise DomainError(
                "SCREENWRITER_FRAGMENT_SCOPE", "场景须保留计划故事时点和人物视角", 422
            )
    value = {
        "title": "片段",
        "summary": "片段",
        "characters": fragment.characters,
        "character_assets": [item.model_dump(mode="json") for item in fragment.character_assets],
        "estimated_duration_ms": fragment.estimated_duration_ms,
        "scenes": [fragment.scene.model_copy(update={"scene_no": 1}).model_dump(mode="json")],
    }
    if isinstance(fragment, SeriesScriptFragment):
        value["narrative_context"] = fragment.narrative_context.model_copy(
            update={
                "target_scene_no": 1,
                "target_story_time": fragment.scene.dramatic_beat.story_time,
            }
        ).model_dump(mode="json")
        content: ScriptContent = ScriptContentV3.model_validate(value)
    else:
        content = ScriptContent.model_validate(value)
    validate_script_assets(db, episode.project_id, content)


def scene_narrative_projections(
    db: Session,
    settings: Settings,
    run: WorkflowRun,
    story_time: int,
    scene_no: int,
    character_ids: list[UUID],
) -> list[dict]:
    """Freeze a scene target derived only from the validated run plan/ledger baseline."""
    from arclight.application.gateway_text import execution_actor
    from arclight.application.writing_context import (
        load_narrative_projection,
        validate_narrative_context,
    )
    from arclight.domain.script_narrative import NarrativeContext

    actor = execution_actor(db, run)
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    root = NarrativeContext.model_validate(run.input_json["narrative_context"])
    validate_narrative_context(db, episode, actor, root)
    target = root.model_copy(update={"target_story_time": story_time, "target_scene_no": scene_no})
    return [
        load_narrative_projection(
            db,
            episode.project_id,
            actor,
            target,
            character_id=identity,
        )
        for identity in [None, *character_ids]
    ]


def create_fragment(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan_loop_id: UUID,
    plan: ScreenwriterPlan,
    scene_no: int,
    previous: list[ScriptFragment],
) -> AgentLoopResult[ScriptFragment]:
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        series = isinstance(plan, SeriesScreenwriterPlan)
        version = SERIES_FRAGMENT_VERSION if series else FRAGMENT_VERSION
        prefix = f"{version}:scene:{scene_no}:attempt:"
        loops = [loop for loop in creation_loops(db, run_id) if operation(loop).startswith(prefix)]
        selected = next((loop for loop in loops if loop.status == "COMPLETED"), None)
        selected = selected or (loops[-1] if loops else None)
        key = _fragment_key(scene_no, creation_attempt(run), series=series)
        if selected and (selected.status != "STOPPED" or operation(selected) == key):
            request = AgentModelRequest.model_validate(selected.seed_json)
        else:
            fragment_seed = {
                "plan_loop_id": str(plan_loop_id),
                "plan": plan.model_dump(mode="json"),
                "scene_no": scene_no,
                "previous": [item.model_dump(mode="json") for item in previous[-2:]],
                "prior_scene_ids": [item.scene.scene_id for item in previous],
            }
            if isinstance(plan, SeriesScreenwriterPlan):
                planned = plan.plan.scenes[scene_no - 1]
                fragment_seed["narrative_projections"] = scene_narrative_projections(
                    db,
                    settings,
                    run,
                    planned.story_time,
                    scene_no,
                    planned.character_ids,
                )
            seed_model = SeriesFragmentSeed if series else FragmentSeed
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="screenwriter",
                stage="script",
                operation_key=key,
                agent_version=run.workflow_version if series else SCREENWRITER_VERSION,
                prompt_version=version,
                result_schema_version=version,
                system_instruction=SERIES_FRAGMENT_PROMPT if series else FRAGMENT_PROMPT,
                messages=[
                    AgentUserMessage(
                        data=seed_model.model_validate(fragment_seed).model_dump(mode="json")
                    )
                ],
            )
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        planning._registry(settings, series=series),
        SeriesScriptFragment if series else ScriptFragment,
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
        content_model = (
            ScriptContentV3 if isinstance(plan, SeriesScreenwriterPlan) else ScriptContent
        )
        return content_model.model_validate(
            {
                "schema_version": "3.0" if isinstance(plan, SeriesScreenwriterPlan) else "2.0",
                **(
                    {"narrative_context": plan.narrative_context.model_dump(mode="json")}
                    if isinstance(plan, SeriesScreenwriterPlan)
                    else {}
                ),
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
