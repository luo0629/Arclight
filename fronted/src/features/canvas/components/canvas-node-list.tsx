"use client";

import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui-kit";
import type { GraphNode } from "@/types/api";
import { nodeGroup, nodeLabel, selectionLabel } from "../nodes";

export default function CanvasNodeList({
  nodes,
  selectedId,
  onSelect,
  pagination,
}: {
  nodes: GraphNode[];
  selectedId?: string;
  onSelect: (node: GraphNode) => void;
  pagination: ReactNode;
}) {
  const [query, setQuery] = useState("");
  const [group, setGroup] = useState("all");
  const [page, setPage] = useState(0);
  const matching = nodes.filter(
    (node) =>
      (group === "all" || nodeGroup(node) === group) &&
      [
        node.title,
        nodeLabel(node),
        node.resource_id ?? "",
        node.description ?? "",
        node.version_no ? `版本 ${node.version_no}` : "",
      ].some((value) =>
        value.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()),
      ),
  );
  const last = Math.max(0, Math.ceil(matching.length / 30) - 1);
  const current = Math.min(page, last);
  return (
    <section
      aria-label="内容与媒体列表"
      className="version-list flex min-w-0 flex-col gap-3 rounded-xl border bg-card p-4"
    >
      <h2>内容与媒体版本</h2>
      <p className="text-sm text-muted-foreground">
        与画布使用相同的精确版本和真实状态。选择一项查看内容、来源与预览。
      </p>
      <div className="flex flex-wrap gap-3">
        <label className="flex min-w-0 flex-1 flex-col gap-1 text-sm">
          搜索名称或版本 ID
          <input
            className="h-10 min-w-0 rounded-lg border bg-background px-3"
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setPage(0);
            }}
          />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          内容类型
          <select
            aria-label="内容类型"
            className="h-10 rounded-lg border bg-background px-3"
            value={group}
            onChange={(e) => {
              setGroup(e.target.value);
              setPage(0);
            }}
          >
            {[
              ["all", "全部内容"],
              ["text", "文本与镜头"],
              ["assets", "资产与参考"],
              ["images", "候选图"],
              ["audio", "配音、音乐与音效"],
              ["timelines", "时间轴"],
              ["deliveries", "成片"],
            ].map(([value, label]) => (
              <option key={value} value={value}>
                {label}
              </option>
            ))}
          </select>
        </label>
      </div>
      <p role="status" className="text-xs text-muted-foreground">
        共 {matching.length} 项 · 第 {current + 1} / {last + 1} 页
      </p>
      {matching.slice(current * 30, current * 30 + 30).map((node) => (
        <Button
          key={node.id}
          variant="outline"
          size="lg"
          aria-pressed={selectedId === node.id}
          data-node-id={node.id}
          className={`version-row h-auto min-w-0 justify-start gap-3 py-4 text-left whitespace-normal ${selectedId === node.id ? "active ring-2 ring-ring" : ""}`}
          onClick={() => onSelect(node)}
        >
          <span
            aria-hidden="true"
            className={`kind-dot size-2.5 shrink-0 rounded-full bg-primary ${node.kind}`}
          />
          <span className="min-w-0 flex-1">
            <strong className="block">{node.title}</strong>
            <small className="mt-1 block text-xs text-muted-foreground">
              {node.version_no ? (
                <>
                  <span>版本 {node.version_no}</span> ·{" "}
                </>
              ) : null}
              {nodeLabel(node)}
              {selectionLabel(node) ? ` · ${selectionLabel(node)}` : ""}
            </small>
            {node.resource_id && (
              <small className="mt-1 block break-all text-xs text-muted-foreground">
                {node.resource_id}
              </small>
            )}
          </span>
          <span className="flex shrink-0 flex-col gap-1">
            <StatusBadge status={node.status} />
            {node.stale && node.status !== "STALE" && (
              <StatusBadge status="STALE" />
            )}
          </span>
        </Button>
      ))}
      {!matching.length && <p>没有匹配的内容，请调整筛选或刷新工作区。</p>}
      {last > 0 && (
        <nav aria-label="媒体列表分页" className="flex flex-wrap gap-2">
          <Button
            variant="outline"
            disabled={!current}
            onClick={() => setPage(current - 1)}
          >
            上一页
          </Button>
          <Button
            variant="outline"
            disabled={current === last}
            onClick={() => setPage(current + 1)}
          >
            下一页
          </Button>
        </nav>
      )}
      {pagination}
    </section>
  );
}
