"""Approved-script audio preparation contracts; no provider execution."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from arclight.agents.contracts import Dialogue


class AudioContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AudioSnapshotCreate(AudioContract):
    creation_key: UUID
    script_version_id: UUID


class AudioLine(Dialogue):
    scene_id: str
    scene_no: int
    sequence_no: int


class AudioSnapshotRead(AudioContract):
    id: UUID
    episode_id: UUID
    script_version_id: UUID
    script_sha256: str
    line_count: int
    created_at: datetime


class AudioInputCreate(AudioContract):
    creation_key: UUID
    target_asset_id: UUID


class AudioInputRead(AudioContract):
    snapshot_id: UUID
    line_id: str
    dependency_manifest_id: UUID


class AudioBindingCreate(AudioContract):
    asset_version_id: UUID


class AudioBindingRead(AudioContract):
    id: UUID
    snapshot_id: UUID
    line_id: str
    asset_version_id: UUID
    dependency_manifest_id: UUID
    file_id: UUID
    duration_ms: int = Field(gt=0)
    created_at: datetime
