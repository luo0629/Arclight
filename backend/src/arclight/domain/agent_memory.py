"""Memory is a bounded projection of authority, never a model-writable fact store."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.agent_state import (
    AgentDraftReference,
    AgentPlanStep,
    AgentStep,
    Checksum,
)
from arclight.domain.generation_requests import safe_metadata

MEMORY_VERSION: Literal["agent-memory/1"] = "agent-memory/1"
MAX_MEMORY_SOURCES = 256
PreferenceKey = Literal["narrative_tone", "dialogue_style", "pacing", "visual_tone"]
PREFERENCE_TASKS: dict[str, frozenset[str]] = {
    "narrative_tone": frozenset({"outline", "script", "rewrite", "producer"}),
    "dialogue_style": frozenset({"script", "rewrite", "producer"}),
    "pacing": frozenset({"outline", "script", "storyboard", "rewrite", "producer"}),
    "visual_tone": frozenset({"storyboard", "art", "image", "review", "producer"}),
}
ConfirmationRule = Literal[
    "APPROVED_ASSET_VERSION",
    "PROJECT_STORY_ENTRY",
    "PROJECT_VISUAL_SPEC",
    "CONFIRMED_OUTLINE",
    "USER_CONFIRMED_PREFERENCE",
]


class MemoryValue(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, hide_input_in_errors=True)


class PreferenceChange(MemoryValue):
    creation_key: UUID
    expected_revision: int = Field(strict=True, ge=0)
    preference_name: PreferenceKey
    value: str | None = Field(min_length=1, max_length=1000)
    confirmation: Literal["CONFIRM"]

    @model_validator(mode="after")
    def explicit_safe_value(self):
        if self.value is not None and not self.value.strip():
            raise ValueError("A preference cannot contain only whitespace")
        safe_metadata(self.model_dump(mode="json"))
        return self


class MemorySource(MemoryValue):
    source_index: int = Field(strict=True, ge=0)
    source_id: UUID
    source_sha256: Checksum
    category: Literal["DOMAIN_FACT", "EVIDENCE", "LONG_TERM_PREFERENCE"]
    confirmation_rule: ConfirmationRule | None = None
    confirmed_by: str | None = Field(default=None, min_length=1, max_length=100)

    @model_validator(mode="after")
    def consistent_authority(self):
        if self.category == "EVIDENCE":
            if self.confirmation_rule is not None or self.confirmed_by is not None:
                raise ValueError("Evidence cannot assert confirmation")
        elif self.category == "LONG_TERM_PREFERENCE":
            if self.confirmation_rule != "USER_CONFIRMED_PREFERENCE" or not self.confirmed_by:
                raise ValueError("A preference requires explicit recorded human confirmation")
        elif self.confirmation_rule in {None, "USER_CONFIRMED_PREFERENCE"}:
            raise ValueError("A domain fact requires a registered domain rule")
        return self


class MemoryReference(MemorySource):
    manifest_id: UUID
    evidence_round: int = Field(strict=True, ge=0, le=32)


class WorkingMemory(MemoryValue):
    state_revision: int | None = Field(default=None, ge=0)
    plan: list[AgentPlanStep] = Field(default_factory=list, max_length=16)
    steps: list[AgentStep] = Field(default_factory=list, max_length=16)
    observation_ids: list[UUID] = Field(default_factory=list, max_length=256)
    tool_invocation_ids: list[UUID] = Field(default_factory=list, max_length=256)
    drafts: list[AgentDraftReference] = Field(default_factory=list, max_length=16)


class AgentMemory(MemoryValue):
    memory_version: Literal["agent-memory/1"] = MEMORY_VERSION
    run_id: UUID
    project_id: UUID
    stage: str
    input_sha256: Checksum
    startup_manifest_id: UUID
    constraints_sha256: Checksum
    legacy_without_authority: bool
    working: WorkingMemory
    domain_facts: list[MemoryReference] = Field(max_length=MAX_MEMORY_SOURCES)
    evidence: list[MemoryReference] = Field(max_length=MAX_MEMORY_SOURCES)
    long_term_preferences: list[MemoryReference] = Field(max_length=4)
    constraint_precedence: Literal["REQUIRED_CONSTRAINTS"] = "REQUIRED_CONSTRAINTS"
    preference_role: Literal["ADVISORY_DATA"] = "ADVISORY_DATA"

    @model_validator(mode="after")
    def bounded_sources(self):
        rows = self.domain_facts + self.evidence + self.long_term_preferences
        if len(rows) > MAX_MEMORY_SOURCES:
            raise ValueError("Required memory sources exceed the limit and cannot be trimmed")
        if len({(row.manifest_id, row.source_index) for row in rows}) != len(rows):
            raise ValueError("Each source belongs to exactly one memory category")
        for items, category in (
            (self.domain_facts, "DOMAIN_FACT"),
            (self.evidence, "EVIDENCE"),
            (self.long_term_preferences, "LONG_TERM_PREFERENCE"),
        ):
            if any(row.category != category for row in items):
                raise ValueError("Memory source category and partition differ")
        return self
