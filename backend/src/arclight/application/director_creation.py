"""Persistent per-scene shots reuse director planning and the original Agent Runtime."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from arclight.agents.contracts import ScriptContent, ShotContent, StoryboardContent
from arclight.application import director_planning as planning
from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.application.agent_tools import build_agent_tool_registry, grant_agent_tools
from arclight.application.commands import require_run
from arclight.application.content_references import validate_storyboard_assets
from arclight.application.director_research import approved_input
from arclight.application.gateway_text import execution_actor
from arclight.application.screenwriter_creation import creation_loops, operation
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_tools import AgentToolGrantRequest
from arclight.domain.director_creation import (
    DIRECTOR_VERSION,
    FRAGMENT_PROMPT,
    FRAGMENT_VERSION,
    ITERATIVE_DIRECTOR_VERSIONS,
    DirectorFragment,
    DirectorFragmentSeed,
)
from arclight.domain.director_planning import PLAN_VERSION, RESEARCH_TOOL, DirectorPlan
from arclight.domain.director_revision import CHECK_TOOL
from arclight.domain.versions import digest
from arclight.persistence.models import Episode, WorkflowRun


def authorize_director(db: Session, run: WorkflowRun, actor: str, settings: Settings) -> None:
    grant_agent_tools(
        db,
        AgentToolGrantRequest(
            run_id=run.id,
            agent_name="director",
            allowed_tools=[RESEARCH_TOOL]
            + ([CHECK_TOOL] if run.workflow_version in ITERATIVE_DIRECTOR_VERSIONS else []),
        ),
        actor,
        build_agent_tool_registry(settings=settings),
    )


def creation_attempt(run: WorkflowRun) -> int:
    return int(
        (run.agent_state_json or {})
        .get("extensions", {})
        .get("director.creation", {})
        .get("attempt", 0)
    )


def create_plan(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, actor: str
) -> AgentLoopResult[DirectorPlan]:
    with factory() as db:
        run = require_run(db, run_id, actor)
        loops = [
            loop
            for loop in creation_loops(db, run.id, "director")
            if operation(loop) == PLAN_VERSION
            or operation(loop).startswith(PLAN_VERSION + ":attempt:")
        ]
        selected = next((loop for loop in loops if loop.status == "COMPLETED"), None)
        selected = selected or (loops[-1] if loops else None)
        attempt = creation_attempt(run)
        if selected:
            saved = (
                int(operation(selected).rsplit(":", 1)[-1])
                if ":attempt:" in operation(selected)
                else 0
            )
            if selected.status != "STOPPED" or saved == attempt:
                attempt = saved
        limits = AgentRunLimits.model_validate(run.input_json["director"]["agent_limits"])
    return planning.plan_director(factory, settings, run_id, actor, limits=limits, attempt=attempt)


def _validate_fragment(
    db: Session,
    settings: Settings,
    run_id: UUID,
    plan_loop_id: UUID,
    plan: DirectorPlan,
    scene_no: int,
    fragment: DirectorFragment,
) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    script, _ = approved_input(db, run, execution_actor(db, run), settings)
    source = ScriptContent.model_validate(script.content_json)
    scene = plan.scenes[scene_no - 1]
    if (
        fragment.plan_loop_id != plan_loop_id
        or fragment.scene_id != scene.scene_id
        or fragment.scene_no != scene_no
        or digest(script.content_json) != plan.script_sha256
        or script.id != plan.script_version_id
    ):
        raise DomainError("DIRECTOR_FRAGMENT_SCOPE", "镜头须保留原计划、剧本和场景", 422)
    if sum(shot.duration_ms for shot in fragment.shots) != scene.estimated_duration_ms:
        raise DomainError("DIRECTOR_FRAGMENT_DURATION", "场景镜头总时长须满足已保存计划", 422)
    if not {item.model_dump_json() for item in fragment.evidence} <= {
        item.model_dump_json() for item in plan.evidence
    }:
        raise DomainError("DIRECTOR_FRAGMENT_EVIDENCE", "镜头须引用已保存计划的精确证据", 422)
    try:
        content = StoryboardContent(shots=fragment.shots).validate_against(source)
    except ValueError:
        raise DomainError(
            "DIRECTOR_FRAGMENT_REFERENCE", "镜头须保留有效场景、台词和精确绑定", 422
        ) from None
    episode = db.get(Episode, run.episode_id)
    assert episode is not None
    validate_storyboard_assets(db, episode.project_id, content)


def create_fragment(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan_loop_id: UUID,
    plan: DirectorPlan,
    scene_no: int,
    previous: list[DirectorFragment],
) -> AgentLoopResult[DirectorFragment]:
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        script, _ = approved_input(db, run, execution_actor(db, run), settings)
        source = ScriptContent.model_validate(script.content_json)
        prefix = f"{FRAGMENT_VERSION}:scene:{scene_no}:attempt:"
        loops = [
            loop
            for loop in creation_loops(db, run.id, "director")
            if operation(loop).startswith(prefix)
        ]
        selected = next((loop for loop in loops if loop.status == "COMPLETED"), None)
        selected = selected or (loops[-1] if loops else None)
        key = prefix + str(creation_attempt(run))
        if selected and (selected.status != "STOPPED" or operation(selected) == key):
            request = AgentModelRequest.model_validate(selected.seed_json)
        else:
            request = AgentModelRequest(
                run_id=run.id,
                agent_name="director",
                stage="storyboard",
                operation_key=key,
                agent_version=DIRECTOR_VERSION,
                prompt_version=FRAGMENT_VERSION,
                result_schema_version=FRAGMENT_VERSION,
                system_instruction=FRAGMENT_PROMPT,
                max_tokens=8000,
                messages=[
                    AgentUserMessage(
                        data=DirectorFragmentSeed(
                            plan_loop_id=plan_loop_id,
                            plan=plan,
                            scene=source.scenes[scene_no - 1],
                            characters=source.characters,
                            character_assets=source.character_assets,
                            previous_shots=[
                                shot for fragment in previous for shot in fragment.shots
                            ][-2:],
                        ).model_dump(mode="json")
                    )
                ],
            )
    return run_persistent_agent_loop(
        factory,
        settings,
        request,
        planning._registry(settings),
        DirectorFragment,
        validate_result=lambda db, fragment: _validate_fragment(
            db, settings, run_id, plan_loop_id, plan, scene_no, fragment
        ),
    )


def assemble_storyboard(
    plan_loop_id: UUID, plan: DirectorPlan, fragments: list[DirectorFragment]
) -> StoryboardContent:
    if len(fragments) != len(plan.scenes):
        raise DomainError("DIRECTOR_INCOMPLETE", "全部场景齐备后才能保存分镜候选", 422)
    shots: list[ShotContent] = []
    for number, (scene, fragment) in enumerate(zip(plan.scenes, fragments, strict=True), 1):
        if (
            fragment.plan_loop_id != plan_loop_id
            or fragment.scene_id != scene.scene_id
            or fragment.scene_no != number
        ):
            raise DomainError("DIRECTOR_FRAGMENT_SCOPE", "组装须保留原场景与计划顺序", 422)
        for shot in fragment.shots:
            shots.append(shot.model_copy(update={"shot_no": len(shots) + 1}))
    return StoryboardContent(shots=shots)
