"""Bounded requests for server-resolved, append-only retrieval evidence."""

from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

TRACE_VERSION = "context-trace/1"
MAX_EVIDENCE_ROUNDS = 32


class EvidenceRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    creation_key: UUID
    expected_round: int = Field(strict=True, ge=0, lt=MAX_EVIDENCE_ROUNDS)
    asset_version_ids: list[UUID] = Field(default_factory=list, max_length=64)
    story_entry_ids: list[UUID] = Field(default_factory=list, max_length=100)
    source_ids: list[UUID] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def exact_selections(self) -> "EvidenceRequest":
        lists = (self.asset_version_ids, self.story_entry_ids, self.source_ids)
        if not any(lists) or any(len(values) != len(set(values)) for values in lists):
            raise ValueError("检索证据必须指定不重复的精确输入")
        return self
