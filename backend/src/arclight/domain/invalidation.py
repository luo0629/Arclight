"""Deterministic change scopes, shared by propagation and late output validation."""

from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from arclight.domain.dependencies import DependencyInput

ChangeKind = Literal["CONTENT", "STYLE", "REFERENCE", "DIALOGUE", "SEQUENCE", "RENDER", "LIFECYCLE"]


class DependencyChange(DependencyInput):
    model_config = ConfigDict(extra="forbid")
    dependency_type: Literal["CONTENT_DEPENDENCY"] = "CONTENT_DEPENDENCY"
    change_kind: ChangeKind = "CONTENT"
    expected_revision: int = Field(strict=True, ge=0)


class InvalidationCreate(DependencyChange):
    creation_key: UUID
    reason: str = Field(min_length=1, max_length=1000)


class BatchInvalidationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    creation_key: UUID
    reason: str = Field(min_length=1, max_length=1000)
    changes: list[DependencyChange] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def distinct_changes(self):
        keys = [
            (item.source_asset_version_id, item.change_kind, tuple(sorted(item.selectors)))
            for item in self.changes
        ]
        if len(keys) != len(set(keys)):
            raise ValueError("相同版本、变更类型和范围只能出现一次")
        return self


def selectors_overlap(changed: list[str], consumed: list[str]) -> bool:
    # Empty means the whole source. Colon-separated stable identities allow a
    # producer to consume either a complete field or an individual dialogue line.
    return (
        not changed
        or not consumed
        or any(
            left == right or left.startswith(right + ":") or right.startswith(left + ":")
            for left in changed
            for right in consumed
        )
    )


def affects_input(change: dict, item: dict, target_kind: str) -> bool:
    if change["source_asset_version_id"] != item["source_asset_version_id"]:
        return False
    kind, edge = change["change_kind"], item["dependency_type"]
    if kind == "DIALOGUE":
        affected = edge == "CONTENT_DEPENDENCY" and target_kind in {"AUDIO", "SUBTITLE", "RENDER"}
    elif kind == "SEQUENCE":
        affected = edge == "SEQUENCE_DEPENDENCY" and (
            target_kind in {"TIMELINE", "RENDER"}
            or (item.get("asset_type") == "TIMELINE" and target_kind == "SUBTITLE")
        )
    elif kind == "STYLE":
        affected = edge == "STYLE_DEPENDENCY"
    elif kind == "REFERENCE":
        affected = edge in {"REFERENCE_DEPENDENCY", "CONTENT_DEPENDENCY", "RENDER_DEPENDENCY"}
    elif kind == "RENDER":
        affected = edge == "RENDER_DEPENDENCY"
    else:
        affected = True
    return affected and selectors_overlap(change.get("selectors", []), item.get("selectors", []))


def required_action(kind: str) -> str:
    if kind in {"TIMELINE", "RENDER"}:
        return "RENDER"
    return "REVIEW" if kind in {"SCRIPT", "STORYBOARD"} else "GENERATE"
