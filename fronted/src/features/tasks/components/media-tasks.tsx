"use client";

import { useEffect } from "react";
import { useSearchParams } from "next/navigation";
import { isResourceId } from "@/lib/routes";

import { Button } from "@/components/ui/button";
import {
  EmptyState,
  LoadingState,
  Notice,
  PagePanel,
} from "@/components/ui-kit";
import useMediaTasks from "../hooks/use-media-tasks";
import { MEDIA_PAGE_SIZE } from "../media-types";
import type { MediaJob, MediaQueue } from "../media-types";
import MediaTaskDetail, {
  MediaStatusBadge,
  mediaTime,
} from "./media-task-detail";

const taskNames: Record<string, string> = {
  image: "图片生成",
  image_generation: "图片生成",
  video: "视频生成",
  video_generation: "视频生成",
  render: "媒体渲染",
  speech: "配音生成",
  tts: "配音生成",
  audio: "音频生成",
  audio_generation: "音频生成",
  continuity_revision: "画面修订",
  configuration: "设定图生成",
  concept_image: "概念图生成",
};
const taskName = (job: MediaJob) =>
  taskNames[job.task_type.toLowerCase()] ?? job.task_type;
const waitTime = (seconds: number | null) =>
  seconds === null ? "未知" : `${seconds} 秒`;

function QueueSummary({ queue }: { queue: MediaQueue }) {
  return (
    <section
      aria-label="媒体队列"
      className="flex min-w-0 flex-col gap-4 rounded-xl border bg-muted/20 p-4"
    >
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h3 className="font-semibold">项目媒体队列</h3>
        <p className="text-xs text-muted-foreground">
          观测于 {mediaTime(queue.observed_at)}
        </p>
      </div>
      <p className="text-xs leading-relaxed text-muted-foreground">
        按业务任务计数。排队任务可能等待退避或执行许可，当前未提供单任务队位及预计完成时间。
      </p>
      {queue.scheduler_stale && (
        <Notice>
          调度心跳已过期，排队任务可能暂未推进。最近心跳：
          {mediaTime(queue.scheduler_heartbeat_at)}。
        </Notice>
      )}
      <div className="flex min-w-0 flex-col divide-y">
        {queue.lanes.map((lane) => (
          <div
            key={lane.lane}
            className="flex min-w-0 flex-col gap-3 py-3 first:pt-0 last:pb-0"
          >
            <h4 className="text-sm font-semibold">
              {lane.lane === "INTERACTIVE" ? "交互队列" : "批量队列"}
            </h4>
            <dl className="grid min-w-0 grid-cols-3 gap-3 text-sm sm:grid-cols-4 lg:grid-cols-7">
              {(
                [
                  ["可调度", lane.ready],
                  ["退避等待", lane.backoff],
                  ["等待许可", lane.waiting_permit],
                  ["执行中", lane.executing],
                  ["结果未知", lane.unknown],
                  ["已结束", lane.terminal],
                  ["死信", lane.dead_letter],
                ] as const
              ).map(([label, value]) => (
                <div key={label}>
                  <dt className="text-xs text-muted-foreground">{label}</dt>
                  <dd className="mt-1 font-medium tabular-nums">{value}</dd>
                </div>
              ))}
            </dl>
            <p className="text-xs leading-relaxed text-muted-foreground">
              最长等待 {waitTime(lane.oldest_wait_seconds)}；等待中位数{" "}
              {waitTime(lane.wait_seconds.p50)}；95% 分位{" "}
              {waitTime(lane.wait_seconds.p95)}。
            </p>
            {!!Object.keys(lane.blocked_reasons).length && (
              <ul
                className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-muted-foreground"
                aria-label={`${lane.lane === "INTERACTIVE" ? "交互" : "批量"}队列阻塞原因`}
              >
                {Object.entries(lane.blocked_reasons).map(([reason, count]) => (
                  <li
                    key={reason}
                    className="break-words [overflow-wrap:anywhere]"
                  >
                    {reason}：{count}
                  </li>
                ))}
              </ul>
            )}
          </div>
        ))}
      </div>
    </section>
  );
}

