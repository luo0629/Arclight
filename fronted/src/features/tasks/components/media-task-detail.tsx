"use client";

import { useState } from "react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { LoadingState, Notice } from "@/components/ui-kit";
import { CostSummary } from "@/features/projects/components/project-costs";
import type useMediaTasks from "../hooks/use-media-tasks";
import {
  MEDIA_COST_PAGE_SIZE,
  MEDIA_PAGE_SIZE,
  MEDIA_UNKNOWN_STATUSES,
} from "../media-types";
import type { MediaJob, MediaStatus } from "../media-types";
import MediaTaskRecovery from "./media-task-recovery";

type MediaState = ReturnType<typeof useMediaTasks>;
const stateLabels: Record<MediaStatus, string> = {
  CREATED: "已创建",
  SUBMITTING: "正在提交",
  SUBMITTED: "已提交",
  RUNNING: "执行中",
  SUCCEEDED: "已完成",
  FAILED: "失败",
  CANCEL_REQUESTED: "已请求取消",
  CANCELLED: "已取消",
  RETRY_WAIT: "等待重试",
  RESULT_UNKNOWN: "结果未知",
  RECONCILING: "正在核验",
  MANUAL_REVIEW: "待人工核验",
};
const categoryLabels: Record<string, string> = {
  INPUT_INVALID: "输入无效",
  PROVIDER_REJECTED: "供应商拒绝",
  PROVIDER_FAILED: "供应商执行失败",
  PROVIDER_UNAVAILABLE: "供应商不可用",
  STORAGE_FAILED: "文件存储失败",
  BUDGET_EXCEEDED: "预算超限",
  QUALITY_FAILED: "质量未通过",
  TRANSIENT: "暂时故障",
  PERMANENT: "不可自动重试",
  UNKNOWN: "结果不确定",
  INPUT: "输入问题",
  PROVIDER: "供应商问题",
  SERVICE: "供应商服务故障",
  UNCERTAIN: "结果不确定",
  CANCELLATION: "取消请求",
  SCHEDULING: "调度问题",
  STORAGE: "存储问题",
  BUDGET: "预算问题",
  QUALITY: "质量问题",
};
export const mediaTime = (value: string | null) =>
  value ? new Date(value).toLocaleString("zh-CN") : "未知";
const money = (value: string | null) =>
  value === null ? "未知" : `${value} USD`;
const category = (value: string | null) =>
  value ? (categoryLabels[value] ?? value) : "未分类";

export function MediaStatusBadge({
  job,
}: {
  job: Pick<MediaJob, "status" | "status_label">;
}) {
  return (
    <Badge
      variant="outline"
      className={
        job.status === "FAILED"
          ? "border-destructive/30 text-destructive"
          : MEDIA_UNKNOWN_STATUSES.includes(job.status)
            ? "bg-secondary"
            : ""
      }
    >
      {job.status_label || stateLabels[job.status]}
    </Badge>
  );
}

function Data({
  label,
  children,
}: {
  label: string;
  children: React.ReactNode;
}) {
  return (
    <div className="min-w-0">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="mt-1 min-w-0 break-words [overflow-wrap:anywhere]">
        {children}
      </dd>
    </div>
  );
}

function Pages({
  label,
  offset,
  count,
  more,
  disabled,
  onChange,
  size = MEDIA_PAGE_SIZE,
}: {
  label: string;
  offset: number;
  count: number;
  more: boolean;
  disabled: boolean;
  onChange: (value: number) => void;
  size?: number;
}) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-3 text-xs text-muted-foreground">
      <p>{count ? `第 ${offset + 1}–${offset + count} 条` : "本页暂无记录"}</p>
      <div className="flex flex-wrap gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`上一页${label}`}
          disabled={disabled || offset === 0}
          onClick={() => onChange(Math.max(0, offset - size))}
        >
          上一页
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          aria-label={`下一页${label}`}
          disabled={disabled || !more}
          onClick={() => onChange(offset + size)}
        >
          下一页
        </Button>
      </div>
    </div>
  );
}

