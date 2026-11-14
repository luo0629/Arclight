/** Read-only presentation of FLOW's authoritative production-state/1 contract. */
export const productionStages = [
  { id: "script", label: "剧本", conditions: ["SCRIPT_APPROVED"] },
  { id: "assets", label: "角色／场景", conditions: ["ASSETS_USABLE"] },
  { id: "storyboard", label: "分镜", conditions: ["STORYBOARD_APPROVED"] },
  { id: "media", label: "画面", conditions: ["SHOTS_APPROVED"] },
  { id: "audio", label: "配音／字幕", conditions: [] },
  { id: "timeline", label: "时间轴", conditions: ["TIMELINE_READY"] },
  {
    id: "export",
    label: "导出",
    conditions: ["RENDER_READY", "DELIVERY_APPROVED", "WARNINGS_CONFIRMED"],
  },
] as const;
export type ProductionStage = (typeof productionStages)[number]["id"];
export type ProductionState = {
  schema_version: "production-state/1";
  project_id: string;
  episode_id: string;
  archived: boolean;
  phase: string;
  blocked_phase: string | null;
  script_version_id: string | null;
  storyboard_version_id: string | null;
  conditions: { condition: string; satisfied: boolean; issues: string[] }[];
  issues: string[];
  warnings: string[];
  warnings_confirmed: boolean;
  shot_count: number;
  approved_shot_count: number;
  shot_counts: Record<string, number>;
  task_counts: Record<string, number>;
  unbound_task_counts: Record<string, number>;
  task_issues: string[];
  has_partial_failure: boolean;
  unavailable_producers: string[];
  evidence_sha256: string;
};

const phaseLabels: Record<string, string> = {
  DRAFT: "尚未起稿",
  SCRIPTING: "剧本制作中",
  WAITING_SCRIPT_REVIEW: "剧本待审核",
  ASSET_PREPARATION: "角色／场景待准备",
  STORYBOARDING: "分镜制作中",
  WAITING_STORYBOARD_REVIEW: "分镜待审核",
  GENERATING_MEDIA: "画面待制作",
  MEDIA_QA: "画面待检查／审核",
  EDITING: "时间轴待准备",
  RENDERING: "导出待完成",
  DELIVERY_QA: "成片待验收",
  COMPLETED: "正式成片已完成",
  COMPLETED_WITH_WARNINGS: "正式成片已完成（例外已确认）",
  FAILED: "生产失败",
  CANCELLED: "生产已取消",
};
const issueLabels: Record<string, string> = {
  MISSING_SCRIPT: "尚无剧本",
  SCRIPT_NOT_APPROVED: "剧本尚未批准",
  MISSING_STORYBOARD: "尚无分镜",
  STORYBOARD_NOT_APPROVED: "分镜尚未批准",
  SOURCE_VERSION_MISMATCH: "来源版本已变化，需要重新核对",
  STALE_INPUT: "输入已失效，需要更新并重审",
  MISSING_ASSET: "缺少所引用的素材",
  ASSET_NOT_APPROVED: "精确素材版本尚未批准",
  INVALID_CONTENT: "内容尚未满足制作要求",
  MISSING_SHOTS: "尚无必需镜头",
  MISSING_MEDIA: "镜头尚缺画面",
  MEDIA_CHECK_PENDING: "媒体检查待完成",
  MEDIA_CHECK_FAILED: "媒体检查未通过",
  CONTINUITY_PENDING: "连续性审查待完成",
  CONTINUITY_REVISION: "连续性问题需要修订",
  HUMAN_REVIEW_REQUIRED: "需要人工审核",
  SELECTION_PENDING: "尚未确认选用",
  SELECTION_REJECTED: "选用已被拒绝",
  EXECUTION_FAILED: "任务失败，请在任务详情中处理",
  EXECUTION_CANCELLED: "任务已取消",
  RESULT_UNCERTAIN: "任务结果未确定，请先核验，避免重复提交",
  MISSING_TIMELINE: "尚无有效时间轴",
  MISSING_RENDER: "尚无有效正式渲染结果",
  DELIVERY_REVIEW_PENDING: "成片内容待审核",
  WARNINGS_NOT_CONFIRMED: "交付例外尚未确认",
  MISSING_VERSION_REGISTRY: "精确版本记录不完整",
  PRODUCER_NOT_CONNECTED: "此生产环节尚未接入",
};
export const phaseLabel = (phase: string) =>
  phaseLabels[phase] ?? "状态暂不可识别";
export const issueLabel = (issue: string) =>
  issueLabels[issue] ?? "有待核对的生产条件，请查看任务详情";
export function conditionMet(state: ProductionState, name: string) {
  return (
    state.conditions.find((item) => item.condition === name)?.satisfied === true
  );
}
export function stageIssues(state: ProductionState, stage: ProductionStage) {
  const names: readonly string[] = productionStages.find(
    (item) => item.id === stage,
  )!.conditions;
  return [
    ...new Set(
      state.conditions
        .filter((item) => names.includes(item.condition) && !item.satisfied)
        .flatMap((item) => item.issues),
    ),
  ];
}
export function stageLabel(state: ProductionState, stage: ProductionStage) {
  if (stage === "audio") return "按时间轴核对";
  const phase = state.blocked_phase ?? state.phase;
  if (
    (stage === "script" && phase === "WAITING_SCRIPT_REVIEW") ||
    (stage === "storyboard" && phase === "WAITING_STORYBOARD_REVIEW") ||
    (stage === "media" && phase === "MEDIA_QA") ||
    (stage === "export" && phase === "DELIVERY_QA")
  )
    return "待审核";
  if (stage === "assets" && !state.script_version_id) return "待确定引用";
  const names = productionStages.find((item) => item.id === stage)!.conditions;
  if (names.every((name) => conditionMet(state, name)))
    return stage === "assets"
      ? "当前引用可用"
      : stage === "export"
        ? "正式交付通过"
        : "条件已满足";
  return "待完成条件";
}
export const stageGuidance: Record<ProductionStage, string> = {
  script: "先保存剧本并提交审核；分镜生产需要已批准的精确剧本版本。",
  assets:
    "核对当前剧本和分镜引用的角色、场景及其批准版本。当前引用可用不代表项目内全部素材已制作。",
  storyboard: "分镜生产需要已批准剧本及可用的引用素材；新版本仍需单独审核。",
  media:
    "先批准分镜，再制作、检查、审核并选用各镜头画面。视频生产能力仍按实际已接入入口提供。",
  audio:
    "先在配音工作区逐句制作、试听、审核与选用声音。没有配音的台词须在渲染清单中逐句说明处理方式；已有素材数量不代表音频制作或审核全部完成。",
  timeline:
    "从已批准分镜和已审核选用的画面准备时间轴；失效输入需更新，修改保存为新版本。",
  export:
    "正式导出需有效时间轴、精确素材与声音决定，并通过导出审核。预览不代表正式交付；最终还需机器检查、内容审核及例外确认。",
};
