"""Bounded rewrite instructions and a patch-only provider contract."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.agents.contracts import Contract, Dialogue, Identifier


class ChapterTarget(Contract):
    scope: Literal["chapter"]
    scene_ids: list[Identifier] = Field(min_length=1, max_length=50)


class SceneTarget(Contract):
    scope: Literal["scene"]
    scene_id: Identifier


class ParagraphTarget(Contract):
    scope: Literal["paragraph"]
    scene_id: Identifier
    field: Literal["action", "dialogue"]
    line_id: Identifier | None = None
    start: int = Field(strict=True, ge=0)
    end: int = Field(strict=True, gt=0)

    @model_validator(mode="after")
    def check_range(self) -> "ParagraphTarget":
        if self.end <= self.start or (self.field == "dialogue") != (self.line_id is not None):
            raise ValueError("段落范围无效；台词必须指定精确 line_id")
        return self


RewriteTarget = Annotated[
    ChapterTarget | SceneTarget | ParagraphTarget, Field(discriminator="scope")
]


class RewriteInstructions(Contract):
    instruction: str = Field(default="", max_length=2000)
    tone: str = Field(default="", max_length=200)
    dialogue_max_chars: int | None = Field(default=None, strict=True, ge=1, le=2000)
    narration_max_percent: int | None = Field(default=None, strict=True, ge=0, le=100)

    @model_validator(mode="after")
    def has_instruction(self) -> "RewriteInstructions":
        if not (self.instruction.strip() or self.tone.strip()) and (
            self.dialogue_max_chars is None and self.narration_max_percent is None
        ):
            raise ValueError("至少填写一项改写指令")
        return self


class RewriteRequest(Contract):
    base_version_id: UUID
    target: RewriteTarget
    instructions: RewriteInstructions


class RewriteLine(Dialogue):
    line_id: Identifier


class RewriteScene(Contract):
    scene_id: Identifier
    title: str = Field(min_length=1, max_length=200)
    action: str = Field(min_length=1, max_length=5000)
    dialogue: list[RewriteLine] = Field(max_length=100)


class RewriteResult(Contract):
    estimated_duration_ms: int = Field(strict=True, gt=0)
    scenes: list[RewriteScene] = Field(max_length=50)
    text: str | None = Field(max_length=5000)
