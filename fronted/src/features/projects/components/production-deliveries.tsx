"use client";

import Link from "next/link";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  EmptyState,
  LoadingState,
  Notice,
  PagePanel,
} from "@/components/ui-kit";
import { productionDeliveryPath } from "@/lib/routes";
import {
  bytes,
  date,
  duration,
  renderLabel,
} from "@/features/rendering/components/render-format";
import { useProject } from "../project-context";
import useProductionList from "../hooks/use-production-list";
import type { ProductionDelivery } from "../production-types";

export default function ProductionDeliveries() {
  const { project } = useProject();
  const rows = useProductionList<ProductionDelivery>({
    projectId: project.id,
    resource: "deliveries",
  });
  return (
    <PagePanel
      title="成片版本"
      description="查看全项目真实渲染产物及正式交付状态。每次重渲染保留历史版本，打开精确成片可预览、下载并核对机器报告。"
    >
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          共 {rows.total} 个成片版本 · 按创建时间从新到旧
        </p>
        <Button
          variant="outline"
          disabled={rows.loading}
          onClick={rows.refresh}
        >
          刷新成片版本
        </Button>
      </div>
      {project.archived && <Notice>项目已归档，成片历史仍可查阅。</Notice>}
      {rows.error && <Notice tone="error">{rows.error}</Notice>}
      {rows.loading && <LoadingState>正在读取成片版本…</LoadingState>}
      {!rows.loading && !rows.error && rows.items.length === 0 && (
        <EmptyState>
          <h3>暂无成片版本</h3>
          <p>完成剧集渲染后，这里将显示真实产物及交付状态。</p>
        </EmptyState>
      )}
      <div className="flex min-w-0 flex-col gap-3" aria-label="项目成片列表">
        {rows.items.map((row) => {
          const href = productionDeliveryPath(row);
          return (
            <article
              key={row.id}
              className="flex min-w-0 flex-wrap items-start justify-between gap-4 rounded-xl border bg-card p-4"
            >
              <div className="flex min-w-0 flex-1 flex-col gap-2">
                <h3 className="break-words text-base">
                  第 {row.episode_no} 集 · {row.episode_title}
                </h3>
                <p className="text-sm">
                  成片 v{row.version_no} · {renderLabel(row.profile)} ·{" "}
                  {duration(row.duration_ms)} · {bytes(row.byte_size)}
                </p>
                <div className="flex flex-wrap gap-2">
                  <Badge variant={row.is_delivery ? "default" : "secondary"}>
                    {row.is_delivery ? "正式交付" : "渲染产物"}
                  </Badge>
                  <Badge variant="outline">
                    {renderLabel(row.acceptance.phase)}
                  </Badge>
                  {row.stale && <Badge variant="outline">素材已失效</Badge>}
                </div>
                <p className="break-all text-xs text-muted-foreground">
                  精确成片：{row.id}
                </p>
                <p className="text-xs text-muted-foreground">
                  创建于 {date(row.created_at)}
                </p>
              </div>
              {href && (
                <Button variant="outline" asChild>
                  <Link href={href}>查看精确成片</Link>
                </Button>
              )}
            </article>
          );
        })}
      </div>
      <nav
        aria-label="成片版本分页"
        className="flex flex-wrap items-center justify-between gap-3"
      >
        <Button
          variant="outline"
          disabled={rows.loading || rows.offset === 0}
          onClick={() => rows.setOffset(rows.offset - rows.limit)}
        >
          上一页成片
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
          下一页成片
        </Button>
      </nav>
    </PagePanel>
  );
}
