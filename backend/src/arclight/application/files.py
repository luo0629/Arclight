"""Exact file receipts share the asset transaction and never store binary content."""

from uuid import UUID, uuid4

from sqlalchemy.orm import Session

from arclight.core.config import Settings
from arclight.persistence.models import AssetVersion, StoredFile


def record_asset_file(db: Session, settings: Settings, project_id: UUID, row: AssetVersion) -> None:
    media = row.metadata_json["media"]
    db.add(
        StoredFile(
            id=uuid4(),
            project_id=project_id,
            asset_version_id=row.id,
            storage_backend=row.metadata_json["storage_backend"],
            bucket=settings.s3_bucket if row.metadata_json["storage_backend"] == "S3" else None,
            object_key=row.object_key,
            checksum=row.checksum,
            byte_size=media["byte_size"],
            media_type=media["media_type"],
            original_name=media["original_name"],
            source_note=media["source_note"],
            rights_note=media["rights_note"],
            rights_confirmed=media["rights_confirmed"],
            created_by=row.created_by,
        )
    )
    db.flush()
