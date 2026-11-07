"""Public start contract for the existing durable art proposal operation."""

from uuid import UUID

from pydantic import Field

from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_direction import ArtDirectionTask

ART_WORKFLOW_VERSION = "art-proposal/1.0"


class ArtRunCreate(AgentToolContract):
    script_version_id: UUID
    task: ArtDirectionTask
    agent_limits: AgentRunLimits = Field(default_factory=AgentRunLimits)
