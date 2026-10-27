"""Bounded deterministic story context; snapshot once, never re-read on retry."""

import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.content_references import require_asset
from arclight.core.errors import DomainError
from arclight.domain.character_names import canonical_name
from arclight.domain.story_bible import StoryEntryInput
from arclight.persistence.models import StoryEntry

MAX_STORY_ENTRIES = 100
MAX_STORY_CONTEXT_CHARS = 40000
RELATIONS = {
    "governs": ({"WORLD"}, {"CHARACTER", "LOCATION", "PROP", "SCENE"}),
    "located_in": ({"CHARACTER", "PROP", "SCENE"}, {"LOCATION"}),
    "appears_in": ({"CHARACTER", "PROP"}, {"SCENE"}),
    "uses": ({"CHARACTER", "SCENE"}, {"PROP"}),
}


def validate_entry(
    db: Session,
    project_id: UUID,
    value: StoryEntryInput,
    entry_id=None,
    *,
    check_names: bool = True,
) -> None:
    if value.kind == "CHARACTER" and check_names:
        names = {canonical_name(name) for name in [value.name, *value.aliases]}
        others = db.scalars(
            select(StoryEntry).where(
                StoryEntry.project_id == project_id, StoryEntry.kind == "CHARACTER"
            )
        )
        for other in others:
            if other.id == entry_id:
                continue
            other_names = {
                canonical_name(name) for name in [other.name, *other.data_json.get("aliases", [])]
            }
            if names & other_names:
                raise DomainError(
                    "CHARACTER_NAME_CONFLICT",
                    "角色名或别名已被使用（含归档角色），请复用或恢复现有角色",
                    409,
                )
    for link in value.relations:
        target = db.get(StoryEntry, link.target_id)
        source_kinds, target_kinds = RELATIONS[link.relation]
        if (
            target is None
            or target.project_id != project_id
            or target.archived
            or target.id == entry_id
            or value.kind not in source_kinds
            or target.kind not in target_kinds
        ):
            raise DomainError("INVALID_STORY_RELATION", "关联不存在、已归档或类型不匹配", 422)
    require_asset(db, project_id, value.asset, {value.kind})


def build_story_context(db: Session, project_id: UUID, *, validate_assets: bool = True) -> dict:
    rows = db.scalars(
        select(StoryEntry)
        .where(StoryEntry.project_id == project_id, StoryEntry.archived.is_(False))
        .order_by(StoryEntry.id)
        .limit(MAX_STORY_ENTRIES + 1)
    ).all()
    if len(rows) > MAX_STORY_ENTRIES:
        raise DomainError(
            "STORY_CONTEXT_TOO_LARGE", "活动故事设定最多 100 条，请先归档不再使用的设定", 422
        )
    entries = []
    for row in rows:
        value = StoryEntryInput.model_validate(
            {"kind": row.kind, "name": row.name, **row.data_json}
        )
        if validate_assets:
            validate_entry(db, project_id, value, row.id, check_names=False)
        entries.append(
            {"id": str(row.id), "revision": row.revision, **value.model_dump(mode="json")}
        )
    snapshot = {"schema_version": 1, "project_id": str(project_id), "entries": entries}
    if len(json.dumps(snapshot, ensure_ascii=False)) > MAX_STORY_CONTEXT_CHARS:
        raise DomainError(
            "STORY_CONTEXT_TOO_LARGE", "故事设定上下文超过 40000 字符，请精简或归档", 422
        )
    return snapshot
