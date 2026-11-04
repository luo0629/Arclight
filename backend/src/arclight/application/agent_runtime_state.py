"""Create and scope the private Agent state attached to one workflow run."""

from uuid import UUID

from arclight.domain.agent_state import AgentRunScope, AgentRunState, initial_agent_state
from arclight.persistence.models import WorkflowRun


def scope_for_run(run: WorkflowRun, project_id: UUID) -> AgentRunScope:
    from arclight.application.production_versions import input_summary
    from arclight.domain.versions import digest

    return AgentRunScope(
        run_id=run.id,
        project_id=project_id,
        workflow_type=run.workflow_type,
        workflow_version=run.workflow_version,
        input_sha256=digest(input_summary(run.input_json)),
    )


def initialize_run_state(run: WorkflowRun, project_id: UUID) -> AgentRunState:
    state = initial_agent_state(scope_for_run(run, project_id))
    run.agent_state_json = state.model_dump(mode="json")
    return state
