"""Task scopes and hard limits shared by preview, capture and actual consumption."""

import json
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.agents.script_rewrite import RewriteTarget
from arclight.core.errors import DomainError
from arclight.domain.generation_requests import safe_metadata

ContextTask = Literal[
    "analyze", "outline", "script", "storyboard", "rewrite", "art", "image", "review", "producer"
]
BUILDER_VERSION = "context/1"


class ContextLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    max_bytes: int = Field(default=262144, ge=1024, le=524288)
    max_assets: int = Field(default=64, ge=1, le=64)
    max_story_entries: int = Field(default=100, ge=1, le=100)
    neighbor_radius: int = Field(default=1, ge=0, le=2)
    max_history: int = Field(default=5, ge=0, le=10)

    def restrict(self, requested: "ContextLimits | None") -> "ContextLimits":
        if requested is None:
            return self
        return ContextLimits.model_validate(
            {
                name: min(value, getattr(requested, name))
                for name, value in self.model_dump().items()
            }
        )


class ContextRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task: ContextTask
    episode_id: UUID
    script_version_id: UUID | None = None
    shot_id: UUID | None = None
    candidate_version_id: UUID | None = None
    rewrite_target: RewriteTarget | None = None
    asset_version_ids: list[UUID] = Field(default_factory=list, max_length=64)
    story_entry_ids: list[UUID] = Field(default_factory=list, max_length=100)
    limits: ContextLimits | None = None

    @model_validator(mode="after")
    def unique_selections(self) -> "ContextRequest":
        for values in (self.asset_version_ids, self.story_entry_ids):
            if len(set(values)) != len(values):
                raise ValueError("上下文选择不可重复")
        return self


@dataclass(frozen=True)
class TaskContract:
    style: bool = False
    entries: bool = False
    assets: bool = False
    script: bool = False
    shot: bool = False
    editable: tuple[str, ...] = ()


TASK_CONTRACTS: dict[str, TaskContract] = {
    "analyze": TaskContract(editable=("analysis",)),
    "outline": TaskContract(entries=True, editable=("outline.episodes",)),
    "script": TaskContract(True, True, True, editable=("script",)),
    "storyboard": TaskContract(True, True, True, True, editable=("storyboard.shots",)),
    "rewrite": TaskContract(True, True, True, True, editable=("request.target",)),
    "art": TaskContract(True, True, True, True, editable=("art.proposal",)),
    "image": TaskContract(True, True, True, True, True, ("candidate_image",)),
    "review": TaskContract(True, True, True, True, True, ("review.decision",)),
    "producer": TaskContract(editable=("production.proposal",)),
}


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def check_size(value: dict, limits: ContextLimits) -> None:
    try:
        safe_metadata(value)
        size = len(json_bytes(value))
    except (TypeError, ValueError) as exc:
        raise DomainError("CONTEXT_INPUT_UNSAFE", "上下文包含不支持或不安全的数据", 422) from exc
    if size > limits.max_bytes:
        raise DomainError("CONTEXT_TOO_LARGE", "必需上下文超过输入限额，请缩小任务范围", 422)


def check_call(snapshot: dict, task_input: dict) -> dict:
    # Existing story provenance has a private storage locator. The model only
    # needs its stable source identity/checksum; object access remains server-side.
    inputs = deepcopy(task_input)
    sources = [inputs]
    outline = inputs.get("outline_context")
    if isinstance(outline, dict) and isinstance(outline.get("source_snapshot"), dict):
        sources.append(outline["source_snapshot"])
    for source in sources:
        revision = source.get("source_revision")
        if isinstance(revision, dict):
            revision.pop("object_ref", None)
    value = {"task_context": snapshot, "task_input": inputs}
    check_size(value, ContextLimits.model_validate(snapshot["limits"]))
    return value
