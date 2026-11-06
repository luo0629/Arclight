"""Bounded investigation requests; all scope and provenance are resolved by the server."""

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.agent_tools import AgentToolContract

SourceKind = Literal["STORY_SOURCE", "STORY_ENTRY", "OUTLINE", "ANALYSIS", "SCRIPT"]
EntryKind = Literal["WORLD", "CHARACTER", "LOCATION", "PROP", "SCENE"]


class StartupQuery(AgentToolContract):
    path: Literal["startup"]


class ResearchSlice(AgentToolContract):
    field: Literal["text", "episodes", "scenes", "characters"]
    start: int = Field(default=0, strict=True, ge=0, le=262144)
    length: int = Field(strict=True, ge=1, le=12000)


class ExactQuery(AgentToolContract):
    path: Literal["exact"]
    source_kind: SourceKind
    source_id: UUID
    revision: int | None = Field(default=None, strict=True, ge=0)
    selection: ResearchSlice | None = None

    @model_validator(mode="after")
    def mutable_revision_only(self):
        if self.revision is not None and self.source_kind != "STORY_ENTRY":
            raise ValueError("Only mutable story entries use revision selectors")
        if self.selection is not None:
            allowed = {
                "STORY_SOURCE": {"text"},
                "OUTLINE": {"episodes"},
                "SCRIPT": {"scenes", "characters"},
                "ANALYSIS": {"characters"},
                "STORY_ENTRY": set(),
            }
            if self.selection.field not in allowed[self.source_kind]:
                raise ValueError("The selected section does not belong to this source kind")
            if self.selection.field != "text" and self.selection.length > 8:
                raise ValueError("Structured investigation slices contain at most eight items")
        return self


class EntrySearchQuery(AgentToolContract):
    path: Literal["entries"]
    name: str = Field(min_length=1, max_length=100)
    entry_kind: EntryKind | None = None
    limit: int = Field(default=4, strict=True, ge=1, le=8)


class CatalogQuery(AgentToolContract):
    path: Literal["catalog"]
    source_kind: Literal["STORY_SOURCE", "OUTLINE", "ANALYSIS"]
    limit: int = Field(default=1, strict=True, ge=1, le=8)


class NeighborQuery(AgentToolContract):
    path: Literal["neighbors"]
    offsets: list[Literal[-2, -1, 1, 2]] = Field(min_length=1, max_length=4)

    @model_validator(mode="after")
    def unique_offsets(self):
        if len(set(self.offsets)) != len(self.offsets):
            raise ValueError("Neighbor offsets must be unique")
        return self


ResearchQuery = Annotated[
    StartupQuery | ExactQuery | EntrySearchQuery | CatalogQuery | NeighborQuery,
    Field(discriminator="path"),
]


class InvestigateStoryParameters(AgentToolContract):
    """Investigate frozen startup, exact sources, literal entry names, catalogs or neighbors.

    Queries keep their bindings until refresh=true. Exact text/structured section
    slices explicitly report their range; all citations identify the complete source.
    """

    query: ResearchQuery
    refresh: bool = Field(
        default=False,
        strict=True,
        description="Bind newer results in a new evidence round; old evidence stays fixed.",
    )


class ResearchMaterial(AgentToolContract):
    source_kind: Literal["STARTUP", "STORY_SOURCE", "STORY_ENTRY", "OUTLINE", "ANALYSIS", "SCRIPT"]
    source_id: UUID
    bound_manifest_id: UUID
    source: dict
    content: dict
    content_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class ResearchMissing(AgentToolContract):
    selector: dict
    reason: Literal[
        "NOT_FOUND",
        "ARCHIVED",
        "NOT_CONFIRMED",
        "RIGHTS_UNCONFIRMED",
        "SOURCE_UNAVAILABLE",
        "INPUT_NOT_USABLE",
        "REVISION_UNAVAILABLE",
        "STORY_NOT_PROVIDED",
    ]


class InvestigateStoryResult(AgentToolContract):
    manifest_id: UUID
    startup_manifest_id: UUID
    evidence_round: int = Field(strict=True, ge=1, le=32)
    materials: list[ResearchMaterial] = Field(max_length=8)
    missing: list[ResearchMissing] = Field(max_length=8)
    has_more: bool
    reused_binding: bool
