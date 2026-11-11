export const selectClass =
  "h-10 w-full min-w-0 rounded-lg border border-input bg-background px-3 text-sm focus-visible:ring-2 focus-visible:ring-ring disabled:opacity-50";

const labels: Record<string, string> = {
  QUEUED: "排队中",
  RUNNING: "渲染中",
  CANCEL_REQUESTED: "正在取消",
  CANCELLED: "已取消",
  SUCCEEDED: "渲染成功",
  FAILED: "渲染失败",
  PENDING: "等待执行",
  PREPARING: "准备素材",
  ENCODING: "编码中",
  INSPECTING: "检查成片",
  REGISTERING: "登记版本",
  COMPLETED: "正式交付已通过",
  COMPLETED_WITH_WARNINGS: "正式交付已接受警告",
  DELIVERY_QA: "待交付确认",
  PREVIEW: "预览版",
  FORMAL: "正式版",
  LEGACY: "历史技术回执",
  NOT_REQUIRED: "无需清理",
  CLEANED: "已清理",
  SUCCEEDED_CLEANUP: "已清理",
  RETRY_PENDING: "等待重试",
  WAITING_REVIEW: "等待拥有者审核",
  DECIDED: "已保存决定，等待恢复",
  RESUMED: "已恢复",
  BLOCKED: "当前受阻",
  EXPLICIT_SILENCE: "已明确静音",
  SUBTITLE_ONLY: "仅保留字幕",
  MEDIA_EXCEPTION: "素材人工例外",
  VALIDATING: "核验输入",
  REVALIDATING: "复核素材",
  PERSISTING: "登记成片",
};
export function renderLabel(value: string | null | undefined) {
  return value ? (labels[value] ?? value) : "暂无记录";
}
export function cleanupLabel(value: string) {
  return (
    (
      {
        NOT_REQUIRED: "无需清理",
        CLEANED: "已清理",
        PENDING: "等待清理",
        FAILED: "清理失败",
      } as Record<string, string>
    )[value] ?? renderLabel(value)
  );
}
export function duration(value: number) {
  return `${(value / 1000).toLocaleString(undefined, { maximumFractionDigits: 3 })} 秒`;
}
export function bytes(value: number) {
  return `${(value / 1024 / 1024).toLocaleString(undefined, { maximumFractionDigits: 2 })} MB`;
}
export function date(value: string) {
  return new Date(value).toLocaleString();
}
