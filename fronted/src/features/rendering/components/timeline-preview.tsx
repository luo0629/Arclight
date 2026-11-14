"use client";

import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { renderingApi } from "../api";
import type { RenderDelivery, Timeline } from "../types";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import DeliveryPreview from "./delivery-preview";
import { selectClass } from "./render-format";

const identity = (row: RenderDelivery) => row.id;
export default function TimelinePreview({
  projectId,
  timeline,
  seekMs,
  onTimeChange,
  dirty,
  refresh,
  unavailable,
}: {
  projectId: string;
  timeline: Timeline;
  seekMs: { ms: number; request: number };
  onTimeChange: (ms: number) => void;
  dirty: boolean;
  refresh: number;
  unavailable: (cause: unknown) => void;
}) {
  const episodeId = timeline.snapshot.episode_id;
  const fetchPage = useCallback(
    (cursor: string | undefined, signal: AbortSignal) =>
      renderingApi.listDeliveries(episodeId, cursor, signal),
    [episodeId],
  );
  const list = useRenderList(episodeId, fetchPage, identity, unavailable);
  useEffect(() => {
    if (refresh) void list.load();
  }, [refresh, list.load]);
  const candidates = list.rows.filter(
    (row) =>
      row.timeline_id === timeline.id &&
      row.episode_id === episodeId &&
      row.project_id === projectId,
  );
  const [selectedId, select] = useState("");
  const selected =
    candidates.find((row) => row.id === selectedId) ?? candidates[0];
  const fetchDetail = useCallback(
    async (signal: AbortSignal) => {
      const detail = await renderingApi.getDelivery(
        episodeId,
        selected!.id,
        signal,
      );
      if (
        detail.id !== selected!.id ||
        detail.timeline_id !== timeline.id ||
        detail.timeline_sha256 !== timeline.content_sha256 ||
        detail.project_id !== projectId ||
        detail.episode_id !== episodeId
      )
        throw new Error("成片与当前时间轴的精确版本不一致，已停止预览。");
      return detail;
    },
    [episodeId, selected, timeline.id, timeline.content_sha256, projectId],
  );
  const detail = useRenderDetail(
    `${timeline.id}:${selected?.id ?? ""}`,
    fetchDetail,
    unavailable,
    !!selected,
  );
  return (
    <section
      aria-label="时间轴保存版本预览"
      className="flex min-w-0 flex-col gap-3"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4>保存版本 v{timeline.version_no} 预览</h4>
        <Button
          variant="ghost"
          disabled={list.loading}
          onClick={() => void list.load()}
        >
          刷新时间轴预览
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        播放已渲染的真实
        MP4。修改后先保存新版本，再在下方创建预览清单并提交渲染。
      </p>
      {dirty && (
        <Notice>
          当前草稿尚未渲染。画面仍对应保存版本 v{timeline.version_no}
          ，定位按当前草稿时点。
        </Notice>
      )}
      {list.loading && <p role="status">正在查找匹配版本的成片…</p>}
      {!list.loading && candidates.length === 0 && (
        <p className="text-sm text-muted-foreground">
          此保存版本尚无可播放成片。完成渲染后刷新预览。
        </p>
      )}
      {candidates.length > 0 && (
        <label className="flex flex-col gap-1 text-sm">
          匹配版本的成片
          <select
            aria-label="时间轴预览成片"
            className={selectClass}
            value={selected?.id}
            onChange={(e) => select(e.target.value)}
          >
            {candidates.map((row) => (
              <option key={row.id} value={row.id}>
                成片 v{row.version_no} ·{" "}
                {row.profile === "PREVIEW" ? "预览版" : "正式版"}
              </option>
            ))}
          </select>
        </label>
      )}
      {list.hasMore && (
        <Button
          variant="outline"
          disabled={list.loading}
          onClick={() => void list.load(true)}
        >
          加载更多成片供预览
        </Button>
      )}
      {(list.error || detail.error) && (
        <Notice tone="error">{list.error || detail.error}</Notice>
      )}
      {detail.data && (
        <DeliveryPreview
          key={detail.data.id}
          projectId={projectId}
          episodeId={episodeId}
          delivery={detail.data}
          onUnavailable={unavailable}
          labelPrefix="时间轴"
          seekMs={seekMs}
          onTimeChange={onTimeChange}
        />
      )}
    </section>
  );
}
