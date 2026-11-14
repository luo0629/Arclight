"use client";

import Link from "next/link";
import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import {
  episodePath,
  isResourceId,
  productionDeliveryPath,
} from "@/lib/routes";
import type { GraphNode } from "@/types/api";
import AudioPlayer from "@/features/audio/components/audio-player";
import { renderingApi } from "@/features/rendering/api";
import type {
  Timeline,
  RenderDeliveryDetail,
} from "@/features/rendering/types";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import TimelinePreview from "@/features/rendering/components/timeline-preview";
import DeliveryPreview from "@/features/rendering/components/delivery-preview";
import { duration } from "@/features/rendering/components/render-format";
import { trackNames } from "@/features/rendering/timeline-editing";

type Media =
  | { kind: "TIMELINE"; timeline: Timeline }
  | { kind: "DELIVERY"; delivery: RenderDeliveryDetail };
export default function MediaDetails({
  node,
  projectId,
  unavailable,
}: {
  node: GraphNode;
  projectId: string;
  unavailable: (cause: unknown) => void;
}) {
  const [seek, setSeek] = useState({ ms: 0, request: 0 });
  const read = useCallback(
    async (signal: AbortSignal): Promise<Media> => {
      if (
        !node.resource_id ||
        !node.asset_id ||
        !node.media_episode_id ||
        !node.checksum ||
        !/^[a-f0-9]{64}$/i.test(node.checksum) ||
        ![
          node.resource_id,
          node.asset_id,
          node.media_episode_id,
          projectId,
        ].every(isResourceId)
      )
        throw new Error("媒体来源标识不完整，请刷新工作区。");
      if (node.preview_kind === "TIMELINE") {
        const timeline = await renderingApi.getTimeline(
          node.resource_id,
          signal,
        );
        if (
          timeline.id !== node.resource_id ||
          timeline.asset_id !== node.asset_id ||
          timeline.content_sha256 !== node.checksum ||
          timeline.snapshot.episode_id !== node.media_episode_id
        )
          throw new Error("时间轴与所选精确版本不一致，已停止预览。");
        return { kind: "TIMELINE", timeline };
      }
      if (!node.delivery_id || !isResourceId(node.delivery_id))
        throw new Error("成片登记标识无效。");
      const delivery = await renderingApi.getDelivery(
        node.media_episode_id,
        node.delivery_id,
        signal,
      );
      if (
        delivery.id !== node.delivery_id ||
        delivery.asset_version_id !== node.resource_id ||
        delivery.asset_id !== node.asset_id ||
        delivery.sha256 !== node.checksum ||
        delivery.project_id !== projectId ||
        delivery.episode_id !== node.media_episode_id
      )
        throw new Error("成片与所选精确版本不一致，已停止预览。");
      return { kind: "DELIVERY", delivery };
    },
    [
      node.resource_id,
      node.asset_id,
      node.media_episode_id,
      node.preview_kind,
      node.delivery_id,
      node.checksum,
      projectId,
    ],
  );
  const detail = useRenderDetail(
    `${projectId}:${node.id}:${node.checksum}`,
    read,
    unavailable,
    ["TIMELINE", "DELIVERY"].includes(node.preview_kind ?? ""),
  );
  if (node.preview_kind === "IMAGE") return null;
  if (
    node.preview_kind === "AUDIO" &&
    node.media_file_id &&
    node.resource_id &&
    node.asset_id &&
    [projectId, node.asset_id, node.resource_id, node.media_file_id].every(
      isResourceId,
    ) &&
    node.checksum &&
    /^[a-f0-9]{64}$/i.test(node.checksum)
  )
    return (
      <AudioPlayer
        key={`${node.media_file_id}:${node.checksum}`}
        projectId={projectId}
        fileId={node.media_file_id}
        expectedSha256={node.checksum}
        expectedVersionId={node.resource_id}
        labelPrefix="画布"
        unavailable={unavailable}
      />
    );
  return (
    <section
      aria-label="画布媒体预览"
      className="flex min-w-0 flex-col gap-3 border-t pt-4"
    >
      {detail.loading && <p role="status">正在读取精确媒体版本…</p>}
      {detail.error && (
        <Notice tone="error">
          {detail.error}
          <Button variant="outline" onClick={() => void detail.load()}>
            重新读取媒体
          </Button>
        </Notice>
      )}
      {detail.data?.kind === "TIMELINE" && (
        <>
          <h3>
            时间轴 v{detail.data.timeline.version_no} ·{" "}
            {duration(detail.data.timeline.snapshot.duration_ms)}
          </h3>
          {detail.data.timeline.snapshot.tracks.map((track) => (
            <div key={track.id} className="min-w-0 border-b pb-2 text-sm">
              <strong>{trackNames[track.kind]}</strong> · {track.clips.length}{" "}
              个片段{track.enabled === false ? " · 已关闭" : ""}
              {track.clips.slice(0, 3).map((clip) => (
                <button
                  key={clip.id}
                  type="button"
                  className="mt-1 block w-full break-all rounded-md p-1 text-left text-xs hover:bg-muted focus-visible:ring-2 focus-visible:ring-ring"
                  onClick={() =>
                    setSeek((value) => ({
                      ms: clip.start_ms,
                      request: value.request + 1,
                    }))
                  }
                >
                  {duration(clip.start_ms)} · {duration(clip.duration_ms)} ·{" "}
                  {clip.source_asset_version_id}
                </button>
              ))}
              {track.clips.length > 3 && (
                <p className="text-xs text-muted-foreground">
                  其余片段可在时间轴编辑器查看。
                </p>
              )}
            </div>
          ))}
          <TimelinePreview
            key={detail.data.timeline.id}
            projectId={projectId}
            timeline={detail.data.timeline}
            seekMs={seek}
            onTimeChange={() => {}}
            dirty={false}
            refresh={0}
            unavailable={unavailable}
          />
          <Link
            className="text-sm underline"
            href={`${episodePath(projectId, detail.data.timeline.snapshot.episode_id, "render")}&timeline=${detail.data.timeline.id}`}
          >
            打开此时间轴版本
          </Link>
        </>
      )}
      {detail.data?.kind === "DELIVERY" && (
        <>
          <h3>
            {detail.data.delivery.profile === "PREVIEW"
              ? "预览输出"
              : "正式输出"}
          </h3>
          <p className="text-sm">
            {detail.data.delivery.is_delivery
              ? "已正式交付"
              : "尚未正式交付，请查看验收报告。"}
          </p>
          <DeliveryPreview
            key={detail.data.delivery.id}
            projectId={projectId}
            episodeId={detail.data.delivery.episode_id}
            delivery={detail.data.delivery}
            onUnavailable={unavailable}
            labelPrefix="画布"
          />
          <Link
            className="text-sm underline"
            href={productionDeliveryPath(detail.data.delivery) ?? "#"}
          >
            查看成片与验收报告
          </Link>
        </>
      )}
      {(!node.preview_kind || node.preview_kind === "AUDIO") && (
        <p className="text-sm text-muted-foreground">
          此精确版本暂无可读取的已登记媒体。历史记录和来源仍保留，刷新工作区可读取最新状态。
        </p>
      )}
    </section>
  );
}
