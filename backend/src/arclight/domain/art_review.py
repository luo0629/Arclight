"""Explicit manual commands; stored art content still uses the ART-01 contract."""

from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.art_proposals import ArtProposal, Checksum
from arclight.domain.art_protection import ArtConfirmation
from arclight.domain.asset_metadata import AssetFields


class ArtSave(AssetFields):
    creation_key: UUID
    proposal: ArtProposal
    expected_revision: int = Field(default=0, strict=True, ge=0)


class ArtSubmit(AssetFields):
    version_id: UUID
    expected_revision: int = Field(strict=True, ge=0)
    option_id: UUID
    comparison_sha256: Checksum


class ArtArchive(AssetFields):
    expected_revision: int = Field(strict=True, ge=0)
    archived: bool = Field(strict=True)


class ArtDecision(AssetFields):
    version_id: UUID
    expected_revision: int = Field(strict=True, ge=0)
    request_sha256: Checksum
    decision: Literal["APPROVE", "REJECT"]
    comment: str = Field(default="", max_length=2000)
    confirmation: ArtConfirmation | None = None

    @model_validator(mode="after")
    def exact_confirmation(self):
        if (self.decision == "APPROVE") != (self.confirmation is not None):
            raise ValueError("批准须提供精确变更确认；拒绝不携带批准确认")
        return self
