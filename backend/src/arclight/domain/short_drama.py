"""The project owner's single authorization to produce an exact script."""

from decimal import Decimal
from uuid import UUID

from pydantic import Field

from arclight.domain.agent_limits import AgentRunLimits
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.workflow_contracts import Checksum


class NeededVoice(AgentToolContract):
    speaker: str
    character_asset_id: UUID | None


class BlockedVoice(AgentToolContract):
    speaker: str
    reason: str


class ShortDramaPreflight(AgentToolContract):
    needed_voices: list[NeededVoice]
    project_budget_limit: Decimal
    blocked_voices: list[BlockedVoice]


class ShortDramaCreate(AgentToolContract):
    script_version_id: UUID
    maximum_cost: Decimal = Field(gt=0, lt=10**12, decimal_places=6)
    impact_digest: Checksum | None = None
    font_file_id: UUID | None = None
    generated_media_rights_confirmed: bool = False

    def agent_limits(self) -> AgentRunLimits:
        return AgentRunLimits(
            maximum_cost=self.maximum_cost,
            max_duration_seconds=3600,
            max_model_calls=96,
            max_steps=128,
            max_calls=400,
            max_agent_calls=400,
            max_tool_calls=300,
            max_delegations=128,
        )
