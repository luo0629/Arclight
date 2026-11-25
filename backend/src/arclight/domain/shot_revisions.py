"""Human shot revision intent; no client-supplied Provider or protected prompt blocks."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.generation_budget import CostQuote
from arclight.domain.production_state import ProductionContract

ShotRevisionOperation = Literal[
    "RECOMPOSE", "EXPRESSION", "REPLACE_SCENE", "REPAIR_HANDS", "CHANGE_OUTFIT"
]
OPERATION_LABELS: dict[str, str] = {
    "RECOMPOSE": "重新构图",
    "EXPRESSION": "表情调整",
    "REPLACE_SCENE": "场景替换",
    "REPAIR_HANDS": "手部修复",
    "CHANGE_OUTFIT": "换装",
}
SHOT_REVISION_MODE = "按修订要求整图重生成；原图仅供比较，不保证其他像素保持不变。"


class ShotRevisionIntent(ProductionContract):
    candidate_id: UUID
    storyboard_id: UUID
    shot_id: UUID
    operation: ShotRevisionOperation
    instruction: str = Field(min_length=1, max_length=2000)
    mode: Literal["PROMPT_REGENERATION"] = "PROMPT_REGENERATION"

    @model_validator(mode="after")
    def meaningful_instruction(self):
        if not self.instruction.strip():
            raise ValueError("修订要求不能为空白")
        return self


class ShotRevisionCommand(ShotRevisionIntent):
    confirmation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShotRevisionSource(ProductionContract):
    candidate_id: UUID
    asset_id: UUID
    version_id: UUID
    version_no: int = Field(strict=True, ge=1)
    storyboard_id: UUID
    shot_id: UUID
    episode_id: UUID
    job_id: UUID
    input_manifest_id: UUID
    provider: str
    model: str
    snapshot_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShotRevisionPreview(ProductionContract):
    intent: ShotRevisionIntent
    source: ShotRevisionSource
    mode_description: str = SHOT_REVISION_MODE
    quote: CostQuote | None
    confirmation_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    preserved_constraints: list[str]


class ShotRevisionOrigin(ProductionContract):
    intent: ShotRevisionIntent
    source: ShotRevisionSource
    created_by: str
    command_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class ShotRevisionReceipt(ProductionContract):
    job_id: UUID
    input_manifest_id: UUID
    source: ShotRevisionSource
    status: str
    mode: Literal["PROMPT_REGENERATION"] = "PROMPT_REGENERATION"
