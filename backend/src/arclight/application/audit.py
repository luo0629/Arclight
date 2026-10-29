"""Audit facts join the caller's business transaction, including worker writes."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.persistence.models import AuditEvent


def audit(
    db: Session,
    project_id: UUID,
    actor: str,
    action: str,
    resource_id: UUID,
    details: dict | None = None,
) -> None:
    db.add(
        AuditEvent(
            project_id=project_id,
            actor_id=actor,
            action=action,
            resource_id=resource_id,
            details_json=details or {},
        )
    )
