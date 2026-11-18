import type { VideoCandidateSummary } from "@/features/video/types";

export type Blocker = { code: string; message: string };
export type ProductionJob = {
  id: string;
  status: string;
  revision: number;
  attempt_count: number;
  maximum_cost: string | null;
  currency: string | null;
};
export type ProductionCandidate = {
  id: string;
  asset_id: string;
  version_id: string;
  version_no: number;
  revision: number;
  status: string;
  image_path: string;
  can_submit: boolean;
  can_approve: boolean;
  can_reject: boolean;
  blocker: Blocker | null;
};
export type GenerationRequest = {
  episode_id: string;
  target_type: "SHOT_IMAGE";
  task_type: "IMAGE";
  scheduling_mode: "BATCH";
  review_target: { storyboard_id: string; shot_id: string };
  spec: {
    provider: string;
    model: string;
    model_config_version: string;
    prompt: string;
    prompt_version: string;
    batch_size?: number;
    output_spec: { width: number; height: number; format: string };
  };
};
export type ProductionShot = {
  id: string;
  shot_no: number;
  description: string;
  job: ProductionJob | null;
  request: GenerationRequest | null;
  blocker: Blocker | null;
  candidates: ProductionCandidate[];
  candidates_truncated: boolean;
  video_candidates: VideoCandidateSummary[];
  video_candidates_truncated: boolean;
};
export type ProductionPage = {
  project_id: string;
  episode_id: string;
  storyboard_id: string;
  writable: boolean;
  owner: boolean;
  total: number;
  offset: number;
  next_offset: number | null;
  shots: ProductionShot[];
};
export const batchActions = {
  generate: "生成新镜头",
  retry: "只重试失败任务",
  submit: "提交候选待审",
  approve: "批准候选",
  reject: "拒绝候选",
};
export type BatchAction = keyof typeof batchActions;
export type ImageConfiguration = {
  provider: string;
  model: string;
  model_config_version: string;
  batch_size: number;
  output_spec: { width: number; height: number; format: string };
};
export type BatchItem = {
  shot_id: string;
  shot_no: number;
  key: string;
  request?: GenerationRequest;
  job?: ProductionJob;
  candidate?: ProductionCandidate;
  cost: string | null;
  phase: "ready" | "blocked" | "unknown" | "accepted" | "failed";
  attempted: boolean;
  result: string;
};
export type ShotBatch = {
  schema: 1;
  user_id: string;
  project_id: string;
  episode_id: string;
  storyboard_id: string;
  action: BatchAction;
  comment: string;
  items: BatchItem[];
};

export function budgetTotal(items: BatchItem[]): string | null {
  let sum = 0n;
  for (const item of items.filter((row) => row.phase !== "blocked")) {
    if (!item.cost || !/^\d+(\.\d{1,6})?$/.test(item.cost)) return null;
    const [whole, fraction = ""] = item.cost.split(".");
    sum += BigInt(whole) * 1000000n + BigInt(fraction.padEnd(6, "0"));
  }
  return `${sum / 1000000n}.${String(sum % 1000000n).padStart(6, "0")}`;
}
