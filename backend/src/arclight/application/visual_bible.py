"""Append visual snapshots under the project write gate; preserve legacy keys."""

import hashlib
import json
from uuid import UUID

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.core.errors import DomainError
from arclight.domain.visual_bible import FIELD_NAMES, VisualFields, VisualPatch
from arclight.persistence.models import AuditEvent, Project, VisualBibleVersion

_ORIGINAL_FIELDS_CLEARED = "visual_bible.original_fields_cleared"


def _prompt_key(field: str) -> str:
    return "description" if field == "art_style" else field


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
    *,
    confirmed_locked_fields: frozenset[str] = frozenset(),
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
    original_clears: set[str] = set()
    if patch is not None:
        # A default blank and an explicit clear can have the same typed value.
        blank_fields = {
            name
            for name, value in patch.items()
            if value == ""
            and fields.get(name) == ""
            and project.style_guide.get(_prompt_key(name), "") != ""
        }
        if blank_fields:
            style = prompt_style(db, project, current)
            original_clears = {name for name in blank_fields if style[_prompt_key(name)] != ""}
        # Only the transactional art approval handler supplies the exact fields
        # acknowledged by the current Owner. Ordinary edit APIs never set this.
        if any(
            name in locked
            and name not in confirmed_locked_fields
            and (fields[name] != value or name in original_clears)
            for name, value in patch.items()
        ):
            raise DomainError("VISUAL_FIELD_LOCKED", "请由Owner先解锁对应视觉规范字段", 409)
        try:
            fields = VisualFields(**{**fields, **patch}).model_dump()
        except ValidationError as error:
            raise DomainError(
                "INVALID_VISUAL_FIELDS", "视觉规范字段超出范围或格式不正确", 422
            ) from error
    else:
        locked = sorted(locks or [])
    if fields == current["fields"] and locked == current["locked_fields"] and not original_clears:
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
    db.flush()
    if original_clears:
        # Keep the otherwise indistinguishable clear tied to this exact revision.
        audit(
            db,
            project.id,
            actor,
            _ORIGINAL_FIELDS_CLEARED,
            row.id,
            {"fields": sorted(original_clears)},
        )
    return row, True


def save_fields(
    db: Session,
    project: Project,
    actor: str,
    expected: int,
    key: UUID,
    fields: VisualPatch,
) -> VisualBibleVersion:
    row, created = append_version(
        db, project, actor, expected, key, patch=fields.model_dump(exclude_unset=True)
    )
    if created:
        audit(db, project.id, actor, "visual_bible.saved", row.id, {"version_no": row.version_no})
    return row


def prompt_style(db: Session, project: Project, visual: dict) -> dict:
    """Project typed visual edits into prompt data without rewriting the original settings."""
    style = dict(project.style_guide)
    potentially_cleared = []
    for field, value in visual["fields"].items():
        key = _prompt_key(field)
        original = style.get(key)
        # The original text description is the only field represented in the initial typed view.
        if value or (field == "art_style" and isinstance(original, str) and len(original) <= 10000):
            style[key] = value
        elif key in style and original != "":
            potentially_cleared.append((field, key))
    if potentially_cleared and visual["version_no"] > 0:
        # A default blank must not shadow an unedited original value; a real clear must.
        history = (
            VisualBibleVersion.project_id == project.id,
            VisualBibleVersion.version_no <= visual["version_no"],
        )
        previously_set = db.execute(
            select(
                *(
                    select(VisualBibleVersion.id)
                    .where(*history, getattr(VisualBibleVersion, field) != "")
                    .exists()
                    for field, _ in potentially_cleared
                )
            )
        ).one()
        first_clears: set[str] = set()
        if not all(previously_set):
            events = db.scalars(
                select(AuditEvent.details_json)
                .join(VisualBibleVersion, VisualBibleVersion.id == AuditEvent.resource_id)
                .where(
                    *history,
                    AuditEvent.project_id == project.id,
                    AuditEvent.action == _ORIGINAL_FIELDS_CLEARED,
                )
            )
            first_clears = {field for event in events for field in event["fields"]}
        for (field, key), was_set in zip(potentially_cleared, previously_set, strict=True):
            if was_set or field in first_clears:
                style[key] = ""
    return style


def snapshot(db: Session, project: Project) -> dict:
    current = state(db, project)
    return {
        "version_id": str(current["id"]) if current["id"] else None,
        "version_no": current["version_no"],
        "fields": current["fields"],
        "locked_fields": current["locked_fields"],
    }
