"""Persistent scene drafts for the iterative director; no formal candidates are written."""

from uuid import UUID

from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_loop_recovery import run_persistent_agent_loop
from arclight.application.director_creation import creation_attempt
from arclight.application.director_performance import director_contracts, offset_working_shot
from arclight.application.director_planning import _registry
from arclight.application.director_validation import revision_input
from arclight.application.screenwriter_creation import creation_loops, operation
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_model import AgentModelRequest, AgentUserMessage
from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.director_planning import DirectorPlan
from arclight.domain.director_revision import (
    DirectorWorkingFragment,
    DirectorWorkingStoryboard,
)
from arclight.persistence.models import WorkflowRun


def _validate(
    db: Session,
    settings: Settings,
    run_id: UUID,
    plan_id: UUID,
    plan: DirectorPlan,
    scene_no: int,
    fragment: DirectorWorkingFragment,
) -> None:
    run = db.get(WorkflowRun, run_id)
    assert run is not None
    revision_input(db, run, plan, settings)
    if (
        fragment.plan_loop_id != plan_id
        or fragment.scene_id != plan.scenes[scene_no - 1].scene_id
        or fragment.scene_no != scene_no
        or not {item.model_dump_json() for item in fragment.evidence}
        <= {item.model_dump_json() for item in plan.evidence}
    ):
        raise DomainError("DIRECTOR_FRAGMENT_SCOPE", "工作片段须保留原计划、场景及精确证据", 422)


def create_working_fragment(
    factory: sessionmaker[Session],
    settings: Settings,
    run_id: UUID,
    plan_id: UUID,
    plan: DirectorPlan,
    scene_no: int,
    previous: list[DirectorWorkingFragment],
) -> AgentLoopResult[DirectorWorkingFragment]:
    with factory.begin() as db:
        run = db.get(WorkflowRun, run_id)
        assert run is not None
        script = revision_input(db, run, plan, settings)
        contracts = director_contracts(run.workflow_version)
        prefix = f"{contracts.working_version}:scene:{scene_no}:attempt:"
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
                agent_version=run.workflow_version,
                prompt_version=contracts.working_version,
                result_schema_version=contracts.working_version,
                system_instruction=contracts.working_prompt,
                max_tokens=8000,
                messages=[
                    AgentUserMessage(
                        data=contracts.working_seed(
                            plan_loop_id=plan_id,
                            plan=plan,
                            scene=script.scenes[scene_no - 1],
                            characters=script.characters,
                            character_assets=script.character_assets,
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
        _registry(settings),
        contracts.fragment,
        validate_result=lambda db, result: _validate(
            db, settings, run_id, plan_id, plan, scene_no, result
        ),
    )


def assemble_working(
    plan_id: UUID, plan: DirectorPlan, fragments: list[DirectorWorkingFragment]
) -> DirectorWorkingStoryboard:
    if len(fragments) != len(plan.scenes):
        raise DomainError("DIRECTOR_INCOMPLETE", "全部场景齐备后才能校验工作稿", 422)
    from arclight.domain.director_performance import (
        PERFORMANCE_DIRECTOR_VERSION,
        PerformanceDirectorPlan,
    )

    contracts = director_contracts(
        PERFORMANCE_DIRECTOR_VERSION if isinstance(plan, PerformanceDirectorPlan) else ""
    )
    scenes = []
    offset = 0
    for number, (scene, fragment) in enumerate(zip(plan.scenes, fragments, strict=True), 1):
        if (
            fragment.plan_loop_id != plan_id
            or fragment.scene_id != scene.scene_id
            or fragment.scene_no != number
        ):
            raise DomainError("DIRECTOR_FRAGMENT_SCOPE", "组装须保留原计划及完整场景顺序", 422)
        scenes.append(
            contracts.scene(
                scene_id=fragment.scene_id,
                scene_no=fragment.scene_no,
                shots=[offset_working_shot(shot, offset) for shot in fragment.shots],
            )
        )
        offset += len(fragment.shots)
    return contracts.work(scenes=scenes)
