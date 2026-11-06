"""Bounded read-only art investigation over the common evidence and image contracts."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract, RetrieveEvidenceResult

RESEARCH_TOOL = "investigate_art"


class ArtCatalogQuery(AgentToolContract):
    path: Literal["catalog"]
    asset_type: Literal["CHARACTER", "SCENE", "PROP", "STYLE"]
    name: str = Field(min_length=1, max_length=100)
    limit: int = Field(default=4, strict=True, ge=1, le=8)
    offset: int = Field(default=0, strict=True, ge=0, le=10000)


class ArtDirectoryQuery(AgentToolContract):
    path: Literal["directories"]
    asset_ids: list[UUID] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_assets(self):
        if len(set(self.asset_ids)) != len(self.asset_ids):
            raise ValueError("Directory selections must be unique")
        return self


class ArtVersionQuery(AgentToolContract):
    version_ids: list[UUID] = Field(min_length=1, max_length=8)

    @model_validator(mode="after")
    def unique_versions(self):
        if len(set(self.version_ids)) != len(self.version_ids):
            raise ValueError("Reference selections must be unique")
        return self


class ArtReferenceQuery(ArtVersionQuery):
    path: Literal["references"]
    include_images: bool = Field(default=True, strict=True)


class ArtAuthorizationQuery(ArtVersionQuery):
    path: Literal["authorization"]


class ArtVisualQuery(AgentToolContract):
    path: Literal["visual"]


class ArtCapabilityQuery(AgentToolContract):
    path: Literal["capabilities"]
    provider: str = Field(min_length=1, max_length=100)
    model: str = Field(min_length=1, max_length=200)


class ArtModelQuery(AgentToolContract):
    path: Literal["model"]


class InvestigateArtParameters(AgentToolContract):
    query: Annotated[
        ArtCatalogQuery
        | ArtDirectoryQuery
        | ArtReferenceQuery
        | ArtAuthorizationQuery
        | ArtVisualQuery
        | ArtCapabilityQuery
        | ArtModelQuery,
        Field(discriminator="path"),
    ]
    refresh: bool = Field(default=False, strict=True)


class ArtResearchMaterial(AgentToolContract):
    manifest_id: UUID
    source_kind: Literal["DIRECTORY", "ASSET", "AUTHORIZATION", "VISUAL", "CAPABILITY", "MODEL"]
    source_id: UUID
    bound_manifest_id: UUID
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    content: dict


class ArtResearchMissing(AgentToolContract):
    selector: dict
    reason: Literal[
        "NOT_FOUND",
        "INPUT_NOT_USABLE",
        "NO_IMAGE",
        "NO_SELECTED_VERSION",
        "RIGHTS_UNDECLARED",
        "OUT_OF_SCOPE",
        "OBJECT_UNAVAILABLE",
        "PROVIDER_NOT_INSTALLED",
        "MODEL_CAPABILITY_UNDECLARED",
        "MODEL_CAPABILITY_INSUFFICIENT",
    ]


class InvestigateArtResult(RetrieveEvidenceResult):
    schema_version: Literal["art-research/1"] = "art-research/1"
    materials: list[ArtResearchMaterial] = Field(max_length=16)
    missing: list[ArtResearchMissing] = Field(max_length=32)
    has_more: bool
    reused_binding: bool
