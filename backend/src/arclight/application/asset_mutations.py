"""Shared revision checks for asset mutations in HTTP and media services."""

from sqlalchemy import update
from sqlalchemy.orm import Session

from arclight.core.errors import DomainError
from arclight.persistence.models import VisualAsset


def change_asset(db: Session, row: VisualAsset, expected: int, **values) -> None:
    changed = db.scalar(
        update(VisualAsset)
        .where(VisualAsset.id == row.id, VisualAsset.revision == expected)
        .values(**values, revision=VisualAsset.revision + 1)
        .returning(VisualAsset.id)
        .execution_options(synchronize_session=False)
    )
    if changed is None:
        raise DomainError("VERSION_CONFLICT", "资产已被修改，请保留草稿并重新载入", 409)
    db.refresh(row)
