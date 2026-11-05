"""Explicit Owner confirmation, immutable revisions and task-scoped preference reads."""

from datetime import UTC
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from arclight.application.audit import audit
from arclight.application.commands import require_project
from arclight.application.generation_leases import utc
from arclight.core.errors import DomainError
from arclight.domain.agent_memory import PREFERENCE_TASKS, PreferenceChange
from arclight.domain.versions import digest
from arclight.persistence.agent_preferences import AgentPreference
from arclight.persistence.models import User


def preference_snapshot(row: AgentPreference) -> dict:
    return {
        "id": str(row.id),
        "project_id": str(row.project_id),
        "preference_name": row.preference_name,
        "revision": row.revision,
        "value": row.value,
        "confirmed_by": row.confirmed_by,
        "confirmed_at": utc(row.created_at).astimezone(UTC).isoformat(),
        "confirmation_rule": "USER_CONFIRMED_PREFERENCE",
    }


def latest_preferences(db: Session, project_id: UUID) -> list[AgentPreference]:
    latest = (
        select(
            AgentPreference.preference_name, func.max(AgentPreference.revision).label("revision")
        )
        .where(AgentPreference.project_id == project_id)
        .group_by(AgentPreference.preference_name)
        .subquery()
    )
    return list(
        db.scalars(
            select(AgentPreference)
            .join(
                latest,
                (AgentPreference.preference_name == latest.c.preference_name)
                & (AgentPreference.revision == latest.c.revision),
            )
            .where(AgentPreference.project_id == project_id)
            .order_by(AgentPreference.preference_name)
        )
    )


def read_preferences(db: Session, project_id: UUID, actor: str) -> list[dict]:
    require_project(db, project_id, actor)
    return [preference_snapshot(row) for row in latest_preferences(db, project_id)]


def snapshot_preferences(db: Session, project_id: UUID, task: str) -> list[dict]:
    """Called only after the Context Builder's existing project authorization."""
    return [
        preference_snapshot(row)
        for row in latest_preferences(db, project_id)
        if row.value is not None and task in PREFERENCE_TASKS[row.preference_name]
    ]


def confirm_preference(
    db: Session, project_id: UUID, actor: str, change: PreferenceChange
) -> tuple[AgentPreference, bool]:
    change = PreferenceChange.model_validate(change.model_dump(mode="json"))
    if actor != "service-owner":
        user = db.get(User, UUID(actor), populate_existing=True)
        if user is None or user.disabled:
            raise DomainError("FORBIDDEN", "当前用户不能确认长期偏好", 403)
    # This is the same short project write gate used by startup snapshot capture.
    require_project(db, project_id, actor, write=True, owner=True)
    checksum = digest({"actor": actor, "change": change.model_dump(mode="json")})
    previous = db.scalar(
        select(AgentPreference).where(
            AgentPreference.project_id == project_id,
            AgentPreference.creation_key == change.creation_key,
        )
    )
    if previous is not None:
        if previous.request_sha256 != checksum:
            raise DomainError("IDEMPOTENCY_CONFLICT", "同一确认键不能更换偏好或操作者", 409)
        return previous, False
    current = db.scalar(
        select(AgentPreference)
        .where(
            AgentPreference.project_id == project_id,
            AgentPreference.preference_name == change.preference_name,
        )
        .order_by(AgentPreference.revision.desc())
        .limit(1)
    )
    revision = current.revision if current is not None else 0
    if revision != change.expected_revision:
        raise DomainError("AGENT_PREFERENCE_CONFLICT", "偏好修订已变化，请刷新后确认", 409)
    if change.value is None and current is None:
        raise DomainError("AGENT_PREFERENCE_MISSING", "尚无可撤销的已确认偏好", 409)
    row = AgentPreference(
        project_id=project_id,
        preference_name=change.preference_name,
        revision=revision + 1,
        value=change.value,
        confirmed_by=actor,
        creation_key=change.creation_key,
        request_sha256=checksum,
    )
    db.add(row)
    db.flush()
    audit(
        db,
        project_id,
        actor,
        "agent.preference_confirmed",
        row.id,
        {
            "preference_name": row.preference_name,
            "revision": row.revision,
            "retired": row.value is None,
        },
    )
    return row, True


def preference_source(snapshot: dict) -> dict:
    return {
        "kind": "AGENT_PREFERENCE",
        "id": snapshot["id"],
        "revision": snapshot["revision"],
        "sha256": digest(snapshot),
    }


def validate_preference_source(db: Session, project_id: UUID, source: dict) -> None:
    row = db.get(AgentPreference, UUID(source["id"]))
    if (
        row is None
        or row.project_id != project_id
        or row.value is None
        or preference_source(preference_snapshot(row)) != source
    ):
        raise DomainError("CONTEXT_REFERENCE_INVALID", "已确认偏好来源缺失或不匹配", 409)
