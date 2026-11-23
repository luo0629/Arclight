"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import VersionImage from "@/features/assets/components/version-image";
import { loadVideoBlob, readVideoCandidate } from "@/features/video/api";
import { message } from "@/lib/api/client";
import type { CanvasSceneShot, GraphNode } from "@/types/api";
import { imagePath } from "../nodes";

const shotStatus: Record<string, string> = {
  PENDING: "等待制作",
  CREATED: "排队中",
  SUBMITTING: "正在提交",
  SUBMITTED: "生成中",
  RUNNING: "生成中",
  RETRY_WAIT: "等待重试",
  SUCCEEDED: "待检查与选用",
  FAILED: "制作失败",
  CANCEL_REQUESTED: "正在取消",
  CANCELLED: "已取消",
  RESULT_UNKNOWN: "等待核对生成结果",
  RECONCILING: "正在核对生成结果",
  MANUAL_REVIEW: "生成结果待处理",
  READY: "素材就绪",
};

function SceneVideo({
  node,
  projectId,
  onUnavailable,
}: {
  node: GraphNode;
  projectId: string;
  onUnavailable: (cause: unknown) => void;
}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const active = useRef<AbortController | null>(null);
  const objectUrl = useRef("");
  useEffect(
    () => () => {
      active.current?.abort();
      if (objectUrl.current) URL.revokeObjectURL(objectUrl.current);
    },
    [],
  );

  async function play() {
    if (active.current || !node.candidate_id) return;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    setError("");
    try {
      const candidate = await readVideoCandidate(
        projectId,
        node.candidate_id,
        controller.signal,
      );
      if (
        candidate.version?.id !== node.resource_id ||
        candidate.version?.checksum !== node.checksum
      ) {
        throw new Error("视频与画布上的精确版本不一致，请刷新重试。");
      }
      const blob = await loadVideoBlob(projectId, candidate, controller.signal);
      if (controller.signal.aborted) return;
      objectUrl.current = URL.createObjectURL(blob);
      setUrl(objectUrl.current);
    } catch (cause) {
      if (controller.signal.aborted) return;
      setError(message(cause));
      onUnavailable(cause);
    } finally {
      active.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return url ? (
    <video
      aria-label="镜头视频"
      src={url}
      controls
      autoPlay
      playsInline
      className="w-full rounded-md"
    />
  ) : (
    <>
      <Button
        variant="outline"
        size="sm"
        disabled={busy}
        onClick={() => void play()}
      >
        {busy ? "正在读取视频…" : "播放镜头视频"}
      </Button>
      {error && <p role="alert">{error}</p>}
    </>
  );
}

export default function SceneShots({
  shots,
  nodes,
  projectId,
  onUnavailable,
  onOpenShot,
}: {
  shots: CanvasSceneShot[];
  nodes: ReadonlyMap<string, GraphNode>;
  projectId: string;
  onUnavailable: (cause: unknown) => void;
  onOpenShot: (node: GraphNode) => void;
}) {
  if (!shots.length)
    return <p className="text-xs text-muted-foreground">暂无镜头</p>;

  return (
    <ol className="flex flex-col gap-3" aria-label="本场镜头">
      {shots.map((shot) => {
        const shotNode = nodes.get(shot.id);
        const media = shot.media_node_id
          ? nodes.get(shot.media_node_id)
          : undefined;
        const cover = shot.cover_node_id
          ? nodes.get(shot.cover_node_id)
          : media;
        const path = cover ? imagePath(cover, projectId) : null;
        const label = `镜头 ${String(shot.shot_no).padStart(2, "0")}`;
        return (
          <li
            key={shot.id}
            className="flex flex-col gap-1.5 rounded-lg border p-2"
          >
            {shotNode ? (
              <Button
                variant="ghost"
                size="sm"
                onClick={() => onOpenShot(shotNode)}
              >
                {label}
              </Button>
            ) : (
              <strong>{label}</strong>
            )}
            <p className="line-clamp-2 text-xs">{shot.description}</p>
            {path && (
              <VersionImage
                key={path}
                path={path}
                alt={
                  media?.preview_kind === "VIDEO"
                    ? `${label}视频首帧参考`
                    : `${label}照片`
                }
                onUnavailable={onUnavailable}
                compact
              />
            )}
            {media?.preview_kind === "VIDEO" && (
              <SceneVideo
                key={`${media.id}:${media.checksum}`}
                node={media}
                projectId={projectId}
                onUnavailable={onUnavailable}
              />
            )}
            <p className="text-xs text-muted-foreground">
              {shotStatus[shot.status] ?? "等待更新"}
              {shot.error_code ? ` · ${shot.error_code}` : ""}
            </p>
          </li>
        );
      })}
    </ol>
  );
}
