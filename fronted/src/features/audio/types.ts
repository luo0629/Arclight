import type { AssetVersion } from "@/types/api";

export type AudioSnapshot = {
  id: string;
  episode_id: string;
  script_version_id: string;
  script_sha256: string;
  line_count: number;
  created_at: string;
};
export type AudioLine = {
  line_id: string;
  scene_id: string;
  scene_no: number;
  sequence_no: number;
  speaker: string;
  text: string;
  line_type: "dialogue" | "narration";
  emotion: string;
};
export type AudioVoice = {
  id: string;
  project_id: string;
  speaker: string;
  character_asset_id: string | null;
  version_no: number;
  provider: string;
  model: string;
  model_config_version: string;
  parameters: Record<string, unknown>;
  output_spec: Record<string, unknown>;
  provider_configuration: Record<string, unknown>;
  created_at: string;
};
export type SpeechConfiguration = {
  configured: boolean;
  enabled: boolean;
  message: string;
  provider?: string;
  model?: string;
  supported_parameters?: string[];
};
export type AudioBinding = {
  id: string;
  snapshot_id: string;
  line_id: string;
  asset_version_id: string;
  dependency_manifest_id: string;
  file_id: string;
  duration_ms: number;
};
export type AudioHistory = {
  binding: AudioBinding;
  version: AssetVersion;
  tts_job_id: string | null;
  source_audio_version_id: string | null;
};
export type AudioTask = {
  job_id: string;
  snapshot_id: string;
  line_id: string;
  voice_version_id: string;
  target_asset_id: string;
  status: string;
  revision: number;
  error_code: string | null;
  asset_version_id: string | null;
  source_audio_version_id: string | null;
  created_at: string;
};
export type AudioAsset = {
  id: string;
  project_id: string;
  asset_type: string;
  status: string;
  revision: number;
  lock_version: number;
  current_version_id: string | null;
};
export type AudioValidation = {
  media_type: string;
  codec: string;
  duration_ms: number;
  sample_rate: number;
  channels: number;
  silence: {
    schema_version: string;
    threshold_dbfs: number;
    threshold_pcm16: number;
    silent: boolean;
    peak: number;
    rms: number;
    silent_sample_fraction: number;
  };
};
export type AudioImportResult = {
  snapshot_id: string;
  line_id: string;
  target_asset_id: string;
  asset_version_id: string;
  file_id: string;
  source_file_id: string;
  dependency_manifest_id: string;
  binding_id: string;
  validation: AudioValidation;
  asset_revision: number;
  created_by: string;
  created_at: string;
};
export type AudioResult = {
  job_id: string;
  attempt_id: string;
  source_file_id: string;
  voice_version_id: string;
  binding: AudioBinding;
  validation: AudioValidation;
  asset_revision: number;
};
export type AudioFile = {
  id: string;
  project_id: string;
  asset_version_id: string | null;
  checksum: string;
  byte_size: number;
  media_type: string;
  original_name: string;
  source_note: string;
  rights_note: string;
  rights_confirmed: boolean;
};
export type RightsDeclaration = {
  source_note: string;
  rights_note: string;
  rights_confirmed: true;
};
