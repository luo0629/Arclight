"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { useEffect, useState, type FormEvent } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  EmptyState,
  FormLabel,
  LoadingState,
  Notice,
  PagePanel,
  StatusBadge,
} from "@/components/ui-kit";
import { isResourceId, productionTaskPath } from "@/lib/routes";
import { useProject } from "../project-context";
import useProductionList from "../hooks/use-production-list";
import type { ProductionTask } from "../production-types";
import useMediaTasks from "@/features/tasks/hooks/use-media-tasks";
import MediaTaskDetail, {
  mediaTime,
} from "@/features/tasks/components/media-task-detail";

const taskTypes = {
  TEXT: "文字创作",
  IMAGE: "图片生成",
  AUDIO: "音频生成",
  VIDEO: "视频生成",
  RENDER: "渲染",
  OTHER: "其他任务",
};
const statuses: Record<string, string> = {
  QUEUED: "排队中",
  CREATED: "已创建",
  SUBMITTING: "正在提交",
  SUBMITTED: "已提交",
  RUNNING: "执行中",
  WAITING_REVIEW: "等待审核",
  SUCCEEDED: "已完成",
  REJECTED: "已驳回",
  FAILED: "失败",
  CANCELLED: "已取消",
  CANCEL_REQUESTED: "已请求取消",
  RETRY_WAIT: "等待重试",
  RESULT_UNKNOWN: "结果未知",
  RECONCILING: "正在核验",
  MANUAL_REVIEW: "待人工核验",
  UNKNOWN: "状态未知",
};
const errorLabels: Record<string, string> = {
  INPUT_INVALID: "输入无效",
  PROVIDER_REJECTED: "供应商拒绝",
  PROVIDER_FAILED: "供应商执行失败",
  PROVIDER_UNAVAILABLE: "供应商不可用",
  RESULT_UNKNOWN: "结果不确定",
  STORAGE_FAILED: "文件存储失败",
  CANCELLED: "任务取消",
  BUDGET_EXCEEDED: "预算超限",
  QUALITY_FAILED: "质量未通过",
  RENDER_FAILED: "渲染失败",
  RENDER_TIMEOUT: "渲染超时",
  RENDER_INTERRUPTED: "渲染中断",
  RENDER_INPUT_INVALID: "渲染输入无效",
  RENDER_OUTPUT_INVALID: "渲染输出无效",
  INSPECTION_FAILED: "机器检查失败",
  WORKFLOW_FAILED: "文字工作流失败",
  INTERNAL_ERROR: "内部故障",
};
const initialFilters = {
  episode: "all",
  type: "all",
  status: "all",
  from: "",
  to: "",
  error: "all",
};
const options = (labels: Record<string, string>) => [
  { value: "all", label: "全部" },
  ...Object.entries(labels).map(([value, label]) => ({ value, label })),
];

/** Reuses UI-05's exact task read, CAS, reconciliation and recovery controls. */
function ProjectMediaDetail({ jobId }: { jobId: string }) {
  const { project, owner, writable } = useProject();
  const state = useMediaTasks({
    projectId: project.id,
    scopeKey: `project-detail:${jobId}`,
    owner,
    writable,
  });
  const selectJob = state.selectJob;
  useEffect(() => selectJob(jobId), [jobId, selectJob]);
  return (
    <MediaTaskDetail
      state={state}
      selectedId={jobId}
      owner={owner}
      writable={writable}
      busy={false}
    />
  );
}

