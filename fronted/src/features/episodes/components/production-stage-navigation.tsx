"use client";

import Link from "next/link";
import { useCallback, useEffect, useRef, useState } from "react";
import { ChevronDown, ListChecks, X } from "lucide-react";
import { Popover } from "radix-ui";
import { Button } from "@/components/ui/button";
import { api } from "@/lib/api/client";
import { useProjectEvents } from "@/features/projects/project-events";
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

import EpisodeSeriesEvidence from "./episode-series-evidence";

const ignore = () => {};
export default function ProductionStageNavigation({
  episodeId,
  projectId,
  writable,
  refreshRevision,
  onOpen,
}: {
  episodeId: string;
  projectId: string;
  writable: boolean;
  refreshRevision: number;
  onOpen: (view: EpisodeView, node?: string) => void;
}) {
  const { stage, navigate } = useEpisodeNavigation();
  const [open, setOpen] = useState(Boolean(stage));
  const [seriesOpen, setSeriesOpen] = useState(false);
  const read = useCallback(
    async (signal: AbortSignal) => {
      const state = await api<ProductionState>(
        `/episodes/${episodeId}/production-state`,
        { signal: AbortSignal.any([signal, AbortSignal.timeout(12000)]) },
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
  useProjectEvents(status.refresh);
  useEffect(() => {
    if (refreshRevision) status.refresh();
  }, [refreshRevision, status.refresh]);
  useEffect(() => {
    const refreshVisible = () => {
      if (!document.hidden) status.refresh();
    };
    const timer = window.setInterval(refreshVisible, 15000);
    return () => window.clearInterval(timer);
  }, [status.refresh]);
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
    setOpen(false);
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
    else if (current === "audio") onOpen("audio");
    else onOpen("render");
  }
  return (
    <Popover.Root open={open} onOpenChange={setOpen}>
      <Popover.Trigger asChild>
        <Button variant="outline" aria-label="制作进度">
          <ListChecks />
          制作进度
          <ChevronDown className="size-3 text-muted-foreground" />
        </Button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          align="end"
          sideOffset={8}
          collisionPadding={12}
          aria-label="制作进度"
          className="studio-progress-popover"
        >
          <section aria-label="剧集生产阶段">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h2 className="text-sm font-semibold">制作进度</h2>
                <p className="mt-1 text-xs text-muted-foreground" role="status">
                  {state
                    ? phaseLabel(state.phase)
                    : status.loading
                      ? "正在核对生产状态…"
                      : null}
                </p>
              </div>
              <Popover.Close asChild>
                <Button
                  variant="ghost"
                  size="icon-sm"
                  aria-label="关闭制作进度"
                >
                  <X />
                </Button>
              </Popover.Close>
            </div>
            {status.error && (
              <p role="alert" className="mt-2 text-xs text-destructive">
                无法读取制作进度，请刷新后重试。
              </p>
            )}
            <nav aria-label="制作阶段" className="mt-3">
              <ol className="studio-progress-stages">
                {productionStages.map((item, index) => (
                  <li key={item.id}>
                    <Button
                      variant="ghost"
                      aria-current={stage === item.id ? "step" : undefined}
                      aria-expanded={stage === item.id}
                      aria-controls={
                        stage === item.id
                          ? "production-stage-detail"
                          : undefined
                      }
                      className={cn(
                        "h-full min-h-12 w-full flex-col items-start gap-0.5 border-border px-2.5 py-2 text-left",
                        stage === item.id && "border-ring bg-secondary",
                      )}
                      onClick={() => navigate({ stage: item.id })}
                    >
                      <span>
                        {index + 1}. {item.label}
                      </span>
                      {state && (
                        <span className="text-xs font-normal whitespace-normal text-muted-foreground">
                          {stageLabel(state, item.id)}
                        </span>
                      )}
                    </Button>
                  </li>
                ))}
              </ol>
            </nav>
            <div className="mt-3 text-xs text-muted-foreground">
              {state && (
                <div className="flex flex-wrap items-center gap-x-4 gap-y-1">
                  <span>
                    必需镜头已批准 {state.approved_shot_count} /{" "}
                    {state.shot_count}
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
                  {(!writable || state.archived) && (
                    <span>当前只读，可查看各阶段</span>
                  )}
                </div>
              )}
            </div>
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
                  查看阶段不会启动生产。实际提交时会重新检查权限、精确版本和审核状态；请先处理提示中的前置条件。
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
                        key={`${episodeId}:${state.storyboard_version_id}`}
                        episodeId={episodeId}
                        storyboardId={state.storyboard_version_id}
                        evidence={state.evidence_sha256}
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
                          <Link
                            href={projectPath(projectId, "assets")}
                            onClick={() => setOpen(false)}
                          >
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
                      <Button
                        variant="ghost"
                        onClick={() => {
                          setOpen(false);
                          onOpen("tasks");
                        }}
                      >
                        查看本集任务与审核
                      </Button>
                    </div>
                  </>
                )}
              </div>
            )}
            <div className="mt-3 flex flex-wrap items-center gap-1 border-t pt-2">
              <Button
                variant="ghost"
                size="sm"
                aria-expanded={seriesOpen}
                onClick={() => setSeriesOpen((value) => !value)}
              >
                本集连续剧来源
              </Button>
              <Button
                variant="ghost"
                size="sm"
                disabled={status.loading}
                onClick={status.refresh}
              >
                刷新阶段状态
              </Button>
            </div>
            {seriesOpen && (
              <div className="mt-2">
                <EpisodeSeriesEvidence
                  projectId={projectId}
                  episodeId={episodeId}
                />
              </div>
            )}
          </section>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}

function StageAudio({
  episodeId,
  storyboardId,
  evidence,
}: {
  episodeId: string;
  storyboardId: string | null;
  evidence: string;
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
  const previousEvidence = useRef(evidence);
  useEffect(() => {
    if (previousEvidence.current === evidence) return;
    previousEvidence.current = evidence;
    detail.refresh();
  }, [evidence, detail.refresh]);
  const timeline = detail.data;
  return (
    <section
      aria-label="声音与字幕现状"
      className="mt-3 rounded-lg bg-muted/40 p-3"
    >
      {detail.loading && !timeline ? (
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
        </>
      )}
    </section>
  );
}