export default function MediaTaskDetail({
  state,
  selectedId,
  owner,
  writable,
  busy,
}: {
  state: MediaState;
  selectedId: string;
  owner: boolean;
  writable: boolean;
  busy: boolean;
}) {
  const [recovery, setRecovery] = useState<"resolve" | "replay" | null>(null);
  const { detail, detailLoading, detailError } = state;
  if (!detail || detail.job.id !== selectedId)
    return (
      <section
        id={`media-detail-${selectedId}`}
        aria-label={`媒体任务详情 ${selectedId}`}
        className="min-w-0 border-t pt-4"
      >
        {detailError ? (
          <Notice tone="error">{detailError}</Notice>
        ) : (
          <LoadingState>正在读取媒体任务详情</LoadingState>
        )}
      </section>
    );
  const { job, latestAttempt } = detail;
  const disabled =
    busy || state.mutating || !state.detailFresh || detailLoading;
  const readingDisabled = busy || state.mutating || detailLoading;
  const unknown = MEDIA_UNKNOWN_STATUSES.includes(job.status);
  const retryable =
    job.status === "FAILED" &&
    !job.cancel_requested_at &&
    latestAttempt?.status === "FAILED" &&
    !!latestAttempt.finished_at;
  const replayable =
    ["FAILED", "CANCELLED"].includes(job.status) &&
    !!detail.deadLetter &&
    !latestAttempt?.isContinuityRevision;
  const cancelable =
    !["SUCCEEDED", "CANCELLED", "CANCEL_REQUESTED"].includes(job.status) &&
    !job.cancel_requested_at;
  const errorLabels: Record<string, string> = {
    attempts: "尝试记录",
    latestAttempt: "最新尝试",
    schedule: "调度信息",
    deadLetter: "死信信息",
    lane: "所属队列",
    calls: "供应商调用",
    costs: "费用汇总",
    costEntries: "费用明细",
  };

  return (
    <section
      id={`media-detail-${job.id}`}
      aria-label={`媒体任务详情 ${job.id}`}
      aria-busy={detailLoading || state.mutating}
      className="flex min-w-0 flex-col gap-5 border-t pt-5"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-semibold">任务详情</h4>
        <div className="flex flex-wrap gap-2 text-xs text-muted-foreground">
          <output aria-label="媒体任务修订">修订 {job.revision}</output>
          <span>更新于 {mediaTime(job.updated_at)}</span>
        </div>
      </div>
      {detailError && <Notice tone="error">{detailError}</Notice>}
      {!state.detailFresh && (
        <Notice>当前详情尚未确认最新状态，刷新成功后可继续操作。</Notice>
      )}
      {Object.entries(detail.errors).map(([key, error]) => (
        <Notice key={key} tone="error">
          {errorLabels[key] ?? "部分详情"}：{error}
        </Notice>
      ))}
      <dl className="grid min-w-0 gap-x-6 gap-y-4 text-sm sm:grid-cols-2 lg:grid-cols-3">
        <Data label="任务状态">
          <output aria-label="媒体任务状态">
            <MediaStatusBadge job={job} />
          </output>
        </Data>
        <Data label="历史供应商 / 模型">
          {job.provider} / {job.model}
        </Data>
        <Data label="目标类型">{job.target_type}</Data>
        <Data label="目标资产">{job.target_asset_id ?? "尚未登记"}</Data>
        <Data label="输入清单">{job.input_manifest_id}</Data>
        <Data label="尝试总数">{job.attempt_count}</Data>
        <Data label="取消意图">
          <output aria-label="媒体任务取消请求">
            {job.cancel_requested_at ? "已请求取消" : "未请求取消"}
          </output>
        </Data>
        <Data label="所属队列">
          {detail.lane
            ? `${detail.lane.lane === "INTERACTIVE" ? "交互队列" : detail.lane.lane === "BATCH" ? "批量队列" : detail.lane.lane}（${detail.lane.origin}）`
            : detail.errors.lane
              ? "未取得队列信息"
              : "暂无队列记录"}
        </Data>
        <Data label="下次可尝试时间">
          {detail.schedule
            ? mediaTime(detail.schedule.next_attempt_at)
            : detail.errors.schedule
              ? "未取得调度信息"
              : "暂无调度记录"}
        </Data>
        <Data label="执行期限">
          {detail.schedule
            ? mediaTime(detail.schedule.deadline_at)
            : detail.errors.schedule
              ? "未取得调度信息"
              : "暂无调度记录"}
        </Data>
        {detail.schedule?.blocked_code && (
          <Data label="调度阻塞">{category(detail.schedule.blocked_code)}</Data>
        )}
        {detail.schedule?.policy.max_attempts !== undefined && (
          <Data label="最大尝试次数">
            {detail.schedule.policy.max_attempts}
          </Data>
        )}
        {detail.schedule?.policy.max_elapsed_seconds !== undefined && (
          <Data label="最大执行时限">
            {detail.schedule.policy.max_elapsed_seconds} 秒
          </Data>
        )}
      </dl>
      {detail.deadLetter && (
        <Notice>
          <strong>任务已进入死信</strong>
          <p>
            原因：{category(detail.deadLetter.reason)}；类别：
            {category(detail.deadLetter.category)}
            {detail.deadLetter.error_code
              ? `；错误：${category(detail.deadLetter.error_code)}`
              : ""}
            。
          </p>
        </Notice>
      )}
      {job.cancel_requested_at && (
        <Notice>
          已于 {mediaTime(job.cancel_requested_at)}{" "}
          请求取消。供应商已接收的调用仍可能继续，最终结果以任务状态和核验记录为准。
        </Notice>
      )}
      {unknown && (
        <Notice>
          供应商结果尚未确认。请先核验结果，避免重复提交和重复计费。
        </Notice>
      )}
      {state.pendingReplay && owner && (
        <Notice>
          上次重新发起的结果尚未确认。打开恢复表单可沿用原命令核验同一新任务。
        </Notice>
      )}
      {writable && (
        <div className="flex flex-wrap gap-2">
          {retryable && (
            <Button
              size="lg"
              disabled={disabled}
              onClick={() => void state.mutate("retry")}
            >
              重试媒体任务
            </Button>
          )}
          {cancelable && (
            <Button
              variant="outline"
              size="lg"
              disabled={disabled}
              onClick={() => {
                if (
                  window.confirm(
                    "请求取消媒体任务？供应商已接收的调用仍需核验结果。",
                  )
                )
                  void state.mutate("cancel");
              }}
            >
              请求取消媒体任务
            </Button>
          )}
          {unknown && (
            <Button
              variant="outline"
              size="lg"
              disabled={disabled}
              onClick={() => void state.mutate("reconcile")}
            >
              核验供应商结果
            </Button>
          )}
          {owner && unknown && latestAttempt && !latestAttempt.finished_at && (
            <Button
              variant="outline"
              size="lg"
              disabled={disabled}
              onClick={() =>
                setRecovery(recovery === "resolve" ? null : "resolve")
              }
            >
              人工确认结果
            </Button>
          )}
          {owner && replayable && (
            <Button
              variant="outline"
              size="lg"
              disabled={disabled}
              onClick={() =>
                setRecovery(recovery === "replay" ? null : "replay")
              }
            >
              从死信重新发起
            </Button>
          )}
        </div>
      )}
      {recovery &&
        writable &&
        owner &&
        ((recovery === "resolve" &&
          unknown &&
          latestAttempt &&
          !latestAttempt.finished_at) ||
          (recovery === "replay" && replayable)) && (
          <MediaTaskRecovery
            key={`${job.id}:${job.revision}:${recovery}`}
            mode={recovery}
            disabled={disabled}
            batchSize={detail.batchSize}
            pendingReplay={state.pendingReplay}
            onSubmit={state.mutate}
            onClose={() => setRecovery(null)}
          />
        )}
      <section
        aria-label="媒体尝试记录"
        className="flex min-w-0 flex-col gap-3 border-t pt-4"
      >
        <h4 className="font-semibold">尝试记录</h4>
        {!detail.attempts.length && (
          <p className="text-sm text-muted-foreground">
            {detail.errors.attempts
              ? "尝试记录未能读取，请刷新重试。"
              : "尚无尝试记录。"}
          </p>
        )}
        <ol className="flex min-w-0 flex-col divide-y">
          {detail.attempts.map((attempt) => (
            <li
              key={attempt.id}
              aria-label={`尝试 ${attempt.attempt_no}`}
              className="flex min-w-0 flex-col gap-3 py-4 first:pt-0"
            >
              <div className="flex flex-wrap items-center justify-between gap-2">
                <strong className="text-sm">
                  第 {attempt.attempt_no} 次尝试
                </strong>
                <MediaStatusBadge
                  job={{ status: attempt.status, status_label: "" }}
                />
              </div>
              <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-2 lg:grid-cols-3">
                <Data label="供应商 / 模型">
                  {attempt.provider} / {attempt.model}
                </Data>
                <Data label="开始时间">{mediaTime(attempt.submitted_at)}</Data>
                <Data label="结束时间">
                  {attempt.finished_at
                    ? mediaTime(attempt.finished_at)
                    : "尚未结束"}
                </Data>
                <Data label="耗时">
                  {attempt.latency_ms === null
                    ? "未知"
                    : `${attempt.latency_ms} 毫秒`}
                </Data>
                <Data label="错误类别">{category(attempt.error_category)}</Data>
                <Data label="错误代码">
                  {attempt.error_code ? category(attempt.error_code) : "无"}
                </Data>
              </dl>
              {!!attempt.outputHandles.length && (
                <div className="min-w-0 text-sm">
                  <p className="text-muted-foreground">输出对象引用</p>
                  <ul className="mt-1 flex min-w-0 flex-col gap-1">
                    {attempt.outputHandles.map((handle, index) => (
                      <li
                        key={`${index}:${handle}`}
                        className="break-words [overflow-wrap:anywhere]"
                      >
                        {handle}
                      </li>
                    ))}
                  </ul>
                </div>
              )}
            </li>
          ))}
        </ol>
        <Pages
          label="尝试记录"
          offset={state.attemptOffset}
          count={detail.attempts.length}
          more={detail.attempts.length === MEDIA_PAGE_SIZE}
          disabled={readingDisabled}
          onChange={state.setAttemptOffset}
        />
      </section>
      <section
        aria-label="媒体供应商调用"
        className="flex min-w-0 flex-col gap-3 border-t pt-4"
      >
        <h4 className="font-semibold">供应商调用</h4>
        <p className="text-xs text-muted-foreground">
          调用记录按开始时间从早到晚显示。
        </p>
        {!detail.calls.length && (
          <p className="text-sm text-muted-foreground">
            {detail.errors.calls
              ? "调用记录未能读取，请刷新重试。"
              : "尚无调用记录。"}
          </p>
        )}
        <ol className="flex min-w-0 flex-col divide-y">
          {detail.calls.map((call) => (
            <li
              key={call.id}
              className="grid min-w-0 gap-2 py-3 text-sm sm:grid-cols-2"
            >
              <p className="break-words [overflow-wrap:anywhere]">
                {call.kind} · {call.outcome ?? "结果待确认"}
              </p>
              <p className="text-muted-foreground">
                {mediaTime(call.started_at)}
                {call.completed_at
                  ? ` 至 ${mediaTime(call.completed_at)}`
                  : "，尚未完成"}
              </p>
            </li>
          ))}
        </ol>
        <Pages
          label="供应商调用"
          offset={state.callOffset}
          count={detail.calls.length}
          more={detail.calls.length === MEDIA_PAGE_SIZE}
          disabled={readingDisabled}
          onChange={state.setCallOffset}
        />
      </section>
      <section
        aria-label="媒体任务费用"
        className="flex min-w-0 flex-col gap-4 border-t pt-4"
      >
        <h4 className="font-semibold">任务费用</h4>
        {detail.costs ? (
          <CostSummary value={detail.costs} />
        ) : (
          <p className="text-sm text-muted-foreground">
            尚无可用的费用汇总；未取得账单不代表费用为零。
          </p>
        )}
        {detail.costEntries && (
          <>
            <p className="text-xs text-muted-foreground">
              费用明细共 {detail.costEntries.total}{" "}
              条。金额来自费用账本，预留与实际费用分别展示。
            </p>
            <ol className="flex min-w-0 flex-col divide-y">
              {detail.costEntries.items.map((entry) => (
                <li
                  key={entry.id}
                  className="flex min-w-0 flex-col gap-3 py-4 first:pt-0"
                >
                  <strong className="break-words text-sm [overflow-wrap:anywhere]">
                    第 {entry.attempt_no} 次尝试 · {entry.provider} /{" "}
                    {entry.model}
                  </strong>
                  <dl className="grid min-w-0 gap-3 text-sm sm:grid-cols-3">
                    <Data label="实际费用">{money(entry.actual_cost)}</Data>
                    <Data label="报价估算">{money(entry.estimated_cost)}</Data>
                    <Data label="待核账预留">
                      {money(entry.pending_estimate)}
                    </Data>
                    <Data label="核账状态">
                      {entry.cost_is_estimate ? "估算 / 待核账" : "已核账"}
                    </Data>
                    {entry.attempt_id && (
                      <Data label="尝试标识">{entry.attempt_id}</Data>
                    )}
                    {entry.call_id && (
                      <Data label="调用标识">{entry.call_id}</Data>
                    )}
                  </dl>
                </li>
              ))}
            </ol>
            <Pages
              label="任务费用"
              offset={state.costOffset}
              count={detail.costEntries.items.length}
              more={
                state.costOffset + MEDIA_COST_PAGE_SIZE <
                detail.costEntries.total
              }
              disabled={readingDisabled}
              onChange={state.setCostOffset}
              size={MEDIA_COST_PAGE_SIZE}
            />
          </>
        )}
      </section>
    </section>
  );
}
