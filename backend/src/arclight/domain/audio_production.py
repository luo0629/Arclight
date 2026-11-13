"""Approved-script audio preparation contracts; no provider execution."""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from arclight.agents.contracts import Dialogue


class AudioContract(BaseModel):
    model_config = ConfigDict(extra="forbid", from_attributes=True)


class AudioAssetMetadata(AudioContract):
    audio_snapshot_id: UUID
    line_id: str


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


class AudioImportCreate(AudioContract):
    creation_key: UUID
    source_file_id: UUID
    target_asset_id: UUID | None = None
    expected_revision: int = Field(default=0, strict=True, ge=0)


class AudioSilenceRead(AudioContract):
    schema_version: str
    threshold_dbfs: int
    threshold_pcm16: int
    silent: bool
    peak: float = Field(ge=0, le=1)
    rms: float = Field(ge=0, le=1)
    silent_sample_fraction: float = Field(ge=0, le=1)


class AudioValidationRead(AudioContract):
    media_type: str
    codec: str
    duration_ms: int = Field(gt=0, le=600000)
    sample_rate: int = Field(ge=8000, le=48000)
    channels: int = Field(ge=1, le=2)
    silence: AudioSilenceRead


class AudioImportRead(AudioContract):
    snapshot_id: UUID
    line_id: str
    target_asset_id: UUID
    asset_revision: int
    asset_version_id: UUID
    file_id: UUID
    source_file_id: UUID
    dependency_manifest_id: UUID
    binding_id: UUID
    validation: AudioValidationRead
    created_by: str
    created_at: datetime
