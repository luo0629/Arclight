"""Freeze declared generation inputs and validated exact reference snapshots."""

from uuid import UUID

from sqlalchemy.orm import Session

from arclight.agents.contracts import AssetReference
from arclight.application.content_references import require_asset
from arclight.domain.asset_provenance import GenerationDeclaration
from arclight.domain.asset_versions import ImportedImage
from arclight.persistence.models import AssetVersion, VisualAsset


def capture_provenance(
    db: Session,
    project_id: UUID,
    image: ImportedImage,
    declaration: GenerationDeclaration | None,
) -> dict:
    snapshots = []
    if declaration:
        for reference in declaration.references:
            require_asset(
                db,
                project_id,
                AssetReference(**reference.model_dump(mode="json")),
                {"CHARACTER", "SCENE", "PROP", "STYLE", "IMAGE", "SHOT_IMAGE"},
            )
            version = db.get(AssetVersion, reference.asset_version_id)
            asset = db.get(VisualAsset, reference.asset_id)
            assert version is not None and asset is not None
            snapshots.append(
                {
                    **reference.model_dump(mode="json"),
                    "version_no": version.version_no,
                    "checksum": version.checksum,
                    "asset_name": asset.name,
                    "asset_type": asset.asset_type,
                }
            )
    return {
        "kind": "EXTERNAL_DECLARATION" if declaration else "MANUAL_IMPORT",
        "verification": "USER_DECLARED" if declaration else "NOT_APPLICABLE",
        "generation": declaration.model_dump(mode="json", exclude={"references"})
        if declaration
        else None,
        "width": image.width,
        "height": image.height,
        "references": snapshots,
    }
