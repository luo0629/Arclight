"""Short transactions around director working loops and the original review graph."""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from arclight.application.agent_runtime_state import scope_for_run
from arclight.application.commands import emit
from arclight.application.director_creation import assemble_storyboard, create_fragment, create_plan
from arclight.application.gateway_text import execution_actor
from arclight.application.production_versions import text_settings
from arclight.core.config import Settings
from arclight.core.errors import DomainError
from arclight.domain.agent_state import restore_agent_state, transition_agent_step
from arclight.domain.director_creation import (
    DIRECTOR_VERSION,
    ITERATIVE_DIRECTOR_VERSION,
    ITERATIVE_DIRECTOR_VERSIONS,
    DirectorFragment,
)
from arclight.domain.director_revision import DirectorWorkingFragment
from arclight.persistence.models import Episode, Storyboard, WorkflowRun

logger = logging.getLogger(__name__)


class DirectorWaiting(RuntimeError):
    """Native human pause keeps the outer director node ready for reentry."""


def generate_storyboard(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> dict:
    from arclight.application.screenwriter_execution import _active, _waiting

    with factory.begin() as db:
        run = _active(db, run_id, revision)
        actor = execution_actor(db, run)
    outcome = create_plan(factory, settings, run_id, actor)
    if _waiting(factory, run_id, revision, outcome, "director"):
        raise DirectorWaiting
    assert outcome.result is not None and outcome.loop_id is not None
    with factory() as db:
        current = db.get(WorkflowRun, run_id)
        assert current is not None
        iterative = current.workflow_version in ITERATIVE_DIRECTOR_VERSIONS
    if iterative:
        from arclight.application.director_revision import revise_storyboard
        from arclight.application.director_working import assemble_working, create_working_fragment

        working: list[DirectorWorkingFragment] = []
        for scene_no in range(1, len(outcome.result.scenes) + 1):
            part = create_working_fragment(
                factory, settings, run_id, outcome.loop_id, outcome.result, scene_no, working
            )
            if _waiting(factory, run_id, revision, part, "director"):
                raise DirectorWaiting
            assert part.result is not None
            working.append(part.result)
        checked, content = revise_storyboard(
            factory,
            settings,
            run_id,
            outcome.result,
            assemble_working(outcome.loop_id, outcome.result, working),
        )
        if _waiting(factory, run_id, revision, checked, "director"):
            raise DirectorWaiting
        assert content is not None
        return content.model_dump(mode="json")
    fragments: list[DirectorFragment] = []
    for scene_no in range(1, len(outcome.result.scenes) + 1):
        fragment = create_fragment(
            factory, settings, run_id, outcome.loop_id, outcome.result, scene_no, fragments
        )
        if _waiting(factory, run_id, revision, fragment, "director"):
            raise DirectorWaiting
        assert fragment.result is not None
        fragments.append(fragment.result)
    return assemble_storyboard(outcome.loop_id, outcome.result, fragments).model_dump(mode="json")


def execute_director(
    factory: sessionmaker[Session], settings: Settings, run_id: UUID, revision: int
) -> bool:
    from arclight.application.screenwriter_execution import (
        ScreenwriterExecutionDeferred,
        _fail,
        _review_graph,
        _waiting,
    )

    with factory() as db:
        run = db.get(WorkflowRun, run_id)
        if run is None or run.workflow_version not in {
            DIRECTOR_VERSION,
            ITERATIVE_DIRECTOR_VERSION,
        }:
            return False
    try:
        with factory.begin() as db:
            from arclight.application.gateway_text import validate_publication
            from arclight.application.model_gateway import lock_gate

            lock_gate(db)
            run = db.get(WorkflowRun, run_id, populate_existing=True, with_for_update=True)
            assert run is not None
            if run.revision != revision or run.status not in {"QUEUED", "RUNNING"}:
                return True
            validate_publication(db, run_id)
            episode = db.get(Episode, run.episode_id)
            assert episode is not None
            settings = text_settings(db, episode.project_id, run, settings)
            scope = scope_for_run(run, episode.project_id)
            state = restore_agent_state(run.agent_state_json, None, scope)
            if (
                next(step for step in state.steps if step.step_id == "storyboard").status
                == "PENDING"
            ):
                run.agent_state_json = transition_agent_step(
                    state,
                    scope,
                    expected_revision=state.revision,
                    step_id="storyboard",
                    target="RUNNING",
                ).model_dump(mode="json")
            if run.status == "QUEUED":
                emit(db, run, "workflow.started", {"revision": revision})
            run.status = "RUNNING"
            actor = execution_actor(db, run)
            candidate = db.scalar(select(Storyboard.id).where(Storyboard.run_id == run_id))
        # A completed plan supplies the original fenced review-graph lease. Its own
        # persistent Runtime protects planning I/O before that lease exists.
        if candidate is None:
            outcome = create_plan(factory, settings, run_id, actor)
            if _waiting(factory, run_id, revision, outcome, "director"):
                return True
        _review_graph(factory, settings, run_id, revision)
    except DomainError as exc:
        if exc.code in {"AGENT_LOOP_BUSY", "AGENT_EXECUTION_LOST"}:
            raise ScreenwriterExecutionDeferred(exc.code) from None
        if exc.code == "SCREENWRITER_DELIVERY_STALE":
            return True
        _fail(factory, run_id, revision, exc.code)
    except Exception as exc:
        logger.error("director_failed run_id=%s error_type=%s", run_id, type(exc).__name__)
        _fail(factory, run_id, revision, "WORKFLOW_EXECUTION_FAILED")
    return True
