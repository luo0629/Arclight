"""Read related project facts within the same snapshot as existing canvas reads."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.persistence.models import Project


def lock_project_snapshot(db: Session, project_id: UUID) -> None:
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        driver = connection.connection.driver_connection
        assert driver is not None
        if not driver.in_transaction:
            connection.exec_driver_sql("BEGIN")
    project = db.get(Project, project_id, populate_existing=True, with_for_update={"read": True})
    if project is None or project.deleted_at is not None:
        raise DomainError("NOT_FOUND", "项目不存在", 404)
