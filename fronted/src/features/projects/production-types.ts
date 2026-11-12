import type { RenderDelivery } from "@/features/rendering/types";

export type ProductionPage<T> = {
  items: T[];
  total: number;
  limit: number;
  offset: number;
};

export type ProductionTask = {
  id: string;
  family: "WORKFLOW" | "GENERATION" | "RENDER";
  project_id: string;
  episode_id: string | null;
  episode_no: number | null;
  episode_title: string | null;
  task_type: "TEXT" | "IMAGE" | "AUDIO" | "VIDEO" | "RENDER" | "OTHER";
  workflow_type: string | null;
  status: string;
  error_code: string | null;
  created_at: string;
  updated_at: string;
  detail_url: string;
};

export type ProductionReview = {
  id: string;
  source: "HUMAN_REVIEW" | "WORKFLOW" | "SCRIPT" | "STORYBOARD" | "OUTLINE";
  project_id: string;
  episode_id: string | null;
  episode_no: number | null;
  episode_title: string | null;
  kind: string;
  object_id: string;
  version_id: string | null;
  run_id: string | null;
  asset_id: string | null;
  status: string;
  blocking_code: string | null;
  created_at: string;
  detail_url: string;
};

export type ProductionDelivery = RenderDelivery & {
  episode_no: number;
  episode_title: string;
  detail_url: string;
};

export const PRODUCTION_PAGE_SIZE = 20;
