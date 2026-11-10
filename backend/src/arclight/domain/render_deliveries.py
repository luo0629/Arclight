"""Public historical render identities, without private storage coordinates."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from arclight.domain.render_manifests import (
    RenderContract,
    RenderFileReference,
    RenderOutputProfile,
    RenderProfile,
    RenderSoftware,
    Sha256,
)


class RenderDeliveryRead(RenderContract):
    id: UUID
    project_id: UUID
    episode_id: UUID
    job_id: UUID
    asset_id: UUID
    asset_version_id: UUID
    version_no: int
    file_id: UUID
    sha256: Sha256
    byte_size: int
    duration_ms: int
    profile: RenderProfile
    manifest_id: UUID
    manifest_sha256: Sha256
    timeline_id: UUID
    dependency_manifest_id: UUID
    inspection_id: UUID
    report_id: UUID | None
    report_file_id: UUID | None
    storage_backend: Literal["LOCAL", "S3"]
    stale: bool
    is_delivery: Literal[False] = False
    created_at: datetime


class RenderDeliverySource(RenderContract):
    asset_id: UUID
    asset_version_id: UUID
    asset_type: str
    sha256: Sha256
    file: RenderFileReference | None


class RenderDeliveryDetail(RenderDeliveryRead):
    timeline_sha256: Sha256
    timeline_dependency_manifest_id: UUID
    timeline_dependency_manifest_sha256: Sha256
    sources: list[RenderDeliverySource]
    font: RenderFileReference | None
    software: RenderSoftware
    output: RenderOutputProfile
