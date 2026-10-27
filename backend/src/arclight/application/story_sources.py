"""Append-only source revisions and fixed source identities for text workflows."""

import base64
import hashlib
from dataclasses import dataclass, replace
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.domain.story_import import DecodedSource, decode_source
from arclight.persistence.models import (
    AuditEvent,
    Episode,
    StorySource,
    StorySourceBinding,
    StorySourceRevision,
    WorkflowRun,
)


@dataclass(frozen=True)
class SourceDraft:
    original_name: str
    media_type: str
    decoded: DecodedSource
    source_note: str
    rights_note: str
    origin_kind: str = "FILE"
    rights_status: str = "CONFIRMED"
    base_source_id: UUID | None = None
    change_note: str = ""


def require_source(db: Session, episode_id: UUID, source_id: UUID) -> StorySource:
    row = db.get(StorySource, source_id)
    if row is None or row.episode_id != episode_id:
        raise DomainError("NOT_FOUND", "故事来源不存在", 404)
    return row


def provenance(db: Session, source: StorySource) -> dict:
    revision = db.scalar(
        select(StorySourceRevision).where(StorySourceRevision.source_id == source.id)
    )
    return revision_snapshot(source, revision)


def revision_snapshot(source: StorySource, revision: StorySourceRevision | None) -> dict:
    # A compatibility fallback supports imported fixtures; persisted legacy sources
    # receive matching metadata in 0011 without changing their bytes, IDs or run JSON.
    return {
        "source_id": str(source.id),
        "root_source_id": str(revision.root_source_id if revision else source.id),
        "parent_source_id": str(revision.parent_source_id)
        if revision and revision.parent_source_id
        else None,
        "version_no": revision.version_no if revision else 1,
        "change_note": revision.change_note if revision else "既有文件导入",
        "origin_kind": revision.origin_kind if revision else "FILE",
        "rights_status": revision.rights_status if revision else "CONFIRMED",
        "source_note": source.source_note,
        "rights_note": source.rights_note,
        "encoding": source.encoding,
        "sha256": source.sha256,
        "object_ref": {
            "backend": "database",
            "key": revision.object_key if revision else f"story-sources/{source.id}/original",
            "sha256": source.sha256,
            "byte_size": source.byte_size,
        },
    }


def save_source(
    db: Session, episode: Episode, actor: str, draft: SourceDraft, key: UUID, fingerprint: str
) -> StorySource:
    # The caller holds the existing project write gate. Replay is checked before
    # checking the parent, so a lost successful response remains retryable.
    existing = db.scalar(
        select(StorySource).where(
            StorySource.episode_id == episode.id,
            StorySource.import_key == key,
        )
    )
    if existing:
        if existing.request_fingerprint != fingerprint:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一保存凭证不能用于不同来源内容或说明", 409)
        return existing
    base = require_source(db, episode.id, draft.base_source_id) if draft.base_source_id else None
    parent = provenance(db, base) if base else None
    if base:
        if not draft.change_note.strip():
            raise DomainError("SOURCE_CHANGE_NOTE_REQUIRED", "修订必须说明修改原因", 422)
        if db.scalar(
            select(StorySourceRevision.source_id).where(
                StorySourceRevision.parent_source_id == base.id,
            )
        ):
            raise DomainError(
                "SOURCE_VERSION_CONFLICT", "该来源已有新修订，请读取最新版本后再保存", 409
            )
    row = StorySource(
        id=uuid4(),
        episode_id=episode.id,
        import_no=(
            db.scalar(
                select(func.max(StorySource.import_no)).where(StorySource.episode_id == episode.id)
            )
            or 0
        )
        + 1,
        import_key=key,
        request_fingerprint=fingerprint,
        original_name=draft.original_name,
        source_type=draft.decoded.source_type,
        media_type=draft.media_type,
        encoding=draft.decoded.encoding,
        byte_size=len(draft.decoded.raw),
        sha256=draft.decoded.sha256,
        raw_bytes=draft.decoded.raw,
        text=draft.decoded.text,
        source_note=draft.source_note,
        rights_note=draft.rights_note,
        created_by=actor,
    )
    db.add(row)
    db.flush()
    db.add(
        StorySourceRevision(
            source_id=row.id,
            root_source_id=UUID(parent["root_source_id"]) if parent else row.id,
            parent_source_id=base.id if base else None,
            version_no=parent["version_no"] + 1 if parent else 1,
            change_note=draft.change_note or "创建来源",
            origin_kind=draft.origin_kind,
            rights_status=draft.rights_status,
            storage_backend="database",
            object_key=f"story-sources/{row.id}/original",
        )
    )
    db.add(
        AuditEvent(
            project_id=episode.project_id,
            actor_id=actor,
            action="story_source.revised" if base else "story_source.imported",
            resource_id=row.id,
            details_json={
                "episode_id": str(episode.id),
                "source_id": str(row.id),
                "base_source_id": str(base.id) if base else None,
                "sha256": row.sha256,
                "byte_size": row.byte_size,
                "rights_confirmed": draft.rights_status == "CONFIRMED",
            },
        )
    )
    db.flush()
    return row


def decode_inline_text(story: str, name: str) -> DecodedSource:
    try:
        encoded = base64.b64encode(story.encode("utf-8")).decode("ascii")
    except UnicodeError:
        raise DomainError("SOURCE_NOT_TEXT", "故事含无效文本字符", 422) from None
    decoded = decode_source(name, "text/plain", "utf-8", encoded)
    # Inline U+FEFF is part of the submitted text, not an imported file header.
    return replace(decoded, text=story, encoding="utf-8")


def capture_inline(db: Session, episode: Episode, actor: str, story: str) -> StorySource:
    decoded = decode_inline_text(story, "手动故事.txt")
    return save_source(
        db,
        episode,
        actor,
        SourceDraft(
            "手动故事.txt",
            "text/plain",
            decoded,
            "创作任务的手动输入",
            "未单独提供授权说明",
            origin_kind="INLINE",
            rights_status="UNDECLARED",
        ),
        uuid4(),
        hashlib.sha256(decoded.raw).hexdigest(),
    )


def bind_source(db: Session, run: WorkflowRun, source_id: UUID) -> None:
    db.add(StorySourceBinding(source_id=source_id, run_id=run.id))
