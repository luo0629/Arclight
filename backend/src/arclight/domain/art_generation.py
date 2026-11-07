"""A proposal delivery receipt over the existing art and durable runtime records."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.agent_runtime import AgentLoopResult
from arclight.domain.agent_tools import AgentToolContract
from arclight.domain.art_direction import (
    ArtDirectionResult,
    ArtDirectionSeed,
    ArtEvidence,
    ArtOptionAssessment,
)
from arclight.domain.art_proposals import Checksum
from arclight.domain.versions import digest

DELIVERY_KEY = "art-generation/1:delivery"
DELIVERY_KIND = "ART_DELIVERY"


class ArtDeliveryInput(AgentToolContract):
    schema_version: Literal["art-generation/1"] = "art-generation/1"
    seed: ArtDirectionSeed
    input_sha256: Checksum
    agent_request_sha256: Checksum
    startup_manifest_id: UUID
    direction_sha256: Checksum
    evidence: list[ArtEvidence] = Field(min_length=2, max_length=64)
    assessments: list[ArtOptionAssessment] = Field(min_length=1, max_length=4)
    comparison_sha256: Checksum


class ArtDelivery(AgentToolContract):
    schema_version: Literal["art-generation/1"] = "art-generation/1"
    project_id: UUID
    episode_id: UUID
    run_id: UUID
    loop_id: UUID
    proposal_id: UUID
    version_id: UUID
    review_request_id: UUID
    option_id: UUID
    direction_sha256: Checksum


class ArtGenerationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)
    direction: AgentLoopResult[ArtDirectionResult]
    delivery: ArtDelivery | None = None

    @model_validator(mode="after")
    def matching_delivery(self):
        if (self.delivery is not None) != (self.direction.status == "COMPLETED"):
            raise ValueError("Only a completed direction has a delivered proposal")
        if self.delivery is not None and (
            self.delivery.loop_id != self.direction.loop_id
            or self.delivery.run_id != self.direction.run_id
            or self.delivery.project_id != self.direction.project_id
            or self.direction.result is None
            or self.delivery.direction_sha256
            != digest(self.direction.result.model_dump(mode="json"))
        ):
            raise ValueError("The delivery must identify the exact completed direction")
        return self
