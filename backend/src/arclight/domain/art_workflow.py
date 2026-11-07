"""Public start contract for the existing durable art proposal operation."""

from uuid import UUID

from pydantic import Field

from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_concepts import CONCEPT_WORKFLOW_VERSION, ArtConceptPlan
from arclight.domain.art_direction import ArtDirectionTask

ART_WORKFLOW_VERSION = "art-proposal/1.0"
ART_WORKFLOW_VERSIONS = frozenset({ART_WORKFLOW_VERSION, CONCEPT_WORKFLOW_VERSION})


class ArtRunCreate(AgentToolContract):
    script_version_id: UUID
    task: ArtDirectionTask
    concept_plan: ArtConceptPlan | None = None
    agent_limits: AgentRunLimits = Field(default_factory=AgentRunLimits)
