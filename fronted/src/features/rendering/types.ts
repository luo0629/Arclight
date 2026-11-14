export type RenderProfile = "PREVIEW" | "FORMAL";

export type CropRect = { x: number; y: number; width: number; height: number };
export type ClipVisual = {
  crop: CropRect;
  motion: { kind: "STATIC" | "LINEAR"; end_crop: CropRect | null };
  transition: { kind: "CUT" | "FADE"; duration_ms: number };
};
export type ClipAudio = {
  purpose_role: "BACKGROUND" | "AMBIENCE" | "TRANSITION";
  gain_milli: number;
  fade_in_ms: number;
  fade_out_ms: number;
  envelope: { offset_ms: number; gain_milli: number }[];
};
export type TimelineClip = {
  id: string;
  ordinal: number;
  source_asset_version_id: string;
  source_sha256: string;
  start_ms: number;
  duration_ms: number;
  source_start_ms: number;
  shot_id: string | null;
  script_version_id: string | null;
  line_id: string | null;
  audio_version_id: string | null;
  review_id: string | null;
  review_storyboard_id: string | null;
  review_shot_id: string | null;
  visual?: ClipVisual | null;
  audio?: ClipAudio | null;
};
export type TimelineSnapshot = {
  schema_version: 1 | 2;
  episode_id: string;
  storyboard_id: string;
  default_duration_ms: number;
  duration_ms: number;
  output_profile: Record<string, unknown>;
  tracks: {
    id: string;
    kind: "VIDEO" | "VOICE" | "SFX" | "MUSIC" | "SUBTITLE";
    ordinal: number;
    enabled?: boolean;
    clips: TimelineClip[];
  }[];
};
export type Timeline = {
  id: string;
  asset_id: string;
  version_no: number;
  status: string;
  stale: boolean;
  content_sha256: string;
  dependency_manifest_id: string;
  created_at: string;
  based_on_timeline_id: string | null;
  snapshot: TimelineSnapshot;
};
export type ShotSelection = { shot_id: string; asset_version_id: string };
export type TimelineCreate = {
  creation_key: string;
  storyboard_id: string;
  default_duration_ms: number;
  selected_shots: ShotSelection[];
};
export type TimelinePreparation = {
  storyboard_id: string;
  selected_shots: ShotSelection[];
  shots: {
    shot_id: string;
    shot_no: number;
    description: string;
    options: {
      asset_id: string;
      asset_version_id: string;
      version_no: number;
      description: string;
    }[];
  }[];
  blocking_issues: string[];
};
export type RenderFileReference = {
  file_id: string;
  sha256: string;
  byte_size: number;
  media_type: string;
  source_note: string;
  rights_note: string;
  rights_confirmed: boolean;
};
export type RenderSource = {
  asset_id: string;
  asset_version_id: string;
  asset_type: string;
  sha256: string;
  file?: RenderFileReference | null;
};
export type RenderOutput = {
  name: RenderProfile;
  width: number;
  height: number;
  fps: number;
  container: "mp4";
  video_codec: string;
  video_encoder: string;
  video_bitrate: number;
  pixel_format: string;
  audio_codec: string;
  audio_bitrate: number;
  audio_sample_rate: number;
  audio_channels: number;
};
export type RenderSoftware = {
  renderer_version: string;
  pyav_version: string;
  ffmpeg_libraries: Record<string, string>;
  caption_libraries?: Record<string, string>;
};
export type LineAudioDecision = {
  script_version_id: string;
  line_id: string;
  mode: "SILENT" | "SUBTITLE_ONLY";
  reason: string;
};
export type LineAudioPreparation = {
  script_version_id: string;
  voiced_count: number;
  lines: {
    line_id: string;
    text: string;
    subtitle_available: boolean;
  }[];
};
export type RenderManifest = {
  id: string;
  project_id: string;
  created_by: string;
  content_sha256: string;
  snapshot: {
    schema_version: 1 | 2;
    timeline_id: string;
    timeline_sha256: string;
    dependency_manifest_id: string;
    dependency_manifest_sha256: string;
    timeline: TimelineSnapshot;
    visuals: Record<string, unknown>[];
    sources: RenderSource[];
    output: RenderOutput;
    subtitle_style: Record<string, unknown>;
    font: RenderFileReference | null;
    software: RenderSoftware;
    created_at: string;
    line_audio_decisions?: LineAudioDecision[];
  };
};
export type RenderAdmission = {
  manifest_id: string;
  manifest_sha256: string;
  profile: RenderProfile;
  review_request_id: string | null;
  decision_id: string | null;
  checked_at: string;
  admitted: true;
  requires_recheck: true;
  is_delivery: false;
};
export type RenderJob = {
  id: string;
  manifest_id: string;
  manifest_sha256: string;
  retry_of_id: string | null;
  profile: RenderProfile;
  status: string;
  revision: number;
  progress: number;
  phase: string;
  attempt_count: number;
  cancel_requested_at: string | null;
  output_file_id: string | null;
  output_asset_version_id: string | null;
  artifact_id: string | null;
  error_code: string | null;
  cleanup_status: string;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  is_delivery: boolean;
  report_id: string | null;
  delivery_phase: string | null;
};
export type RenderInspection = {
  passed: boolean;
  error_code: string | null;
  output_sha256: string;
  byte_size: number;
  [key: string]: unknown;
};
export type RenderAttempt = {
  attempt_no: number;
  status: string;
  progress: number;
  phase: string;
  error_code: string | null;
  cleanup_status: string;
  cleanup_error_code: string | null;
  created_at: string;
  finished_at: string | null;
  inspection: RenderInspection | null;
};
export type DeliveryAcceptance = {
  phase:
    | "LEGACY"
    | "PREVIEW"
    | "DELIVERY_QA"
    | "COMPLETED"
    | "COMPLETED_WITH_WARNINGS";
  is_delivery: boolean;
  warnings_confirmed: boolean;
  review_id: string | null;
  reviewed_by_user_id: string | null;
  blocking_issues: string[];
};
export type RenderDelivery = {
  id: string;
  project_id: string;
  episode_id: string;
  job_id: string;
  asset_id: string;
  asset_version_id: string;
  version_no: number;
  file_id: string;
  sha256: string;
  byte_size: number;
  duration_ms: number;
  profile: RenderProfile;
  manifest_id: string;
  manifest_sha256: string;
  timeline_id: string;
  dependency_manifest_id: string;
  inspection_id: string;
  report_id: string | null;
  report_file_id: string | null;
  storage_backend: "LOCAL" | "S3";
  stale: boolean;
  is_delivery: boolean;
  acceptance: DeliveryAcceptance;
  created_at: string;
};
export type RenderDeliveryDetail = RenderDelivery & {
  timeline_sha256: string;
  timeline_dependency_manifest_id: string;
  timeline_dependency_manifest_sha256: string;
  sources: RenderSource[];
  font: RenderFileReference | null;
  software: RenderSoftware;
  output: RenderOutput;
};
export type RenderWarning = {
  id: string;
  code: "EXPLICIT_SILENCE" | "SUBTITLE_ONLY" | "MEDIA_EXCEPTION";
  source_version_id: string;
  line_id: string | null;
  reason: string;
  human_review_request_id: string | null;
  human_review_decision_id: string | null;
  human_review_request_sha256: string | null;
};
export type MachineReport = {
  schema_version: "render-report/1";
  checker: "arclight-delivery-v1";
  render_status: "passed";
  job_id: string;
  attempt_id: string;
  artifact_id: string;
  asset_version_id: string;
  manifest_id: string;
  manifest_sha256: string;
  timeline_id: string;
  timeline_sha256: string;
  timeline_version: number;
  inspection_id: string;
  input_sha256: string;
  output_file_id: string;
  output_sha256: string;
  byte_size: number;
  duration_ms: number;
  profile: "FORMAL";
  sources: RenderSource[];
  font: RenderFileReference | null;
  software: RenderSoftware;
  output: RenderOutput;
  line_audio_decisions: LineAudioDecision[];
  checks: {
    video_decodable: boolean;
    audio_stream_present: boolean;
    subtitle_range_valid: boolean;
    missing_required_shots: number;
    missing_audio_lines: number;
    stale_dependencies: number;
    technical: RenderInspection;
  };
  warnings: RenderWarning[];
};
export type RenderDeliveryReport = {
  report_id: string;
  report_file_id: string;
  report_sha256: string;
  report: {
    schema_version: 1;
    manifest_id: string;
    manifest_sha256: string;
    timeline_id: string;
    output_file_id: string;
    output_sha256: string;
    details: unknown;
  };
  acceptance: DeliveryAcceptance;
};
export type ReviewDecision = {
  id: string;
  created_at: string;
  updated_at: string;
  request_id: string;
  actor_id: string;
  decision_json: {
    version_id: string;
    request_sha256: string;
    decision: "APPROVE" | "REJECT";
    comment: string;
    impact_digest: string | null;
  };
};
export type ReviewResume = {
  id: string;
  created_at: string;
  updated_at: string;
  request_id: string;
  decision_id: string;
  actor_id: string;
  result_json: Record<string, unknown>;
};
export type HumanReview = {
  id: string;
  created_at: string;
  updated_at: string;
  project_id: string;
  requested_by: string;
  request_sha256: string;
  snapshot: {
    schema_version: "human-review/1";
    project_id: string;
    episode_id: string | null;
    run_id: string | null;
    loop_id: string | null;
    thread_id: string;
    kind:
      | "SCRIPT"
      | "STORYBOARD"
      | "REFERENCE"
      | "VISUAL_SPEC"
      | "EXCEPTIONAL_SHOT"
      | "EXPORT"
      | "DELIVERY";
    purpose: "PRODUCTION" | "PREVIEW";
    object_id: string;
    version_id: string;
    content_sha256: string;
    input_sha256: string;
    source_version_ids: string[];
    expected_revision: number;
  };
  status: "WAITING_REVIEW" | "DECIDED" | "RESUMED" | "BLOCKED";
  blocking_code: string | null;
  decision: ReviewDecision | null;
  resume: ReviewResume | null;
};
