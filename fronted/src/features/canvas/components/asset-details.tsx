"use client";

import { Button } from "@/components/ui/button";
import { StatusBadge } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import ContinuityComparisonButton from "@/features/continuity/components/continuity-comparison";
import type { Graph, GraphNode } from "@/types/api";

const types: Record<string, string> = {
  CHARACTER: "角色",
  SCENE: "场景",
  PROP: "道具",
  STYLE: "风格",
  SCRIPT: "剧本",
  STORYBOARD: "分镜",
  SHOT_IMAGE: "镜头画面",
  SHOT_PREVIEW: "预览视频",
  AUDIO: "音频",
  SUBTITLE: "字幕",
  TIMELINE: "时间轴",
  RENDER: "成片",
};

export function ExactDependencies({
  node,
  graph,
  onSelect,
}: {
  node: GraphNode;
  graph: Graph;
  onSelect: (node: GraphNode) => void;
}) {
  const edges = graph.edges.filter(
    (edge) =>
      edge.relation === "dependency" &&
      (edge.source === node.id || edge.target === node.id),
  );
  return (
    <section
      aria-label="精确版本依赖"
      className="flex min-w-0 flex-col gap-3 border-t pt-4 text-sm"
    >
      <h3 className="font-semibold">精确版本依赖</h3>
      {edges.length === 0 && (
        <p className="text-muted-foreground">此版本尚无登记依赖。</p>
      )}
      {edges.map((edge) => {
        const incoming = edge.target === node.id;
        const related = graph.nodes.find(
          (item) => item.id === (incoming ? edge.source : edge.target),
        );
        if (!related) return null;
        return (
          <div
            key={edge.id}
            className="flex min-w-0 flex-col gap-1 border-b pb-3"
          >
            <span className="text-xs text-muted-foreground">
              {incoming ? "输入来源" : "下游产物"} · {edge.label}
            </span>
            <Button
              variant="link"
              className="h-auto justify-start whitespace-normal p-0 text-left"
              onClick={() => onSelect(related)}
            >
              {related.title} · v{related.version_no}
            </Button>
            <code className="break-all text-xs">{related.resource_id}</code>
            {edge.dependency_manifest_id && (
              <span className="break-all text-xs text-muted-foreground">
                输入清单：{edge.dependency_manifest_id}
              </span>
            )}
          </div>
        );
      })}
    </section>
  );
}

export default function AssetDetails({
  node,
  graph,
  onSelect,
  projectId,
  onChanged,
}: {
  node: GraphNode;
  graph: Graph;
  onSelect: (node: GraphNode) => void;
  projectId: string;
  onChanged: () => Promise<void>;
}) {
  return (
    <section
      aria-label="画布资产详情"
      className="flex min-w-0 flex-col gap-3 text-sm"
    >
      <h2 className="text-lg font-semibold">{node.title}</h2>
      <p>
        {types[node.asset_type ?? ""] ?? node.asset_type}
        {node.version_no ? ` · v${node.version_no}` : " · 资产目录"}
      </p>
      <div className="flex flex-wrap items-center gap-2">
        <StatusBadge status={node.status} />
        {node.stale && node.status !== "STALE" && (
          <StatusBadge status="STALE" />
        )}
        {node.kind === "asset_version" && (
          <span>
            {node.current
              ? "正式选用"
              : ["DRAFT", "READY_FOR_REVIEW"].includes(node.status)
                ? "候选版本"
                : "历史／未选用"}
          </span>
        )}
      </div>
      {node.kind === "asset_version" &&
        node.asset_id &&
        node.resource_id &&
        ["SHOT_IMAGE", "IMAGE"].includes(node.asset_type ?? "") && (
          <ContinuityComparisonButton
            key={`${projectId}:${node.asset_id}:${node.resource_id}`}
            projectId={projectId}
            assetId={node.asset_id}
            versionId={node.resource_id}
            onChanged={onChanged}
          />
        )}
      {node.preview_path && (
        <VersionImage key={node.preview_path} path={node.preview_path} />
      )}
      <dl className="grid min-w-0 grid-cols-1 gap-2 [&_dt]:text-xs [&_dt]:text-muted-foreground [&_dd]:break-all">
        <dt>资产 ID</dt>
        <dd>{node.asset_id}</dd>
        {node.kind === "asset_version" && (
          <>
            <dt>精确版本 ID</dt>
            <dd>{node.resource_id}</dd>
            <dt>内容校验值</dt>
            <dd>{node.checksum}</dd>
          </>
        )}
        {node.dependency_manifest_id && (
          <>
            <dt>原始输入清单</dt>
            <dd>{node.dependency_manifest_id}</dd>
          </>
        )}
      </dl>
      {node.kind === "asset" && (
        <div
          className="flex flex-col items-start gap-2"
          aria-label="资产精确版本"
        >
          {graph.nodes
            .filter(
              (item) =>
                item.kind === "asset_version" &&
                item.asset_id === node.asset_id,
            )
            .map((version) => (
              <Button
                key={version.id}
                variant="outline"
                onClick={() => onSelect(version)}
              >
                查看 v{version.version_no}
                {version.current ? " · 正式选用" : ""}
                {version.stale ? " · 已失效" : ""}
              </Button>
            ))}
        </div>
      )}
    </section>
  );
}
