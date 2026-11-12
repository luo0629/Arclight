"use client";

import Link from "next/link";
import { useCallback, useEffect } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { api } from "@/lib/api/client";
import { cn } from "@/lib/utils";
import { isResourceId, projectPath, type EpisodeView } from "@/lib/routes";
import useEpisodeNavigation from "../hooks/use-episode-navigation";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import { renderingApi } from "@/features/rendering/api";
import {
  phaseLabel,
  issueLabel,
  productionStages,
  stageLabel,
  stageIssues,
  stageGuidance,
  type ProductionState,
  type ProductionStage,
} from "../production-stages";

const ignore = () => {};
export default function ProductionStageNavigation({
  episodeId,
  projectId,
  writable,
  revision,
  onOpen,
}: {
  episodeId: string;
  projectId: string;
  writable: boolean;
  revision: unknown;
  onOpen: (view: EpisodeView, node?: string) => void;
}) {
  const { stage, navigate } = useEpisodeNavigation();
  const read = useCallback(
    async (signal: AbortSignal) => {
      const state = await api<ProductionState>(
        `/episodes/${episodeId}/production-state`,
        { signal },
      );
      if (
        state.schema_version !== "production-state/1" ||
        state.episode_id !== episodeId ||
        state.project_id !== projectId ||
        !Array.isArray(state.conditions) ||
        !Number.isSafeInteger(state.shot_count) ||
        !Number.isSafeInteger(state.approved_shot_count) ||
        state.shot_count < 0 ||
        state.approved_shot_count < 0 ||
        state.approved_shot_count > state.shot_count
      )
        throw new Error("Invalid production state");
      return state;
    },
    [episodeId, projectId],
  );
  const status = useRenderDetail(
    `${projectId}:${episodeId}:${writable}`,
    read,
    ignore,
  );
  useEffect(() => {
    if (revision) void status.load();
  }, [revision, status.load]);
  useEffect(() => {
    const refresh = () => {
      if (!document.hidden) void status.load();
    };
    const timer = window.setInterval(refresh, 15000);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [status.load]);
  const state = status.data;
  const selected = productionStages.find((item) => item.id === stage);
  const pendingShots = state
    ? [
        "QA_PENDING",
        "CONTINUITY_REVIEW",
        "READY_FOR_REVIEW",
        "NEEDS_REVIEW",
      ].reduce((sum, key) => sum + (state.shot_counts[key] ?? 0), 0)
    : 0;
  function openStage(current: ProductionStage) {
    if (!state) return;
    const version =
      current === "script"
        ? state.script_version_id
        : state.storyboard_version_id;
    if (current === "script" || current === "storyboard")
      onOpen(
        "list",
        version && isResourceId(version) ? `${current}:${version}` : "",
      );
    else if (current === "media") onOpen("library");
    else onOpen("render");
  }
  return (
    <section
      aria-label="剧集生产阶段"
      className="mb-4 max-h-[45dvh] min-w-0 shrink-0 overflow-y-auto rounded-xl border bg-card p-3 sm:p-4"
    >
      <div className="mb-3 flex flex-wrap items-center justify-between gap-2 text-sm">
        <h2 className="font-semibold">制作进度</h2>
        <span role="status">
          {status.loading
            ? "正在核对生产状态…"
            : state
              ? phaseLabel(state.phase)
              : "生产状态暂不可用"}
        </span>
        <Button
          variant="ghost"
          size="sm"
          disabled={status.loading}
          onClick={() => void status.load()}
        >
          刷新阶段状态
        </Button>
      </div>
      <nav aria-label="制作阶段" className="overflow-x-auto pb-2">
        <ol className="flex min-w-max gap-1">
          {productionStages.map((item, index) => (
            <li key={item.id}>
              <Button
                variant="ghost"
                aria-current={stage === item.id ? "step" : undefined}
                aria-expanded={stage === item.id}
                aria-controls="production-stage-detail"
                className={cn(
                  "h-auto min-w-24 flex-col items-start gap-1 px-3 py-2",
                  stage === item.id && "bg-muted ring-1 ring-border",
                )}
                onClick={() => navigate({ stage: item.id })}
              >
                <span>
                  {index + 1}. {item.label}
                </span>
                <span className="text-xs font-normal text-muted-foreground">
                  {state ? stageLabel(state, item.id) : "待核对"}
                </span>
              </Button>
            </li>
          ))}
        </ol>
      </nav>
      {status.error && (
        <Notice tone="error" role="alert">
          无法读取当前剧集的生产状态。旧状态已清空，请确认权限或稍后重试。
        </Notice>
      )}
      {state && (
        <div className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-1 text-xs text-muted-foreground">
          <span>
            必需镜头已批准 {state.approved_shot_count} / {state.shot_count}
          </span>
          {state.shot_count > 0 && (
            <progress
              aria-label="必需镜头批准进度"
              value={state.approved_shot_count}
              max={state.shot_count}
              className="h-2 w-28 accent-primary"
            />
          )}
          <span>待检查／审核镜头 {pendingShots}</span>
          {state.has_partial_failure && (
            <span>存在部分失败，请查看任务详情</span>
          )}
          {state.blocked_phase && (
            <span>阻塞位置：{phaseLabel(state.blocked_phase)}</span>
          )}
          {(!writable || state.archived) && <span>当前只读，可查看各阶段</span>}
        </div>
      )}
      {selected && (
        <div
          id="production-stage-detail"
          className="mt-3 border-t pt-3 text-sm"
        >
          <div className="flex items-center justify-between gap-2">
            <h3>{selected.label} · 阶段详情</h3>
            <Button
              variant="ghost"
              size="sm"
              onClick={() => navigate({ stage: "" })}
            >
              收起阶段详情
            </Button>
          </div>
          <p className="mt-2 max-w-3xl text-muted-foreground">
            {stageGuidance[selected.id]}
          </p>
          <p className="mt-2 text-xs text-muted-foreground">
            查看阶段不会启动生产。实际提交时会重新检查权限、精确版本、审核和预算；请先处理提示中的前置条件。
          </p>
          {state && (
            <>
              <ul aria-label="阶段待处理条件" className="mt-2 space-y-1">
                {[
                  ...new Set(
                    [
                      ...stageIssues(state, selected.id),
                      ...state.issues,
                      ...state.task_issues,
                    ].map(issueLabel),
                  ),
                ].map((label) => (
                  <li key={label}>{label}</li>
                ))}
              </ul>
              {selected.id === "audio" && (
                <StageAudio
                  key={`${episodeId}:${state.evidence_sha256}`}
                  episodeId={episodeId}
                  storyboardId={state.storyboard_version_id}
                />
              )}
              {selected.id === "media" && (
                <p className="mt-2 text-xs text-muted-foreground">
                  未绑定必需镜头的生成任务{" "}
                  {Object.values(state.unbound_task_counts).reduce(
                    (sum, count) => sum + count,
                    0,
                  )}{" "}
                  项，单独在任务中心核对，不计入镜头完成进度。
                </p>
              )}
              <div className="mt-3 flex flex-wrap gap-2">
                {selected.id === "assets" ? (
                  <Button asChild variant="outline">
                    <Link href={projectPath(projectId, "assets")}>
                      查看角色与场景素材
                    </Link>
                  </Button>
                ) : (
                  <Button
                    variant="outline"
                    onClick={() => openStage(selected.id)}
                  >
                    查看
                    {selected.id === "audio"
                      ? "声音与字幕输入"
                      : selected.id === "export"
                        ? "渲染与成片"
                        : selected.id === "timeline"
                          ? "时间轴版本"
                          : selected.id === "media"
                            ? "镜头素材列表"
                            : `${selected.label}版本`}
                  </Button>
                )}
                <Button variant="ghost" onClick={() => onOpen("tasks")}>
                  查看本集任务与审核
                </Button>
              </div>
            </>
          )}
        </div>
      )}
    </section>
  );
}

function StageAudio({
  episodeId,
  storyboardId,
}: {
  episodeId: string;
  storyboardId: string | null;
}) {
  const read = useCallback(
    async (signal: AbortSignal) => {
      const timelines = await renderingApi.listTimelines(
        episodeId,
        undefined,
        signal,
      );
      const timeline = timelines[0];
      if (!timeline) return null;
      if (timeline.snapshot.episode_id !== episodeId)
        throw new Error("Invalid timeline scope");
      return timeline;
    },
    [episodeId],
  );
  const detail = useRenderDetail(episodeId, read, ignore);
  const timeline = detail.data;
  return (
    <div
      aria-label="声音与字幕现状"
      className="mt-3 rounded-lg bg-muted/40 p-3"
    >
      {detail.loading ? (
        <p>正在读取精确时间轴…</p>
      ) : detail.error ? (
        <>
          <p role="alert">声音与字幕数据读取失败，不能判断完成情况。</p>
          <Button variant="ghost" onClick={() => void detail.load()}>
            重试声音与字幕
          </Button>
        </>
      ) : !timeline ? (
        <p>尚无时间轴，配音与字幕装配情况待核对。</p>
      ) : (
        <>
          <p>
            时间轴 v{timeline.version_no}：配音片段{" "}
            {timeline.snapshot.tracks
              .filter((track) => track.kind === "VOICE")
              .reduce((sum, track) => sum + track.clips.length, 0)}
            ，字幕片段{" "}
            {timeline.snapshot.tracks
              .filter((track) => track.kind === "SUBTITLE")
              .reduce((sum, track) => sum + track.clips.length, 0)}
            。
          </p>
          <p className="mt-1 text-xs text-muted-foreground">
            仅统计此时间轴已装配的精确片段，不代表所有台词已配音或字幕已批准。
          </p>
          {(timeline.stale ||
            timeline.snapshot.storyboard_id !== storyboardId) && (
            <p className="mt-2">
              此时间轴已失效或来源分镜已变化，需更新后重新核对。
            </p>
          )}
          <details className="mt-2 text-xs">
            <summary>查看来源时间轴</summary>
            <p className="mt-1 break-all">{timeline.id}</p>
          </details>
        </>
      )}
    </div>
  );
}
