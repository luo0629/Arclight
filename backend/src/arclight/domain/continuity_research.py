"""Bounded evidence requests; no review decision or selection authority."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract, RetrieveEvidenceResult
from arclight.domain.continuity_reviews import ReviewTarget

RESEARCH_TOOL = "investigate_continuity"


class InvestigateContinuityParameters(AgentToolContract):
    target: ReviewTarget
    reference_version_ids: list[UUID] = Field(default_factory=list, max_length=8)
    include_images: bool = Field(default=True, strict=True)
    refresh: bool = Field(default=False, strict=True)

    @model_validator(mode="after")
    def unique_references(self):
        if len(set(self.reference_version_ids)) != len(self.reference_version_ids):
            raise ValueError("Exact reference selections must be unique")
        return self


class ContinuityMaterial(AgentToolContract):
    manifest_id: UUID
    source_kind: Literal["CANDIDATE", "STORYBOARD", "SCRIPT", "SHOT", "ASSET", "VISUAL"]
    source_id: UUID
    bound_manifest_id: UUID
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: dict


class ContinuityMissing(AgentToolContract):
    selector: dict
    reason: Literal[
        "NOT_FOUND",
        "INPUT_NOT_USABLE",
        "OUT_OF_SCOPE",
        "NO_IMAGE",
        "NOT_REQUESTED",
        "NO_EXACT_REFERENCE",
        "NO_ADJACENT_SHOT",
        "NO_VISUAL_BIBLE",
    ]


class InvestigateContinuityResult(RetrieveEvidenceResult):
    schema_version: Literal["continuity-research/1"] = "continuity-research/1"
    target: ReviewTarget
    reference_version_ids: list[UUID] = Field(max_length=64)
    materials: list[ContinuityMaterial] = Field(max_length=18)
    missing: list[ContinuityMissing] = Field(max_length=192)
    reused_binding: bool
