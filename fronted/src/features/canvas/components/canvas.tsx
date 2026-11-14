"use client";

import {
  Sprout,
  FileText,
  Clapperboard,
  Film,
  Image,
  Folder,
  Volume2,
} from "lucide-react";
import { cn } from "@/lib/utils";
import { StatusBadge } from "@/components/ui-kit";
import { Button } from "@/components/ui/button";
import { useEffect, useRef, useState } from "react";
import {
  Background,
  Controls,
  Handle,
  Position,
  ReactFlow,
  useNodesState,
  type Node,
  type NodeProps,
  type ReactFlowInstance,
} from "@xyflow/react";
import { api, json, message } from "@/lib/api/client";
import type { Graph, GraphNode, Layout } from "@/types/api";
import VersionImage from "@/features/assets/components/version-image";
import { imagePath, nodeGroup, nodeLabel, selectionLabel } from "../nodes";

type Paper = Node<
  GraphNode & {
    projectId: string;
    unavailable: (cause: unknown) => void;
    selectNode: (node: GraphNode) => void;
  } & Record<string, unknown>,
  "paper"
>;
function PaperNode({ data, selected }: NodeProps<Paper>) {
  const path = imagePath(data, data.projectId);
  const Icon =
    data.preview_kind === "AUDIO"
      ? Volume2
      : data.preview_kind === "DELIVERY"
        ? Film
        : data.preview_kind === "TIMELINE"
          ? Clapperboard
          : ({
              episode: Sprout,
              script: FileText,
              storyboard: Clapperboard,
              shot: Film,
              asset: Folder,
              asset_version: Image,
            }[data.kind] ?? FileText);
  return (
    <article
      className={cn(
        "paper-node flex w-60 flex-col gap-3 rounded-xl border bg-card p-4 shadow-sm [&>strong]:line-clamp-2 [&>p]:line-clamp-3 [&>p]:text-xs [&>p]:text-muted-foreground [&>small]:text-xs",
        data.kind,
        data.kind === "episode" && "bg-accent",
        data.kind === "storyboard" && "bg-secondary/50",
        selected && "selected ring-2 ring-ring ring-offset-4",
      )}
    >
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <div className="paper-kicker flex items-center gap-2 text-[11px] text-muted-foreground">
        <span className="flex size-7 items-center justify-center rounded-lg bg-background text-primary">
          <Icon className="size-4" />
        </span>
        {nodeLabel(data)}
        {data.version_no ? ` / v${data.version_no}` : ""}
      </div>
      <strong>{data.title}</strong>
      {path && (
        <VersionImage
          key={path}
          path={path}
          onUnavailable={data.unavailable}
          compact
        />
      )}
      {data.description && <p>{data.description}</p>}
      <StatusBadge status={data.status} />
      {data.stale && data.status !== "STALE" && <StatusBadge status="STALE" />}
      {selectionLabel(data) && <small>{selectionLabel(data)}</small>}
      {data.duration_ms && <small>{data.duration_ms / 1000} 秒</small>}
      <Button
        variant="outline"
        size="sm"
        className="nodrag nopan"
        aria-label={`查看${nodeLabel(data)}：${data.title}${data.version_no ? ` v${data.version_no}` : ""}`}
        onClick={(event) => {
          event.stopPropagation();
          data.selectNode(data);
        }}
      >
        查看内容与预览
      </Button>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </article>
  );
}
const nodeTypes = { paper: PaperNode };
function buildNodes(
  graph: Graph,
  projectId: string,
  unavailable: (cause: unknown) => void,
  selectNode: (node: GraphNode) => void,
): Paper[] {
  const counts: Record<string, number> = {};
  return graph.nodes.map((data) => {
    const group = data.kind === "asset_version" ? nodeGroup(data) : data.kind;
    const index = counts[group] ?? 0;
    counts[group] = index + 1;
    const column =
      {
        episode: 0,
        script: 1,
        storyboard: 2,
        shot: 3,
        asset: -2,
        assets: -1,
        images: 4,
        audio: 5,
        timelines: 6,
        deliveries: 7,
      }[group] ?? 0;
    return {
      id: data.id,
      type: "paper",
      data: { ...data, projectId, unavailable, selectNode },
      ariaLabel: `${nodeLabel(data)}：${data.title}${data.version_no ? ` v${data.version_no}` : ""}`,
      position: graph.layout.positions[data.id] ?? {
        x: 30 + column * 290,
        y: 45 + index * (data.kind === "asset_version" ? 320 : 210),
      },
    };
  });
}
export default function Canvas({
  graph,
  episodeId,
  projectId,
  onUnavailable,
  writable,
  onSelect,
  onOpenShot,
  selectedId,
  focusId,
}: {
  selectedId?: string;
  focusId?: string;
  graph: Graph;
  episodeId: string;
  projectId: string;
  onUnavailable: (cause: unknown) => void;
  writable: boolean;
  onSelect: (node: GraphNode) => void;
  onOpenShot: (node: GraphNode) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Paper>(
    buildNodes(graph, projectId, onUnavailable, onSelect).map((node) => ({
      ...node,
      selected: node.id === selectedId,
    })),
  );
  const [instance, setInstance] = useState<ReactFlowInstance<Paper> | null>(
    null,
  );
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  const [notice, setNotice] = useState("");
  const revision = useRef(graph.layout.revision);
  const focused = useRef<string | undefined>(undefined);
  useEffect(() => {
    setNodes((current) =>
      buildNodes(graph, projectId, onUnavailable, onSelect).map((node) => {
        const prior = current.find((item) => item.id === node.id);
        return {
          ...node,
          position: prior?.position ?? node.position,
          selected: node.id === selectedId,
        };
      }),
    );
    if (graph.layout.revision > revision.current)
      setNotice("布局已被其他成员更新；保存可能冲突，请先刷新页面。");
  }, [graph, selectedId, setNodes, projectId, onUnavailable, onSelect]);
  useEffect(() => {
    if (!instance || !focusId || focused.current === focusId) return;
    const target = nodes.find((node) => node.id === focusId);
    if (!target?.measured?.width || !target.measured.height) return;
    focused.current = focusId;
    void instance.fitView({
      nodes: [{ id: focusId }],
      minZoom: 0.5,
      maxZoom: 1,
      duration: 0,
    });
  }, [instance, focusId, nodes]);
  async function save() {
    if (!instance) return;
    setSaving(true);
    setNotice("");
    try {
      const result = await api<Layout>(
        `/episodes/${episodeId}/canvas/layout`,
        json("PUT", {
          expected_revision: revision.current,
          positions: Object.fromEntries(
            nodes.map((node) => [node.id, node.position]),
          ),
          viewport: instance.getViewport(),
        }),
      );
      revision.current = result.revision;
      setDirty(false);
      setNotice("画布位置已保存");
    } catch (error) {
      setNotice(message(error));
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="canvas-wrap relative min-w-0 overflow-hidden bg-card">
      <div
        className="canvas-note pointer-events-none absolute top-3 left-3 z-10 max-w-[calc(100%-24px)] rounded-lg border bg-card/90 px-3 py-2 text-xs text-muted-foreground"
        role="status"
      >
        {notice ||
          "文本与媒体精确版本 · 实线标注真实依赖；也可切换版本列表查看和预览。"}
      </div>
      <ReactFlow<Paper>
        nodes={nodes}
        edges={graph.edges.map((edge) => ({
          ...edge,
          style: {
            strokeDasharray: edge.relation === "dependency" ? undefined : "5 5",
          },
        }))}
        nodeTypes={nodeTypes}
        onNodeDoubleClick={(_, node) => {
          if (node.data.kind === "shot") onOpenShot(node.data);
        }}
        onNodesChange={onNodesChange}
        onInit={setInstance}
        onNodeClick={(_, node) => onSelect(node.data)}
        onNodeDragStop={() => setDirty(true)}
        onMoveEnd={() => setDirty(true)}
        nodesDraggable={writable && !saving}
        nodesConnectable={false}
        onlyRenderVisibleElements
        edgesReconnectable={false}
        deleteKeyCode={null}
        defaultViewport={graph.layout.viewport}
        minZoom={0.1}
        maxZoom={3}
        fitView={graph.layout.revision === 0}
        fitViewOptions={{ padding: 0.25, maxZoom: 1 }}
        ariaLabelConfig={{
          "controls.zoomIn.ariaLabel": "放大画布",
          "controls.zoomOut.ariaLabel": "缩小画布",
          "controls.fitView.ariaLabel": "适应画布",
          "controls.interactive.ariaLabel": "切换交互",
        }}
      >
        <Background color="var(--border)" gap={22} size={1.2} />
        <Controls showInteractive={false} />
      </ReactFlow>
      {writable && (
        <Button
          variant="outline"
          size="lg"
          className="save-layout absolute right-3 bottom-3 z-10 shadow-sm"
          onClick={save}
          disabled={!dirty || saving}
        >
          {saving ? "保存中…" : dirty ? "保存画布位置" : "位置已保存"}
        </Button>
      )}
    </div>
  );
}
