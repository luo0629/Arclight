"""The project owner's single authorization to produce an exact script."""

from uuid import UUID

from pydantic import Field

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
    blocked_voices: list[BlockedVoice]


class ShortDramaRetry(AgentToolContract):
    expected_revision: int = Field(ge=0, strict=True)


class ShortDramaCreate(AgentToolContract):
    script_version_id: UUID
    impact_digest: Checksum | None = None
    font_file_id: UUID | None = None
    generated_media_rights_confirmed: bool = False
