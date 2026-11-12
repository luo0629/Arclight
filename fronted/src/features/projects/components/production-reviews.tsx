"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  EmptyState,
  LoadingState,
  Notice,
  PagePanel,
  StatusBadge,
} from "@/components/ui-kit";
import { productionReviewPath } from "@/lib/routes";
import { useProject } from "../project-context";
import useProductionList from "../hooks/use-production-list";
import type { ProductionReview } from "../production-types";

const kinds: Record<string, string> = {
  SCRIPT: "剧本",
  STORYBOARD: "分镜",
  OUTLINE: "分集大纲",
  REFERENCE: "美术参考提案",
  VISUAL_SPEC: "视觉规范提案",
  EXCEPTIONAL_SHOT: "画面例外",
  EXPORT: "导出审核",
  DELIVERY: "成片交付",
};
const blockers: Record<string, string> = {
  PROJECT_ARCHIVED: "项目已归档，仅供查阅",
  EPISODE_ARCHIVED: "剧集已归档，仅供查阅",
  REVIEW_INPUT_CHANGED: "审核输入已有变化，请在原工作区核对",
  REVIEW_SCOPE_INVALID: "审核范围需要核对",
  REVIEW_HANDLER_UNAVAILABLE: "原审核入口暂不可用",
  NOT_FOUND: "原审核对象暂不可访问",
  REVIEW_UNAVAILABLE: "审核当前受阻，请在原工作区核对",
};

export default function ProductionReviews() {
  const { project, owner, writable } = useProject();
  const rows = useProductionList<ProductionReview>({
    projectId: project.id,
    resource: "pending-reviews",
  });
  return (
    <PagePanel
      title="待审队列"
      description="汇总项目待审版本、工作流与人工处理请求。进入对应工作区核对精确版本后处理，批准和恢复仍分别记录。"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          共 {rows.total} 项待处理 · 按创建时间从新到旧
        </p>
        <Button
          variant="outline"
          disabled={rows.loading}
          onClick={rows.refresh}
        >
          刷新待审队列
        </Button>
      </div>
      {(!owner || !writable) && (
        <Notice>
          当前可查阅待审记录；处理操作由原工作区核对项目权限与状态。
        </Notice>
      )}
      {rows.error && <Notice tone="error">{rows.error}</Notice>}
      {rows.loading && <LoadingState>正在读取待审队列…</LoadingState>}
      {!rows.loading && !rows.error && rows.items.length === 0 && (
        <EmptyState>
          <h3>暂无待处理审核</h3>
          <p>新的待审版本或人工处理请求会出现在这里。</p>
        </EmptyState>
      )}
      <div className="flex min-w-0 flex-col gap-3" aria-label="项目待审列表">
        {rows.items.map((row) => {
          const href = productionReviewPath(row);
          return (
            <article
              key={`${row.source}:${row.id}`}
              className="flex min-w-0 flex-wrap items-start justify-between gap-4 rounded-xl border bg-card p-4"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <h3 className="break-words text-base">
                  {kinds[row.kind] ?? "待审版本"}
                </h3>
                <p className="break-words text-sm text-muted-foreground">
                  {row.episode_no === null
                    ? "项目共用"
                    : `第 ${row.episode_no} 集 · ${row.episode_title ?? ""}`}
                </p>
                <div className="flex flex-wrap gap-2">
                  <StatusBadge status={row.status} />
                  <Badge variant="outline">
                    {row.source === "HUMAN_REVIEW"
                      ? "人工处理请求"
                      : "版本审核与确认"}
                  </Badge>
                </div>
                <p className="break-all text-xs text-muted-foreground">
                  精确对象：{row.object_id}
                </p>
                {row.version_id && (
                  <p className="break-all text-xs text-muted-foreground">
                    精确版本：{row.version_id}
                  </p>
                )}
                <time
                  className="text-xs text-muted-foreground"
                  dateTime={row.created_at}
                >
                  {new Date(row.created_at).toLocaleString("zh-CN")}
                </time>
                {row.blocking_code && (
                  <p className="text-sm text-muted-foreground">
                    {blockers[row.blocking_code] ?? blockers.REVIEW_UNAVAILABLE}
                  </p>
                )}
              </div>
              {href ? (
                <Button variant="outline" asChild>
                  <Link href={href}>查看精确审核</Link>
                </Button>
              ) : (
                <p className="text-sm text-muted-foreground">
                  该记录暂时没有可用入口，请刷新后重试。
                </p>
              )}
            </article>
          );
        })}
      </div>
      <nav
        aria-label="待审队列分页"
        className="flex flex-wrap items-center justify-between gap-3"
      >
        <Button
          variant="outline"
          disabled={rows.loading || rows.offset === 0}
          onClick={() => rows.setOffset(rows.offset - rows.limit)}
        >
          上一页待审
        </Button>
        <span className="text-sm text-muted-foreground">
          第 {Math.floor(rows.offset / rows.limit) + 1} 页 · 每页 {rows.limit}{" "}
          项
        </span>
        <Button
          variant="outline"
          disabled={rows.loading || rows.offset + rows.limit >= rows.total}
          onClick={() => rows.setOffset(rows.offset + rows.limit)}
        >
          下一页待审
        </Button>
      </nav>
    </PagePanel>
  );
}
