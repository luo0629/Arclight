"""Current character admission and immutable evidence for future narrative plans."""

import copy
import hashlib
import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.domain.series_planning import SeriesOutlineContent
from arclight.persistence.models import StoryEntry


def character_references(db: Session, project_id: UUID, content: SeriesOutlineContent) -> dict:
    ids = [item.character_id for item in content.series_plan.character_arcs]
    rows = {
        row.id: row
        for row in db.scalars(
            select(StoryEntry)
            .where(StoryEntry.project_id == project_id, StoryEntry.id.in_(ids))
            .execution_options(populate_existing=True)
        )
    }
    references = []
    for arc in content.series_plan.character_arcs:
        row = rows.get(arc.character_id)
        if row is None or row.kind != "CHARACTER" or row.archived:
            raise DomainError("SERIES_CHARACTER_NOT_FOUND", "请选择当前项目中未归档的人物设定", 422)
        if row.revision != arc.character_revision:
            raise DomainError(
                "SERIES_CHARACTER_STALE", "人物设定已变化，请读取当前修订后另存规划", 409
            )
        captured = {
            "id": str(row.id),
            "revision": row.revision,
            "name": row.name,
            "kind": row.kind,
            "data": row.data_json,
        }
        references.append(
            {
                "id": str(row.id),
                "revision": row.revision,
                "name": row.name,
                "sha256": hashlib.sha256(
                    json.dumps(captured, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest(),
            }
        )
    return {"schema_version": "1.0", "kind": "PLANNED", "characters": references}


def planning_source(
    db: Session, project_id: UUID, content: SeriesOutlineContent, source_snapshot: dict
) -> dict:
    source = copy.deepcopy(source_snapshot)
    source["series_plan_references"] = character_references(db, project_id, content)
    return source


def validate_planning_source(
    db: Session, project_id: UUID, content: SeriesOutlineContent, source_snapshot: dict
) -> None:
    current = character_references(db, project_id, content)
    if source_snapshot.get("series_plan_references") != current:
        raise DomainError("SERIES_CHARACTER_STALE", "规划的人物来源已变化，请另存后重新确认", 409)