export default function MediaTasks({
  projectId,
  episodeId,
  owner,
  writable,
  busy,
}: {
  projectId: string;
  episodeId: string;
  owner: boolean;
  writable: boolean;
  busy: boolean;
}) {
  const state = useMediaTasks({
    projectId,
    scopeKey: episodeId,
    writable,
    owner,
  });
  const requestedJob = useSearchParams().get("generation_job");
  const { selectJob } = state;
  useEffect(() => {
    if (requestedJob && isResourceId(requestedJob)) selectJob(requestedJob);
  }, [requestedJob, projectId, episodeId, selectJob]);
  const selectedInPage = state.jobs.some((job) => job.id === state.selectedId);
  const listDisabled = busy || state.loading || state.mutating;
  const detail = state.selectedId ? (
    <MediaTaskDetail
      key={state.selectedId}
      state={state}
      selectedId={state.selectedId}
      owner={owner}
      writable={writable}
      busy={busy}
    />
  ) : null;

  return (
    <section aria-label="项目媒体任务" className="min-w-0">
      <PagePanel
        title="媒体任务中心"
        description="这里显示当前项目的媒体任务，包含其他剧集与未分集任务。文字创作和审核保留在下方。"
      >
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="max-w-2xl text-sm leading-relaxed text-muted-foreground">
            查看每次尝试、历史供应商、排队和费用，再根据任务状态恢复执行。进行中的任务会自动刷新。
          </p>
          <Button
            variant="outline"
            size="lg"
            disabled={listDisabled}
            onClick={state.refresh}
          >
            刷新媒体任务
          </Button>
        </div>
        {!writable && (
          <p className="text-sm text-muted-foreground">
            当前为只读状态，可查看任务与费用。
          </p>
        )}
        {state.notice && <Notice>{state.notice}</Notice>}
        {state.error && <Notice tone="error">{state.error}</Notice>}
        {state.queueError && (
          <Notice tone="error">媒体队列：{state.queueError}</Notice>
        )}
        {state.queue && <QueueSummary queue={state.queue} />}
        {state.loading && !state.jobs.length ? (
          <LoadingState>正在读取项目媒体任务</LoadingState>
        ) : state.error && !state.jobs.length ? (
          <p className="text-sm text-muted-foreground">
            媒体任务暂未能读取，请刷新重试。
          </p>
        ) : state.jobs.length === 0 ? (
          <EmptyState className="py-8">
            <h3>
              {state.offset === 0 ? "还没有媒体任务" : "这一页暂无媒体任务"}
            </h3>
            <p>
              {state.offset === 0
                ? "从项目现有的画面、配音或素材生成入口发起任务后，可在这里查看执行与费用。"
                : "返回上一页查看已有任务，或刷新项目媒体任务。"}
            </p>
          </EmptyState>
        ) : (
          <ol
            aria-label="媒体任务列表"
            className="flex min-w-0 flex-col divide-y rounded-xl border bg-background/50"
          >
            {state.jobs.map((job) => (
              <li key={job.id} className="min-w-0 p-4 sm:p-5">
                <article
                  aria-label={`媒体任务 ${job.id}`}
                  className="flex min-w-0 flex-col gap-4"
                >
                  <div className="flex flex-wrap items-start justify-between gap-3">
                    <div className="flex min-w-0 flex-col gap-1">
                      <h3 className="break-words font-semibold [overflow-wrap:anywhere]">
                        {taskName(job)}
                      </h3>
                      <p className="text-xs text-muted-foreground">
                        {job.episode_id === episodeId
                          ? "当前剧集"
                          : job.episode_id
                            ? "其他剧集"
                            : "未分集任务"}{" "}
                        · {mediaTime(job.created_at)}
                      </p>
                    </div>
                    <MediaStatusBadge job={job} />
                  </div>
                  <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">
                        供应商 / 模型
                      </dt>
                      <dd className="mt-1 break-words [overflow-wrap:anywhere]">
                        {job.provider} / {job.model}
                      </dd>
                    </div>
                    <div>
                      <dt className="text-xs text-muted-foreground">
                        尝试次数 / 修订
                      </dt>
                      <dd className="mt-1 tabular-nums">
                        {job.attempt_count} 次 / 修订 {job.revision}
                      </dd>
                    </div>
                    <div className="min-w-0">
                      <dt className="text-xs text-muted-foreground">
                        所属剧集
                      </dt>
                      <dd className="mt-1 break-words [overflow-wrap:anywhere]">
                        {job.episode_id ?? "未分集"}
                      </dd>
                    </div>
                  </dl>
                  <div className="flex min-w-0 flex-wrap items-center justify-between gap-3">
                    <p className="min-w-0 break-all text-xs text-muted-foreground">
                      任务 {job.id}
                    </p>
                    <Button
                      variant="outline"
                      size="lg"
                      aria-label={`${state.selectedId === job.id ? "收起" : "查看"}媒体任务 ${job.id}`}
                      aria-expanded={state.selectedId === job.id}
                      aria-controls={
                        state.selectedId === job.id
                          ? `media-detail-${job.id}`
                          : undefined
                      }
                      disabled={busy || state.mutating}
                      onClick={() =>
                        state.selectJob(
                          state.selectedId === job.id ? null : job.id,
                        )
                      }
                    >
                      {state.selectedId === job.id ? "收起详情" : "查看详情"}
                    </Button>
                  </div>
                  {state.selectedId === job.id && detail}
                </article>
              </li>
            ))}
          </ol>
        )}
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-xs text-muted-foreground">
            {state.jobs.length
              ? `第 ${state.offset + 1}–${state.offset + state.jobs.length} 条项目媒体任务`
              : "当前页无媒体任务"}
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              size="sm"
              aria-label="上一页媒体任务"
              disabled={listDisabled || state.offset === 0}
              onClick={() =>
                state.setOffset(Math.max(0, state.offset - MEDIA_PAGE_SIZE))
              }
            >
              上一页
            </Button>
            <Button
              variant="outline"
              size="sm"
              aria-label="下一页媒体任务"
              disabled={listDisabled || !state.hasMore}
              onClick={() => state.setOffset(state.offset + MEDIA_PAGE_SIZE)}
            >
              下一页
            </Button>
          </div>
        </div>
        {state.selectedId && !selectedInPage && detail}
      </PagePanel>
    </section>
  );
}
