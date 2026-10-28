"""Append visual snapshots under the project write gate; preserve legacy keys."""

import hashlib
import json
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.domain.visual_bible import FIELD_NAMES, VisualFields
from arclight.persistence.models import Project, VisualBibleVersion


def latest(db: Session, project_id: UUID) -> VisualBibleVersion | None:
    return db.scalar(
        select(VisualBibleVersion)
        .where(VisualBibleVersion.project_id == project_id)
        .order_by(VisualBibleVersion.version_no.desc())
        .limit(1)
    )


def state(db: Session, project: Project) -> dict:
    row = latest(db, project.id)
    if row is not None:
        return {
            "id": row.id,
            "version_no": row.version_no,
            "fields": {name: getattr(row, name) for name in FIELD_NAMES},
            "locked_fields": row.locked_fields,
            "created_by": row.created_by,
            "created_at": row.created_at,
            "change_kind": row.change_kind,
            "legacy_snapshot": row.legacy_snapshot,
        }
    # Existing arbitrary configuration is preserved read-only; only text description is mapped.
    description = project.style_guide.get("description")
    fields = VisualFields(
        art_style=description if isinstance(description, str) and len(description) <= 10000 else ""
    )
    return {
        "id": None,
        "version_no": 0,
        "fields": fields.model_dump(),
        "locked_fields": [],
        "created_by": None,
        "created_at": None,
        "change_kind": "LEGACY",
        "legacy_snapshot": project.style_guide,
    }


def append_version(
    db: Session,
    project: Project,
    actor: str,
    expected: int,
    key: UUID,
    patch: dict | None = None,
    locks: list[str] | None = None,
) -> tuple[VisualBibleVersion, bool]:
    kind = "EDIT" if patch is not None else "LOCK"
    fingerprint = hashlib.sha256(
        json.dumps(
            {"kind": kind, "expected": expected, "patch": patch, "locks": locks},
            sort_keys=True,
            ensure_ascii=False,
        ).encode()
    ).hexdigest()
    existing = db.scalar(
        select(VisualBibleVersion).where(
            VisualBibleVersion.project_id == project.id,
            VisualBibleVersion.creation_key == key,
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同保存键不能用于不同视觉规范", 409)
        return existing, False
    current = state(db, project)
    if current["version_no"] != expected:
        raise DomainError("VERSION_CONFLICT", "视觉规范已变化，请保留草稿并重新载入", 409)
    fields = current["fields"]
    locked = current["locked_fields"]
    if patch is not None:
        if any(name in locked and fields[name] != value for name, value in patch.items()):
            raise DomainError("VISUAL_FIELD_LOCKED", "请由Owner先解锁对应视觉规范字段", 409)
        try:
            fields = VisualFields(**{**fields, **patch}).model_dump()
        except ValidationError as error:
            raise DomainError(
                "INVALID_VISUAL_FIELDS", "视觉规范字段超出范围或格式不正确", 422
            ) from error
    else:
        locked = sorted(locks or [])
    if fields == current["fields"] and locked == current["locked_fields"]:
        raise DomainError("NO_VISUAL_CHANGE", "视觉规范没有变化", 422)
    row = VisualBibleVersion(
        project_id=project.id,
        version_no=expected + 1,
        **fields,
        locked_fields=locked,
        legacy_snapshot=current["legacy_snapshot"],
        change_kind=kind,
        created_by=actor,
        creation_key=key,
        request_fingerprint=fingerprint,
    )
    db.add(row)
    # Compatibility view for text providers. Unknown legacy keys survive unchanged.
    style = dict(project.style_guide)
    for name, value in (patch or {}).items():
        style["description" if name == "art_style" else name] = value
    project.style_guide = style
    db.flush()
    return row, True


def snapshot(db: Session, project: Project) -> dict:
    current = state(db, project)
    return {
        "version_id": str(current["id"]) if current["id"] else None,
        "version_no": current["version_no"],
        "fields": current["fields"],
        "locked_fields": current["locked_fields"],
    }