export default function ProductionTasks() {
  const { project, episodes, episodeList, writable } = useProject();
  const [filters, setFilters] = useState(initialFilters);
  const [query, setQuery] = useState("");
  const [validation, setValidation] = useState("");
  const state = useProductionList<ProductionTask>({
    projectId: project.id,
    resource: "tasks",
    query,
  });
  const selected = useSearchParams().get("generation_job");
  const jobId = selected && isResourceId(selected) ? selected : null;
  const field = (key: keyof typeof filters, value: string) =>
    setFilters((previous) => ({ ...previous, [key]: value }));
  function apply(event: FormEvent) {
    event.preventDefault();
    const from = filters.from ? new Date(filters.from) : null;
    const to = filters.to ? new Date(filters.to) : null;
    if (
      (from && !Number.isFinite(from.getTime())) ||
      (to && !Number.isFinite(to.getTime())) ||
      (from && to && from >= to)
    ) {
      setValidation("结束时间必须晚于开始时间，请检查筛选范围。");
      return;
    }
    const params = new URLSearchParams();
    if (filters.episode !== "all") params.set("episode_id", filters.episode);
    if (filters.type !== "all") params.set("task_type", filters.type);
    if (filters.status !== "all") params.set("status", filters.status);
    if (from) params.set("created_from", from.toISOString());
    if (to) params.set("created_to", to.toISOString());
    if (filters.error !== "all") params.set("error_code", filters.error);
    setValidation("");
    state.setOffset(0);
    setQuery(params.toString());
    if (params.toString() === query) state.refresh();
  }
  function reset() {
    setFilters(initialFilters);
    setValidation("");
    state.setOffset(0);
    setQuery("");
    if (!query) state.refresh();
  }
  return (
    <section aria-label="项目生产任务" className="min-w-0">
      <PagePanel
        title="生产任务"
        description="汇总当前项目的文字工作流、媒体生成与渲染任务。打开详情可查看原任务的记录及可用动作。"
      >
        <form onSubmit={apply} className="flex min-w-0 flex-col gap-4">
          <p className="break-words text-sm [overflow-wrap:anywhere]">
            项目：{project.name}。切换工作区项目可查看对应任务。
          </p>
          <div className="grid min-w-0 gap-4 sm:grid-cols-2 xl:grid-cols-3">
            <FormLabel>
              剧集
              <ChoiceSelect
                aria-label="剧集"
                value={filters.episode}
                onValueChange={(value) => field("episode", value)}
                options={[
                  { value: "all", label: "全部剧集与未分集任务" },
                  ...episodes.map((episode) => ({
                    value: episode.id,
                    label: `第 ${episode.episode_no} 集 ${episode.title}${episode.status === "ARCHIVED" ? "（已归档）" : ""}`,
                  })),
                ]}
              />
            </FormLabel>
            <FormLabel>
              任务类型
              <ChoiceSelect
                aria-label="任务类型"
                value={filters.type}
                onValueChange={(value) => field("type", value)}
                options={options(taskTypes)}
              />
            </FormLabel>
            <FormLabel>
              任务状态
              <ChoiceSelect
                aria-label="任务状态"
                value={filters.status}
                onValueChange={(value) => field("status", value)}
                options={options(statuses)}
              />
            </FormLabel>
            <FormLabel>
              开始时间
              <Input
                aria-label="开始时间"
                type="datetime-local"
                value={filters.from}
                onChange={(event) => field("from", event.target.value)}
                className="min-w-0"
              />
            </FormLabel>
            <FormLabel>
              结束时间
              <Input
                aria-label="结束时间"
                type="datetime-local"
                value={filters.to}
                onChange={(event) => field("to", event.target.value)}
                className="min-w-0"
              />
            </FormLabel>
            <FormLabel>
              错误类别
              <ChoiceSelect
                aria-label="错误类别"
                value={filters.error}
                onValueChange={(value) => field("error", value)}
                options={options(errorLabels)}
              />
            </FormLabel>
          </div>
          {episodeList.hasMore && (
            <Button
              type="button"
              variant="outline"
              className="self-start"
              disabled={episodeList.loading}
              onClick={() => void episodeList.more().catch(() => {})}
            >
              加载更多剧集选项
            </Button>
          )}
          {episodeList.error && (
            <Notice tone="error">剧集选项暂未完整读取，请重新加载剧集。</Notice>
          )}
          <p className="text-xs leading-relaxed text-muted-foreground">
            使用本地时间输入，包含开始时刻，不包含结束时刻。按创建时间从新到旧排列，同一时刻按任务来源和编号稳定排序。错误类别为安全摘要。
          </p>
          {validation && <Notice tone="error">{validation}</Notice>}
          <div className="flex flex-wrap gap-2">
            <Button type="submit">应用筛选</Button>
            <Button type="button" variant="outline" onClick={reset}>
              重置筛选
            </Button>
            <Button
              type="button"
              variant="outline"
              onClick={state.refresh}
              disabled={state.loading}
            >
              刷新任务
            </Button>
          </div>
        </form>
        {!writable && (
          <p className="text-sm text-muted-foreground">
            当前为只读状态，可查看生产记录；任务动作由原详情与服务端权限控制。
          </p>
        )}
        {state.error && <Notice tone="error">{state.error}</Notice>}
        <div aria-label="生产任务列表" aria-busy={state.loading}>
          {state.loading ? (
            <LoadingState>正在读取生产任务</LoadingState>
          ) : !state.error && !state.items.length ? (
            <EmptyState>
              <h3>没有符合条件的任务</h3>
              <p>可以调整筛选条件，或从剧集工作区发起生产。</p>
            </EmptyState>
          ) : (
            <div className="flex min-w-0 flex-col divide-y">
              {state.items.map((task) => {
                const path = productionTaskPath(task);
                return (
                  <article
                    key={`${task.family}:${task.id}`}
                    aria-label={`生产任务 ${task.id}`}
                    className="flex min-w-0 flex-col gap-3 py-5 first:pt-0"
                  >
                    <div className="flex flex-wrap items-center justify-between gap-2">
                      <h3 className="font-semibold">
                        {taskTypes[task.task_type] ?? "生产任务"}
                      </h3>
                      <span aria-label="任务状态摘要">
                        <StatusBadge status={task.status} />
                      </span>
                    </div>
                    <p className="break-words text-sm [overflow-wrap:anywhere]">
                      {task.episode_id
                        ? `第 ${task.episode_no ?? "?"} 集 ${task.episode_title ?? ""}`
                        : "未分集任务"}
                    </p>
                    <p className="break-all text-xs text-muted-foreground">
                      任务 {task.id}
                    </p>
                    <p className="text-xs leading-relaxed text-muted-foreground">
                      创建于 {mediaTime(task.created_at)}；更新于{" "}
                      {mediaTime(task.updated_at)}
                    </p>
                    {task.error_code && (
                      <p className="text-sm text-destructive">
                        错误摘要：{errorLabels[task.error_code] ?? "内部故障"}
                      </p>
                    )}
                    {path ? (
                      <Button asChild variant="outline" className="self-start">
                        <Link
                          href={path}
                          aria-label={`查看任务详情 ${task.id}`}
                        >
                          查看任务详情
                        </Link>
                      </Button>
                    ) : (
                      <Notice>此历史任务缺少可访问的剧集入口。</Notice>
                    )}
                  </article>
                );
              })}
            </div>
          )}
        </div>
        <div className="flex flex-wrap items-center justify-between gap-3">
          <p className="text-sm text-muted-foreground" role="status">
            共 {state.total} 项；第 {Math.floor(state.offset / state.limit) + 1}{" "}
            页
          </p>
          <div className="flex gap-2">
            <Button
              variant="outline"
              disabled={state.loading || state.offset === 0}
              onClick={() => state.setOffset(state.offset - state.limit)}
            >
              上一页
            </Button>
            <Button
              variant="outline"
              disabled={
                state.loading || state.offset + state.limit >= state.total
              }
              onClick={() => state.setOffset(state.offset + state.limit)}
            >
              下一页
            </Button>
          </div>
        </div>
      </PagePanel>
      {jobId && !state.error && !state.loading && (
        <ProjectMediaDetail key={`${project.id}:${jobId}`} jobId={jobId} />
      )}
    </section>
  );
}
