"""Eight explicit, bounded visual direction fields and partial edit contract."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

VisualField = Literal[
    "art_style",
    "line_style",
    "render_style",
    "palette",
    "lighting",
    "composition",
    "prohibited",
    "negative_prompt",
]
FIELD_NAMES = (
    "art_style",
    "line_style",
    "render_style",
    "palette",
    "lighting",
    "composition",
    "prohibited",
    "negative_prompt",
)


class VisualFields(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    art_style: str = Field(default="", max_length=10000)
    line_style: str = Field(default="", max_length=2000)
    render_style: str = Field(default="", max_length=2000)
    palette: str = Field(default="", max_length=2000)
    lighting: str = Field(default="", max_length=2000)
    composition: str = Field(default="", max_length=2000)
    prohibited: str = Field(default="", max_length=4000)
    negative_prompt: str = Field(default="", max_length=4000)


class VisualPatch(VisualFields):
    @model_validator(mode="after")
    def nonempty_patch(self) -> "VisualPatch":
        if not self.model_fields_set:
            raise ValueError("请至少选择一个视觉规范字段")
        return self
