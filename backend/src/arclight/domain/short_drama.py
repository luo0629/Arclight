"""The project owner's single authorization to produce an exact script."""

from typing import Literal
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


class ShortDramaTask(AgentToolContract):
    id: UUID
    family: Literal["WORKFLOW", "GENERATION"]


class ShortDramaIssue(AgentToolContract):
    phase: str
    code: str
    service_kind: str | None = None
    supplier: str | None = None
    model: str | None = None
    task: ShortDramaTask | None = Field(default=None, exclude_if=lambda value: value is None)


class ShortDramaPreflight(AgentToolContract):
    needed_voices: list[NeededVoice]
    blocked_voices: list[BlockedVoice]
    issues: list[ShortDramaIssue]


class ShortDramaRetry(AgentToolContract):
    expected_revision: int = Field(ge=0, strict=True)


class ShortDramaCreate(AgentToolContract):
    script_version_id: UUID
    impact_digest: Checksum | None = None
    font_file_id: UUID | None = None
    generated_media_rights_confirmed: bool = False
