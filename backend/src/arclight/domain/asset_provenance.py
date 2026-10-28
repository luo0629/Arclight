"""Bounded external generation declarations; never evidence of a provider call."""

import math
from typing import Annotated

from pydantic import (
    Field,
    StrictBool,
    StrictFloat,
    StrictInt,
    StrictStr,
    StringConstraints,
    model_validator,
)

from arclight.domain.asset_metadata import AssetFields
from arclight.domain.asset_profiles import ReferenceVersion

Parameter = StrictBool | StrictInt | StrictFloat | Annotated[StrictStr, Field(max_length=1000)]


class GenerationDeclaration(AssetFields):
    provider: str = Field(min_length=1, max_length=160)
    model: str = Field(min_length=1, max_length=160)
    prompt: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        min_length=1, max_length=12000
    )
    negative_prompt: Annotated[str, StringConstraints(strip_whitespace=False)] = Field(
        default="", max_length=4000
    )
    prompt_version: str | None = Field(default=None, min_length=1, max_length=160)
    seed: int | None = Field(default=None, strict=True, ge=0, le=9007199254740991)
    parameters: dict[str, Parameter] = Field(default_factory=dict, max_length=30)
    references: list[ReferenceVersion] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def bounded_parameters_and_references(self) -> "GenerationDeclaration":
        if any(not key.strip() or len(key) > 80 for key in self.parameters):
            raise ValueError("参数名称必须为1至80字符")
        if any(
            isinstance(value, float) and not math.isfinite(value)
            for value in self.parameters.values()
        ):
            raise ValueError("参数数值必须有限")
        if len({ref.asset_version_id for ref in self.references}) != len(self.references):
            raise ValueError("精确参考版本不能重复")
        return self
