"""Read-only notifications over existing, committed event records.

Timestamps are not commit sequence numbers. A cursor records the anchor's
position too: an older transaction appearing before it requires replay, rather
than silently skipping that transaction. Replayed notifications retain their
source identity; authoritative resources still come from the normal APIs.
"""

import re
from dataclasses import dataclass
from datetime import datetime
from uuid import UUID

from sqlalchemy import func, literal, select, tuple_, union_all
from sqlalchemy.orm import Session

from arclight.application.commands import require_project, require_run
from arclight.core.errors import DomainError
from arclight.persistence.models import AuditEvent, Episode, WorkflowEvent, WorkflowRun

PAGE_SIZE = 100
CURSOR = re.compile(r"v1:([aw]):([0-9a-f-]{36}):([1-9][0-9]{0,15})\Z")


@dataclass(frozen=True)
class Cursor:
    source: str
    identity: UUID
    position: int

    def encode(self) -> str:
        return f"v1:{self.source}:{self.identity}:{self.position}"

    @classmethod
    def parse(cls, value: str | None) -> "Cursor | None":
        if not value:
            return None
        match = CURSOR.fullmatch(value)
        if match:
            try:
                return cls(match[1], UUID(match[2]), int(match[3]))
            except ValueError:
                pass
        raise DomainError("EVENT_CURSOR_INVALID", "事件续传标记无效，请重新读取当前状态", 400)


@dataclass(frozen=True)
class Notification:
    cursor: str
    event: str
    data: dict


@dataclass(frozen=True)
class EventPage:
    project_id: UUID
    events: tuple[Notification, ...]
    cursor: str | None
    reset: bool = False
    has_more: bool = False


def public_event(source: str, name: str) -> str:
    if source == "w":
        return {
            "workflow.queued": "run.status_changed",
            "workflow.started": "run.started",
            "workflow.waiting_review": "review.required",
            "workflow.finished": "run.completed",
            "workflow.failed": "run.failed",
            "review.submitted": "review.completed",
        }.get(name, "run.status_changed")
    if name.startswith("generation."):
        return {
            "generation.created": "generation.queued",
            "generation.succeeded": "generation.completed",
            "generation.failed": "generation.failed",
        }.get(name, "generation.progress")
    if name.startswith("render."):
        return "render.completed" if name == "render.succeeded" else "render.progress"
    if name == "asset.stale":
        return "asset.stale"
    return "project.changed"


def read_events(
    db: Session,
    actor: str,
    *,
    project_id: UUID | None = None,
    run_id: UUID | None = None,
    cursor: Cursor | None = None,
    tail: bool = False,
) -> EventPage:
    if run_id is not None:
        run = require_run(db, run_id, actor)
        episode = db.get(Episode, run.episode_id)
        assert episode is not None  # require_run already checked the episode.
        project_id = episode.project_id
    if project_id is None:
        raise ValueError("An event stream requires a project or run scope")
    require_project(db, project_id, actor)
    workflow = (
        select(
            WorkflowEvent.id.label("id"),
            WorkflowEvent.created_at.label("time"),
            literal("w").label("source"),
            WorkflowEvent.event_type.label("name"),
        )
        .join(WorkflowRun, WorkflowRun.id == WorkflowEvent.run_id)
        .join(Episode, Episode.id == WorkflowRun.episode_id)
        .where(Episode.project_id == project_id)
    )
    if run_id is not None:
        feed = workflow.where(WorkflowEvent.run_id == run_id).subquery()
    else:
        audit = select(
            AuditEvent.id.label("id"),
            AuditEvent.created_at.label("time"),
            literal("a").label("source"),
            AuditEvent.action.label("name"),
        ).where(AuditEvent.project_id == project_id)
        feed = union_all(audit, workflow).subquery()

    order = (feed.c.time, feed.c.source, feed.c.id)
    key = tuple_(*order)
    anchor = None
    reset = False
    if cursor:
        anchor = (
            db.execute(
                select(feed).where(feed.c.id == cursor.identity, feed.c.source == cursor.source)
            )
            .mappings()
            .first()
        )
        if anchor is not None:
            position = db.scalar(
                select(func.count())
                .select_from(feed)
                .where(key <= tuple_(anchor["time"], anchor["source"], anchor["id"]))
            )
            if position != cursor.position:
                anchor = None
        if anchor is None:
            # Unknown, foreign, removed or reordered anchors share one response.
            reset = True
            cursor = None
    if tail and cursor is None and not reset:
        last = db.execute(select(feed).order_by(*(c.desc() for c in order)).limit(1))
        row = last.mappings().first()
        if row is not None:
            position = db.scalar(
                select(func.count())
                .select_from(feed)
                .where(key <= tuple_(row["time"], row["source"], row["id"]))
            )
            cursor = Cursor(row["source"], row["id"], position or 1)
        return EventPage(project_id, (), cursor.encode() if cursor else None)

    statement = select(feed).order_by(*order).limit(PAGE_SIZE)
    if anchor is not None:
        statement = statement.where(key > tuple_(anchor["time"], anchor["source"], anchor["id"]))
    rows = db.execute(statement).mappings().all()
    position = cursor.position if cursor else 0
    notices = []
    for row in rows:
        position += 1
        cursor = Cursor(row["source"], row["id"], position)
        name = public_event(row["source"], row["name"])
        timestamp: datetime = row["time"]
        notices.append(
            Notification(
                cursor.encode(),
                name,
                {
                    "project_id": str(project_id),
                    "source": row["source"],
                    "source_id": str(row["id"]),
                    "created_at": timestamp.isoformat(),
                },
            )
        )
    return EventPage(
        project_id,
        tuple(notices),
        cursor.encode() if cursor else None,
        reset,
        len(rows) == PAGE_SIZE,
    )
