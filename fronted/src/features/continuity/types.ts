import type { AssetVersion, VisualAsset } from "@/types/api";

export type ReviewReference = {
  reference_id: string;
  role: string;
  resource_id: string;
  content_sha256: string;
  asset_id: string | null;
  asset_type: string | null;
};

export type ComparisonReview = {
  record: {
    sequence: number;
    recorded_by: string;
    recorded_at: string;
    review: {
      result_id: string;
      conclusion: string;
      summary: string;
      risk: string;
      suggested_action: string;
      target: {
        candidate_id: string;
        asset_version_id: string;
        shot_id: string;
        storyboard_id: string;
      };
      confidence: {
        level: string;
        uncertainty: { reason: string; detail: string }[];
      };
      issues: {
        issue_id: string;
        category: string;
        risk: string;
        summary: string;
        evidence_ids: string[];
        suggested_action: string;
      }[];
      evidence: {
        evidence_id: string;
        reference_id: string;
        basis: string;
        region: { x: number; y: number; width: number; height: number } | null;
      }[];
      versions: {
        id: string;
        kind: string;
        name: string;
        version: string;
        digest: string;
      }[];
      rerun: {
        previous_review_id: string;
        previous_sha256: string;
        reason: string;
      } | null;
    };
  };
  references: {
    reference: ReviewReference;
    name: string;
    image_path: string | null;
    status: string | null;
    unavailable_code: string | null;
  }[];
};

export type ComparisonImage = {
  candidate_id: string | null;
  asset_id: string;
  asset_name: string;
  version: AssetVersion;
  round: number;
  revision_id: string | null;
  image_path: string;
};

export type HumanReview = {
  id: string;
  can_approve: boolean;
  request_sha256: string;
  status: "WAITING_REVIEW" | "DECIDED" | "RESUMED" | "BLOCKED";
  blocking_code: string | null;
  requested_by: string;
  created_at: string;
  snapshot: {
    version_id: string;
    run_id: string | null;
    thread_id: string;
    object_id: string;
    input_sha256: string;
    content_sha256: string;
  };
  decision: {
    id: string;
    actor_id: string;
    created_at: string;
    decision_json: {
      version_id: string;
      request_sha256: string;
      decision: "APPROVE" | "REJECT";
      comment: string;
    };
  } | null;
  resume: {
    id: string;
    created_at: string;
    result_json: { status?: string };
  } | null;
};

export type ContinuityComparison = {
  project_id: string;
  asset: Omit<VisualAsset, "asset_type"> & { asset_type: string };
  version: AssetVersion;
  candidate: {
    id: string;
    job_id: string;
    source: string;
    media_check: {
      status: string;
      error_code: string | null;
      references_valid: boolean;
      image: { width: number; height: number; media_type: string } | null;
    } | null;
  } | null;
  image_path: string;
  latest_review_id: string | null;
  reviews: ComparisonReview[];
  next_offset: number | null;
  images: ComparisonImage[];
  images_truncated: boolean;
  revisions: {
    record: {
      revision_id: string;
      parent_revision_id: string | null;
      root_candidate_id: string;
      round: number;
      max_rounds: number;
      status: string;
      job_id: string | null;
      target: { candidate_id: string; asset_version_id: string };
      request: { review_id: string; action: string; issue_ids: string[] };
      evidence_ids: string[];
      created_at: string;
    };
    feedback: {
      feedback_id: string;
      sequence: number;
      review_id: string;
      action: string;
      reason: string;
      status: string;
      source: string;
      human_review_request_id: string | null;
      created_at: string;
    }[];
  }[];
  human_reviews: HumanReview[];
  next_human_offset: number | null;
  actions: {
    owner: boolean;
    writable: boolean;
    blocker: { code: string; message: string } | null;
    can_submit: boolean;
    can_approve: boolean;
    can_reject: boolean;
    can_select: boolean;
  };
};

export const continuityLabels: Record<string, string> = {
  PASS: "审查通过",
  REVISION_REQUIRED: "需要修订",
  HUMAN_REVIEW: "需要人工处理",
  NONE: "无",
  LOW: "低",
  MEDIUM: "中",
  HIGH: "高",
  CRITICAL: "严重",
  UNKNOWN: "未知",
  CHARACTER_APPEARANCE: "角色外观",
  COSTUME: "服装",
  SCENE: "场景",
  PROP: "道具",
  STORYBOARD_INTENT: "分镜意图",
  ADJACENT_CONTINUITY: "相邻连续性",
  CANDIDATE: "候选图片",
  TARGET_SHOT: "目标镜头",
  STORYBOARD: "精确分镜",
  REFERENCE: "参考资产",
  ADJACENT_SHOT: "相邻镜头",
  VISUAL_BIBLE: "视觉规范",
  REVISE_PROMPT: "修订提示词",
  REGENERATE: "重新生成",
  REQUEST_EVIDENCE: "补充证据",
  INVESTIGATE: "追加取证",
  REVISE: "建议修订",
  READY_FOR_REVIEW: "待人工审核",
  INVESTIGATING: "取证中",
  REVISION_QUEUED: "修订已排队",
  QUEUED: "已排队",
  NEEDS_REVIEW: "已达修订上限，待人工处理",
  WAITING_REVIEW: "等待决定",
  DECIDED: "决定已保存，待恢复",
  RESUMED: "已恢复",
  BLOCKED: "处理受阻",
  APPROVE: "接受例外",
  REJECT: "拒绝例外",
  APPROVED: "已批准",
  REJECTED: "已拒绝",
  OCCLUDED: "遮挡",
  MISSING_REFERENCE: "缺少参考",
  LOW_VISIBILITY: "可见性不足",
  CONTRADICTION: "证据矛盾",
  BUDGET_EXHAUSTED: "预算耗尽",
  OTHER: "其他不确定性",
  PASSED: "通过",
  FAILED: "失败",
  MODEL: "模型决定",
  REVIEWER: "审查升级",
  LIMIT: "达到上限",
};
export const label = (value: string) => continuityLabels[value] ?? value;
export const roundLabel = (image: ComparisonImage) =>
  `${image.version.restored_from_version_id ? "恢复副本" : image.round === 0 ? "原始候选" : `修订第 ${image.round} 轮`} · v${image.version.version_no}`;
