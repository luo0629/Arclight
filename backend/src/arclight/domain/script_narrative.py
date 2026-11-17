"""Versioned writing targets and unconfirmed changes, independent of legacy text schemas."""

from typing import Literal, Self
from uuid import UUID

from pydantic import Field, model_validator

from arclight.domain.story_ledger import (
    LedgerKnowledge,
    LedgerText,
    LedgerValue,
    StateKey,
    StoryTime,
)


class NarrativeRequest(LedgerValue):
    series_plan_version_id: UUID
    ledger_version_id: UUID | None
    expected_ledger_head_revision: int = Field(strict=True, ge=0)
    target_story_time: StoryTime
    target_scene_no: int = Field(default=1, strict=True, ge=1, le=50)
    character_ids: list[UUID] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def unique_characters(self) -> Self:
        if len(self.character_ids) != len(set(self.character_ids)):
            raise ValueError("Writing perspectives must be unique")
        return self


class NarrativeContext(NarrativeRequest):
    schema_version: Literal["writing-context/1"] = "writing-context/1"
    label: Literal["WRITING_TARGET"] = "WRITING_TARGET"
    project_id: UUID
    episode_id: UUID
    episode_no: int = Field(strict=True, ge=1, le=10_000)
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_source: dict
    ledger_content_sha256: str | None
    ledger_source_manifest_sha256: str | None
    ledger_approval_id: UUID | None
    ledger_source: dict | None

    @model_validator(mode="after")
    def complete_ledger_proof(self) -> Self:
        proof = [
            self.ledger_content_sha256,
            self.ledger_source_manifest_sha256,
            self.ledger_approval_id,
            self.ledger_source,
        ]
        if self.ledger_version_id is None:
            if any(value is not None for value in proof):
                raise ValueError("An empty ledger baseline cannot carry a ledger proof")
        elif any(value is None for value in proof):
            raise ValueError("An exact ledger baseline requires all approval proofs")
        return self


class CharacterObjective(LedgerValue):
    character_id: UUID
    purpose: LedgerText
    emotion_start: LedgerText
    emotion_end: LedgerText


class DialogueInteraction(LedgerValue):
    line_ids: list[UUID] = Field(min_length=1, max_length=100)
    character_ids: list[UUID] = Field(min_length=1, max_length=50)
    action: LedgerText

    @model_validator(mode="after")
    def unique_references(self) -> Self:
        if len(self.line_ids) != len(set(self.line_ids)) or len(self.character_ids) != len(
            set(self.character_ids)
        ):
            raise ValueError("Interaction references must be unique")
        return self


class DramaticBeat(LedgerValue):
    story_time: StoryTime
    character_objectives: list[CharacterObjective] = Field(min_length=1, max_length=50)
    conflict: LedgerText
    dialogue_interactions: list[DialogueInteraction] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def unique_objectives(self) -> Self:
        ids = [item.character_id for item in self.character_objectives]
        if len(ids) != len(set(ids)):
            raise ValueError("A scene has one objective per character")
        if any(not set(item.character_ids) <= set(ids) for item in self.dialogue_interactions):
            raise ValueError("Interactions must reference scene characters")
        return self


class StateChangeProposal(LedgerValue):
    """Source identities and CONFIRMED authority are assigned only by the review service."""

    id: UUID
    kind: Literal["EVENT", "RELATIONSHIP", "KNOWLEDGE", "INJURY", "COSTUME", "PROP_OWNERSHIP"]
    subject_id: UUID | None = None
    target_id: UUID | None = None
    state_key: StateKey
    value: LedgerText
    story_start: StoryTime
    story_end: StoryTime | None = None
    known_by: list[LedgerKnowledge] = Field(default_factory=list, max_length=100)
    causes: list[UUID] = Field(default_factory=list, max_length=50)
    supersedes_entry_id: UUID | None = None

    @model_validator(mode="after")
    def complete_change(self) -> Self:
        if self.story_end is not None and self.story_end <= self.story_start:
            raise ValueError("State intervals must be nonempty [start, end)")
        if self.kind != "EVENT" and self.subject_id is None:
            raise ValueError("State changes require an exact subject")
        if self.kind == "RELATIONSHIP" and (
            self.target_id is None or self.target_id == self.subject_id
        ):
            raise ValueError("A relationship requires distinct directed characters")
        if self.id in self.causes or len(self.causes) != len(set(self.causes)):
            raise ValueError("Causes must be unique and cannot refer to the proposal itself")
        for index, left in enumerate(self.known_by):
            for right in self.known_by[index + 1 :]:
                if (
                    left.character_id == right.character_id
                    and (left.story_end is None or right.story_start < left.story_end)
                    and (right.story_end is None or left.story_start < right.story_end)
                ):
                    raise ValueError("Knowledge intervals for a character cannot overlap")
        return self
