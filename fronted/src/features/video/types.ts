import type { AssetVersion } from "@/types/api";

export type CheckedVideo = {
  checksum: string;
  media_type: "video/mp4";
  byte_size: number;
  width: number;
  height: number;
  duration_ms: number;
  frame_rate: number;
  codec: string;
  decoded_frames: number;
  has_audio: boolean;
  full_decode_verified: true;
};
export type VideoSource = {
  schema_version: "shot-video-source/1";
  artifact_id: string | null;
  source_file_id: string;
  generation_manifest_id: string;
  generation_manifest_sha256: string;
  storyboard_id: string;
  shot_id: string;
  video_images: Record<string, unknown>[];
  job_id: string;
  attempt_id: string;
};
export type VideoCandidateSummary = {
  id: string;
  job_id: string;
  asset_id: string;
  version_id: string;
  version_no: number;
  revision: number;
  status: string;
  asset_revision: number;
  content_path: string | null;
  selected: boolean;
};
export type MouthRegion = {
  x: number;
  y: number;
  width: number;
  height: number;
};
export type LipSyncCue = {
  line_id: string;
  audio_version_id: string;
  start_ms: number;
  closed_frame_ms: number;
  open_frame_ms: number;
  mouth: MouthRegion;
};
export type LipSyncRequest = {
  creation_key: string;
  expected_revision: number;
  source_rights_note?: string;
  cues: LipSyncCue[];
};
export type LipSyncOptions = {
  candidate_id: string;
  requires_video_rights_confirmation: boolean;
  dialogue: {
    line_id: string;
    speaker: string;
    text: string;
    presentation: "visible" | "voice_over" | "off_screen" | "obscured";
    lipsync: "required" | "not_applicable";
    exemption_reason: string;
    sources: { audio_version_id: string; duration_ms: number; name: string }[];
  }[];
};
export type LipSyncReceipt = {
  engine: "local-mouth-pose/1";
  source_candidate_id: string;
  source_video_version_id: string;
  source_video_sha256: string;
  script_version_id: string;
  output_sha256: string;
  lines: (LipSyncCue & {
    audio_file_id: string;
    audio_sha256: string;
    duration_ms: number;
  })[];
};
export type VideoCandidate = {
  id: string;
  project_id: string;
  job_id: string;
  attempt_id: string;
  input_manifest_id: string;
  source: string;
  error_code?: string | null;
  source_file_id: string | null;
  replaces_candidate_id: string | null;
  asset_version_id: string | null;
  media_kind: "VIDEO";
  required_review: "VIDEO_TEMPORAL";
  asset_revision: number;
  version: AssetVersion | null;
  content_path: string | null;
  video_source: VideoSource | null;
  lip_sync_required?: boolean;
  lip_sync?: LipSyncReceipt | null;
  production: {
    provider?: string;
    model?: string;
    prompt?: string;
    negative_prompt?: string;
    model_config_version?: string;
    prompt_version?: string;
    parameters?: Record<string, unknown>;
    output_spec?: Record<string, unknown>;
    seed?: number | null;
    variation?: string;
  } | null;
  media_check: {
    schema_version: "candidate-video-check/1";
    status: "PASSED" | "FAILED";
    candidate_id: string;
    input_manifest_id: string;
    manifest_sha256: string;
    references_valid: boolean;
    error_code: string | null;
    video: CheckedVideo | null;
  } | null;
  admission: {
    schema_version: "video-review-summary/1";
    status: "ADMITTED" | "BLOCKED";
    blocked_code: string | null;
    report_id: string | null;
    conclusion: string | null;
    report_sha256: string | null;
    sampled_frame_count: number | null;
    coverage_start_ms: number | null;
    coverage_end_ms: number | null;
    limitations: string[];
    summary?: string | null;
    sampling_strategy?: string | null;
    samples?: { time_ms: number; frame_sha256: string; observation: string }[];
    issues?: {
      issue_id: string;
      time_ms: number;
      description: string;
      resolved: boolean;
    }[];
    review_run_id?: string | null;
    review_status?: string | null;
    review_error_code?: string | null;
    human_review_request_id?: string | null;
    revision_id?: string | null;
    revision_job_id?: string | null;
    revision_status?: string | null;
  } | null;
  actions: Record<
    | "can_submit"
    | "can_approve"
    | "can_reject"
    | "can_select"
    | "can_lock"
    | "can_unlock"
    | "can_replace"
    | "can_restore"
    | "can_start_review"
    | "can_decide_review"
    | "can_revise"
    | "can_lip_sync",
    boolean
  > | null;
};
export type VideoAsset = {
  id: string;
  project_id: string;
  revision: number;
  lock_version: number;
  status: string;
  current_version_id: string | null;
};
