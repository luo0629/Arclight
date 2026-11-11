import type { CostTotals } from "@/features/projects/components/project-costs";

export type MediaStatus =
  | "CREATED"
  | "SUBMITTING"
  | "SUBMITTED"
  | "RUNNING"
  | "SUCCEEDED"
  | "FAILED"
  | "CANCEL_REQUESTED"
  | "CANCELLED"
  | "RETRY_WAIT"
  | "RESULT_UNKNOWN"
  | "RECONCILING"
  | "MANUAL_REVIEW";

export type MediaJob = {
  id: string;
  project_id: string;
  episode_id: string | null;
  target_type: string;
  target_asset_id: string | null;
  input_manifest_id: string;
  task_type: string;
  provider: string;
  model: string;
  status: MediaStatus;
  status_label: string;
  revision: number;
  attempt_count: number;
  cancel_requested_at: string | null;
  created_at: string;
  updated_at: string;
};

/** Explicit public fields; snapshots, dispatch receipts and diagnostics stay out. */
export type MediaAttempt = {
  id: string;
  job_id: string;
  attempt_no: number;
  input_manifest_id: string;
  provider: string;
  model: string;
  status: MediaStatus;
  error_code: string | null;
  error_category: string | null;
  latency_ms: number | null;
  submitted_at: string;
  finished_at: string | null;
  batchSize: number | null;
  outputHandles: string[];
  isContinuityRevision: boolean;
};

export type MediaLane = {
  job_id: string;
  lane: string;
  origin: string;
  policy_version: number;
};
export type MediaQueueLane = {
  lane: "INTERACTIVE" | "BATCH";
  ready: number;
  backoff: number;
  waiting_permit: number;
  executing: number;
  unknown: number;
  terminal: number;
  dead_letter: number;
  oldest_waiting_at: string | null;
  oldest_wait_seconds: number | null;
  wait_seconds: { p50: number | null; p95: number | null };
  blocked_reasons: Record<string, number>;
};
export type MediaQueue = {
  observed_at: string;
  scheduler_heartbeat_at: string | null;
  scheduler_stale: boolean;
  count_unit: "business_jobs";
  policy_version: number;
  lanes: MediaQueueLane[];
};
export type MediaSchedule = {
  deadline_at: string;
  next_attempt_at: string;
  blocked_code: string | null;
  policy: {
    max_attempts?: number;
    max_elapsed_seconds?: number;
    retry_base_seconds?: number;
    retry_max_seconds?: number;
  };
};
export type MediaDeadLetter = {
  id: string;
  attempt_id: string | null;
  reason: string;
  error_code: string | null;
  category: string;
};
export type MediaCall = {
  id: string;
  job_id: string;
  attempt_id: string;
  kind: string;
  started_at: string;
  completed_at: string | null;
  released_at: string | null;
  outcome: string | null;
};
export type MediaCostEntry = {
  id: string;
  task_id: string;
  provider: string;
  model: string;
  attempt_no: number;
  attempt_id: string | null;
  call_id: string | null;
  input_manifest_id: string | null;
  trace_id: string | null;
  actual_cost: string | null;
  estimated_cost: string | null;
  pending_estimate: string;
  cost_is_estimate: boolean;
};
export type MediaCostEntries = { items: MediaCostEntry[]; total: number };
export type MediaDetail = {
  job: MediaJob;
  attempts: MediaAttempt[];
  latestAttempt: MediaAttempt | null;
  batchSize: number | null;
  schedule: MediaSchedule | null;
  deadLetter: MediaDeadLetter | null;
  lane: MediaLane | null;
  calls: MediaCall[];
  costs: CostTotals | null;
  costEntries: MediaCostEntries | null;
  errors: Record<string, string>;
};

export type ResolvePayload = {
  evidence_reference: string;
  confirmed: true;
  receipt: {
    status: "RUNNING" | "SUCCEEDED" | "FAILED" | "CANCELLED" | "NOT_ACCEPTED";
    result?: { output_handles: string[] };
    error_code?: string;
    latency_ms?: number;
    cost?: string;
  };
};
export type MediaAction =
  "retry" | "cancel" | "reconcile" | "resolve" | "replay";
export type MediaActionPayload = ResolvePayload | { reason: string };

export const MEDIA_PAGE_SIZE = 20;
export const MEDIA_COST_PAGE_SIZE = 10;
export const MEDIA_UNKNOWN_STATUSES: MediaStatus[] = [
  "RESULT_UNKNOWN",
  "RECONCILING",
  "MANUAL_REVIEW",
];
export const MEDIA_TERMINAL_STATUSES: MediaStatus[] = [
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
];
