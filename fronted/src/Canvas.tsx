import { Sprout, FileText, Clapperboard, Film } from "lucide-react";
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
import {
  api,
  json,
  message,
  type Graph,
  type GraphNode,
  type Layout,
} from "./api";

type Paper = Node<GraphNode & Record<string, unknown>, "paper">;
const names: Record<string, string> = {
  episode: "故事起点",
  script: "剧本",
  storyboard: "分镜",
  shot: "镜头",
};
function PaperNode({ data, selected }: NodeProps<Paper>) {
  const Icon =
    { episode: Sprout, script: FileText, storyboard: Clapperboard, shot: Film }[
      data.kind
    ] ?? FileText;
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
        {names[data.kind]}
        {data.version_no ? ` / v${data.version_no}` : ""}
      </div>
      <strong>{data.title}</strong>
      {data.description && <p>{data.description}</p>}
      <StatusBadge status={data.status} />
      {data.duration_ms && <small>{data.duration_ms / 1000} 秒</small>}
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </article>
  );
}
const nodeTypes = { paper: PaperNode };
function buildNodes(graph: Graph): Paper[] {
  const counts: Record<string, number> = {};
  return graph.nodes.map((data) => {
    const index = counts[data.kind] ?? 0;
    counts[data.kind] = index + 1;
    const column =
      { episode: 0, script: 1, storyboard: 2, shot: 3 }[data.kind] ?? 0;
    return {
      id: data.id,
      type: "paper",
      data: { ...data },
      position: graph.layout.positions[data.id] ?? {
        x: 30 + column * 290,
        y: 45 + index * 190,
      },
    };
  });
}
export default function Canvas({
  graph,
  episodeId,
  writable,
  onSelect,
  selectedId,
  focusId,
}: {
  selectedId?: string;
  focusId?: string;
  graph: Graph;
  episodeId: string;
  writable: boolean;
  onSelect: (node: GraphNode) => void;
}) {
  const [nodes, setNodes, onNodesChange] = useNodesState<Paper>(
    buildNodes(graph).map((node) => ({
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
      buildNodes(graph).map((node) => {
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
  }, [graph, selectedId, setNodes]);
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
    <div className="canvas-wrap relative h-[max(560px,calc(100dvh-354px))] min-w-0 overflow-hidden rounded-2xl border bg-card shadow-sm">
      <div
        className="canvas-note pointer-events-none absolute top-3 left-3 z-10 max-w-[calc(100%-24px)] rounded-lg border bg-card/90 px-3 py-2 text-xs text-muted-foreground"
        role="status"
      >
        {notice || "拖动便签整理思路，连线来自真实版本依赖。"}
      </div>
      <ReactFlow<Paper>
        nodes={nodes}
        edges={graph.edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        onInit={setInstance}
        onNodeClick={(_, node) => onSelect(node.data)}
        onNodeDragStop={() => setDirty(true)}
        onMoveEnd={() => setDirty(true)}
        nodesDraggable={writable}
        nodesConnectable={false}
        edgesReconnectable={false}
        deleteKeyCode={null}
        defaultViewport={graph.layout.viewport}
        minZoom={0.1}
        maxZoom={3}
        fitView={graph.layout.revision === 0}
        ariaLabelConfig={{
          "controls.zoomIn.ariaLabel": "放大画布",
          "controls.zoomOut.ariaLabel": "缩小画布",
          "controls.fitView.ariaLabel": "适应画布",
          "controls.interactive.ariaLabel": "切换交互",
        }}
      >
        <Background color="#c8dbdf" gap={22} size={1.2} />
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
