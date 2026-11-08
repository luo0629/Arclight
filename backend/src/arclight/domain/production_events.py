"""Trusted stage notifications contain identities, never caller-supplied business facts."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.production_state import EpisodeProductionState, ProductionContract


class StageEvent(ProductionContract):
    schema_version: Literal["production-event/1"] = "production-event/1"
    event_id: UUID
    project_id: UUID
    episode_id: UUID
    source_kind: Literal["TEXT", "MEDIA", "REVIEW"]
    source_id: UUID
    source_revision: int = Field(strict=True, ge=0)
    actor_id: str = Field(min_length=1, max_length=100)
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    script_version_id: UUID | None
    storyboard_version_id: UUID | None
    input_version_ids: list[UUID] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def unique_inputs(self):
        if len(set(self.input_version_ids)) != len(self.input_version_ids):
            raise ValueError("Duplicate stage input version")
        return self


Disposition = Literal["APPLIED", "STALE", "BLOCKED"]
StageAction = Literal[
    "OBSERVED",
    "CONTINUE_TEXT",
    "WAIT_REVIEW",
    "WAIT_MEDIA",
    "WAIT_RECOVERY",
    "PARTIAL_FAILURE",
    "READY_FOR_REVIEW",
    "REVISION_REQUIRED",
    "HUMAN_REVIEW",
]


class StageReceipt(ProductionContract):
    event_id: UUID
    evaluation_key: UUID
    evaluated_by: str = Field(min_length=1, max_length=100)
    disposition: Disposition
    reason: str = Field(min_length=1, max_length=80)
    action: StageAction
    state: EpisodeProductionState | None
    budget: dict[str, str] = Field(default_factory=dict, max_length=4)
