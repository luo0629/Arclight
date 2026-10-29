"""Project-scoped immutable file metadata; private storage keys are never returned."""

from uuid import UUID

from fastapi import APIRouter
from pydantic import computed_field
from sqlalchemy import select

from arclight.api.dependencies import Actor, Database
from arclight.api.routes import Limit, Offset
from arclight.api.schemas import Resource
from arclight.application.commands import require_project
from arclight.core.errors import DomainError
from arclight.persistence.models import StoredFile

router = APIRouter(prefix="/api/v1/projects/{project_id}/files", tags=["files"])


class FileRead(Resource):
    asset_version_id: UUID | None
    checksum: str
    byte_size: int
    media_type: str
    original_name: str
    source_note: str
    rights_note: str
    rights_confirmed: bool
    created_by: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def stage(self) -> str:
        return "REGISTERED" if self.asset_version_id is not None else "CANDIDATE"


def find_file(db: Database, project_id: UUID, file_id: UUID) -> StoredFile:
    row = db.get(StoredFile, file_id)
    if row is None or row.project_id != project_id:
        raise DomainError("NOT_FOUND", "文件不存在", 404)
    return row


@router.get("")
def list_files(
    project_id: UUID, db: Database, actor: Actor, limit: Limit = 20, offset: Offset = 0
) -> list[FileRead]:
    require_project(db, project_id, actor)
    return [
        FileRead.model_validate(row)
        for row in db.scalars(
            select(StoredFile)
            .where(StoredFile.project_id == project_id)
            .order_by(StoredFile.created_at, StoredFile.id)
            .limit(limit)
            .offset(offset)
        )
    ]


@router.get("/{file_id}")
def get_file(project_id: UUID, file_id: UUID, db: Database, actor: Actor) -> FileRead:
    require_project(db, project_id, actor)
    return FileRead.model_validate(find_file(db, project_id, file_id))
