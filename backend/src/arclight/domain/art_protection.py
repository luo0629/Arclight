"""Server-derived comparison and explicit acknowledgement, never asset write commands."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from arclight.domain.art_proposals import Checksum
from arclight.domain.asset_metadata import AssetFields


class ArtChangeLocation(AssetFields):
    design_id: UUID
    field_path: str = Field(
        min_length=1, max_length=160, pattern=r"^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*){0,3}$"
    )


class ArtFieldChange(BaseModel):
    # Preserve exact before/after whitespace, including Visual Bible prompt text.
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    design_id: UUID
    target_type: Literal["CHARACTER", "SCENE", "PROP", "STYLE", "VISUAL_BIBLE"]
    target_id: UUID | None
    field_path: str
    before: JsonValue
    proposed: JsonValue
    protection: Literal["NONE", "APPROVED", "LOCKED"]


class ArtOptionComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    option_id: UUID
    changes: list[ArtFieldChange] = Field(max_length=512)


class ArtComparison(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal["art-comparison/1"] = "art-comparison/1"
    project_id: UUID
    proposal_sha256: Checksum
    comparison_sha256: Checksum
    options: list[ArtOptionComparison] = Field(max_length=4)
    unresolved_question_ids: list[UUID] = Field(max_length=32)
    requires_human_review: Literal[True] = True


class ArtConfirmation(AssetFields):
    schema_version: Literal["art-confirmation/1"] = "art-confirmation/1"
    comparison_sha256: Checksum
    option_id: UUID
    confirmed_changes: list[ArtChangeLocation] = Field(max_length=512)

    @model_validator(mode="after")
    def unique_locations(self):
        locations = [(change.design_id, change.field_path) for change in self.confirmed_changes]
        if len(set(locations)) != len(locations):
            raise ValueError("Confirmed change locations must be unique")
        return self
