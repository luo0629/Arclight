"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { isResourceId } from "@/lib/routes";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { ApiError } from "@/lib/api/client";
import { renderingApi } from "../api";
import type { FormalExportAction, RenderJob, RenderManifest } from "../types";
import { matchesExportReview } from "../formal-export-intent";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import { cleanupLabel, date, renderLabel } from "./render-format";

const idOf = (row: { id: string }) => row.id;
const activeStatuses = ["QUEUED", "RUNNING", "CANCEL_REQUESTED"];

export default function RenderJobs({
  manifest,
  owner,
  writable,
  unavailable,
  refresh,
  onRendered,
  onExportFinished,
  onExportRetry,
  initialJob,
}: {
  manifest: RenderManifest;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  refresh: number;
  onRendered: () => void;
  onExportFinished: (manifestId: string) => void;
  onExportRetry: (manifestId: string) => void;
  initialJob?: RenderJob;
}) {
  const fetchJobs = useCallback(
    (cursor: string | undefined, signal: AbortSignal) =>
      renderingApi.listJobs(manifest.id, cursor, signal),
    [manifest.id],
  );
  const jobs = useRenderList<RenderJob, string>(
    manifest.id,
    fetchJobs,
    idOf,
    unavailable,
  );
  useEffect(() => {
    if (refresh) void jobs.load();
  }, [refresh, jobs.load]);
  const [selectedId, setSelectedId] = useState("");
  const [pinnedJob, setPinnedJob] = useState(initialJob);
  const jobRows = pinnedJob
    ? [
        jobs.rows.find(
          (row) =>
            row.id === pinnedJob.id && row.revision >= pinnedJob.revision,
        ) ?? pinnedJob,
        ...jobs.rows.filter((row) => row.id !== pinnedJob.id),
      ]
    : jobs.rows;
  const selected = selectedId
    ? jobRows.find((job) => job.id === selectedId)
    : jobRows[0];
  const replaceJob = useCallback(
    (row: RenderJob) => {
      setPinnedJob((current) =>
        current?.id === row.id && row.revision >= current.revision
          ? row
          : current,
      );
      jobs.replace(row);
    },
    [jobs.replace],
  );
  const mutation = useRenderMutation(manifest.id, writable, unavailable);
  const formal = manifest.snapshot.output.name === "FORMAL";
  const search = useSearchParams();
  const requestedReview = search.get("review");
  const pinnedReview =
    requestedReview &&
    isResourceId(requestedReview) &&
    (search.get("render_manifest") === manifest.id ||
      initialJob?.manifest_id === manifest.id)
      ? requestedReview
      : null;
  const readReview = useCallback(
    async (signal: AbortSignal) => {
      if (pinnedReview) {
        const next = await renderingApi.getHumanReview(pinnedReview, signal);
        if (next.id !== pinnedReview || !matchesExportReview(next, manifest))
          throw new Error("审核请求与当前渲染清单不一致。");
        return next;
      }
      const rows = await renderingApi.listManifestReviews(
        manifest.project_id,
        manifest.id,
        signal,
      );
      return rows.find((next) => matchesExportReview(next, manifest)) ?? null;
    },
    [manifest, pinnedReview],
  );
  const reviewState = useRenderDetail(
    manifest.id,
    readReview,
    unavailable,
    formal,
  );
  const review = reviewState.data;
  const seen = useRef(new Set<string>());
  const pollGeneration = useRef(0);
  const rowsRef = useRef(jobs.rows);
  rowsRef.current = jobRows;
  useEffect(() => {
    for (const row of jobRows)
      if (row.status === "SUCCEEDED" && !seen.current.has(row.id)) {
        seen.current.add(row.id);
        onRendered();
      }
  }, [jobRows, onRendered]);
  const hasActive = jobRows.some((job) => activeStatuses.includes(job.status));
  const rejected = review?.decision?.decision_json.decision === "REJECT";
  const approved = review?.decision?.decision_json.decision === "APPROVE";
  useEffect(() => {
    if (
      formal &&
      !mutation.busy &&
      !jobs.loading &&
      (rejected || (!hasActive && jobRows.length > 0))
    ) {
      onExportFinished(manifest.id);
    }
  }, [
    formal,
    hasActive,
    jobRows.length,
    jobs.loading,
    manifest.id,
    mutation.busy,
    onExportFinished,
    rejected,
  ]);
  useEffect(() => {
    if (!hasActive) return;
    const controller = new AbortController();
    let polling = false;
    const poll = async () => {
      if (polling || document.hidden) return;
      polling = true;
      const generation = pollGeneration.current;
      try {
        for (const row of rowsRef.current.filter((job) =>
          activeStatuses.includes(job.status),
        )) {
          const next = await renderingApi.getJob(row.id, controller.signal);
          if (
            controller.signal.aborted ||
            generation !== pollGeneration.current
          )
            return;
          const current = rowsRef.current.find((job) => job.id === next.id);
          if (current && next.revision < current.revision) continue;
          replaceJob(next);
        }
      } catch (cause) {
        if (!controller.signal.aborted) {
          unavailable(cause);
          void jobs.load();
        }
      } finally {
        polling = false;
      }
    };
    const timer = window.setInterval(() => void poll(), 3000);
    document.addEventListener("visibilitychange", poll);
    return () => {
      controller.abort();
      window.clearInterval(timer);
      document.removeEventListener("visibilitychange", poll);
    };
  }, [hasActive, manifest.id, replaceJob, jobs.load, unavailable]);
  async function submitPreview(retryOf?: string) {
    if (formal) return;
    const pending = pendingCreationKey(
      `job:${manifest.id}:${retryOf ?? "new"}`,
    );
    await mutation.mutate(
      async (signal) => {
        const job = await renderingApi.createJob(
          manifest.id,
          {
            creation_key: pending.key,
            ...(retryOf ? { retry_of: retryOf } : {}),
          },
          signal,
        );
        pending.clear();
        return job;
      },
      (job) => {
        jobs.prepend(job);
        setSelectedId(job.id);
      },
      "渲染已提交。刷新页面后仍可从本清单的任务记录继续查看。",
    );
  }
  const canActOnFormal =
    writable && !!review && !review.automatic_production_id;
  const canResumeFormal =
    canActOnFormal && approved && (owner || !!review?.resume);
  async function performFormal(body: FormalExportAction) {
    if (!canActOnFormal || !review) return;
    if (body.action === "APPROVE" || body.action === "REJECT") {
      if (!owner) return;
    } else if (!canResumeFormal) return;
    await mutation.mutate(
      async (signal) => {
        if (body.action === "RETRY") onExportRetry(manifest.id);
        try {
          const result = await renderingApi.performFormalExport(
            manifest.id,
            body,
            signal,
          );
          if (
            result.review.id !== review.id ||
            result.review.request_sha256 !== review.request_sha256 ||
            !matchesExportReview(result.review, manifest) ||
            (result.job &&
              (result.job.manifest_id !== manifest.id ||
                result.job.manifest_sha256 !== manifest.content_sha256 ||
                result.job.profile !== "FORMAL"))
          )
            throw new Error("导出结果与当前渲染清单不一致。");
          return result;
        } catch (cause) {
          if (
            !signal.aborted &&
            !(
              cause instanceof ApiError &&
              [401, 403, 404].includes(cause.status)
            )
          ) {
            if (
              cause instanceof ApiError &&
              ["RENDER_INPUT_CHANGED", "REVIEW_INPUT_CHANGED"].includes(
                cause.code,
              )
            ) {
              onExportFinished(manifest.id);
            }
            reviewState.refresh();
            void jobs.load();
          }
          throw cause;
        }
      },
      (result) => {
        reviewState.setData(result.review);
        if (result.job) {
          jobs.prepend(result.job);
          setSelectedId(result.job.id);
        }
      },
      body.action === "REJECT"
        ? "已拒绝本次导出。"
        : "已提交导出，正在等待渲染。",
    );
  }
  async function cancel(job: RenderJob) {
    ++pollGeneration.current;
    await mutation.mutate(
      (signal) => renderingApi.cancelJob(job.id, job.revision, signal),
      replaceJob,
      "取消请求已保存，执行器会在安全边界停止并记录清理状态。",
    );
  }
  const canDecide =
    owner &&
    canActOnFormal &&
    review?.status === "WAITING_REVIEW" &&
    !review.decision;
  const canContinue =
    canResumeFormal &&
    !review?.blocking_code &&
    !jobs.loading &&
    !jobs.error &&
    jobRows.length === 0;
  return (
    <section aria-label="渲染任务" className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        {!formal && (
          <Button
            disabled={!writable || mutation.busy}
            onClick={() => void submitPreview()}
          >
            提交渲染
          </Button>
        )}
        <Button
          variant="ghost"
          disabled={jobs.loading || mutation.busy || reviewState.loading}
          onClick={() => {
            void jobs.load();
            if (formal) reviewState.refresh();
          }}
        >
          刷新渲染任务
        </Button>
      </div>
      {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
      {reviewState.error && <Notice tone="error">{reviewState.error}</Notice>}
      {review && !review.automatic_production_id && !jobRows.length && (
        <section
          aria-label="正式导出审核"
          className="flex flex-col gap-3 rounded-xl border p-4"
        >
          {rejected ? (
            <p>本次导出已拒绝。</p>
          ) : (
            <>
              <h4>{approved ? "本次导出已确认" : "等待项目所有者确认导出"}</h4>
              {review.blocking_code && (
                <Notice tone="error">
                  本次导出受阻：{review.blocking_code}。
                </Notice>
              )}
              {canDecide && (
                <div className="flex flex-wrap gap-2">
                  <Button
                    disabled={mutation.busy || !!review.blocking_code}
                    onClick={() =>
                      void performFormal({
                        action: "APPROVE",
                        request_sha256: review.request_sha256,
                      })
                    }
                  >
                    确认并导出 MP4
                  </Button>
                  <Button
                    variant="outline"
                    disabled={mutation.busy}
                    onClick={() =>
                      void performFormal({
                        action: "REJECT",
                        request_sha256: review.request_sha256,
                      })
                    }
                  >
                    拒绝本次导出
                  </Button>
                </div>
              )}
              {canContinue && (
                <Button
                  className="self-start"
                  disabled={mutation.busy}
                  onClick={() =>
                    void performFormal({
                      action: "CONTINUE",
                      request_sha256: review.request_sha256,
                    })
                  }
                >
                  继续提交本次导出
                </Button>
              )}
            </>
          )}
        </section>
      )}
      <h4>本清单的渲染记录</h4>
      {selectedId && !selected && !jobs.loading && jobRows.length > 0 && (
        <Notice>所选任务不在当前页，请加载更多或重新选择任务。</Notice>
      )}
      {jobs.error && <Notice tone="error">{jobs.error}</Notice>}
      {jobs.loading && jobRows.length === 0 && (
        <p role="status">正在读取渲染任务…</p>
      )}
      {!jobs.loading && !jobs.error && jobRows.length === 0 && (
        <p className="text-sm text-muted-foreground">本清单还没有渲染任务。</p>
      )}
      <div className="flex min-w-0 flex-col gap-2">
        {jobRows.map((job, index) => (
          <Button
            key={job.id}
            variant="outline"
            className="h-auto min-w-0 flex-wrap justify-between gap-2 px-3 py-3 text-left whitespace-normal"
            aria-label={`查看第 ${index + 1} 条渲染记录：${renderLabel(job.status)} · ${date(job.created_at)}`}
            aria-pressed={selected?.id === job.id}
            onClick={() => setSelectedId(job.id)}
          >
            <span className="min-w-0">
              <strong>{renderLabel(job.status)}</strong>
              <span className="ml-2">{job.progress}%</span>
            </span>
            <time
              className="text-xs text-muted-foreground"
              dateTime={job.created_at}
            >
              {date(job.created_at)}
            </time>
          </Button>
        ))}
        {jobs.hasMore && (
          <Button
            variant="outline"
            disabled={jobs.loading}
            onClick={() => void jobs.load(true)}
          >
            加载更多渲染任务
          </Button>
        )}
      </div>
      {selected && (
        <JobDetail
          key={selected.id}
          job={selected}
          writable={writable}
          busy={mutation.busy}
          canRetry={!formal || canResumeFormal}
          unavailable={unavailable}
          onCancel={cancel}
          onRetry={() =>
            formal
              ? performFormal({ action: "RETRY", retry_of: selected.id })
              : submitPreview(selected.id)
          }
        />
      )}
    </section>
  );
}

function JobDetail({
  job,
  writable,
  busy,
  canRetry,
  unavailable,
  onCancel,
  onRetry,
}: {
  job: RenderJob;
  writable: boolean;
  busy: boolean;
  canRetry: boolean;
  unavailable: (cause: unknown) => void;
  onCancel: (job: RenderJob) => Promise<void>;
  onRetry: () => Promise<void>;
}) {
  const fetchAttempts = useCallback(
    (signal: AbortSignal) => renderingApi.listAttempts(job.id, signal),
    [job.id, job.revision],
  );
  const attempts = useRenderDetail(
    `${job.id}:${job.revision}`,
    fetchAttempts,
    unavailable,
  );
  return (
    <section
      aria-label="当前渲染任务"
      className="flex min-w-0 flex-col gap-3 rounded-xl border bg-muted/30 p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4>
          {renderLabel(job.status)} · {job.progress}%
        </h4>
        <span className="text-sm text-muted-foreground">
          {renderLabel(job.phase)}
        </span>
      </div>
      <progress
        value={Math.min(100, Math.max(0, job.progress))}
        max={100}
        aria-label="渲染进度"
        className="h-2 w-full accent-primary"
      />
      {job.error_code && (
        <Notice tone="error">
          渲染错误：{job.error_code}
          。请核对输入与失败记录后重新渲染，历史成片保持不变。
        </Notice>
      )}
      <p className="text-sm">清理状态：{cleanupLabel(job.cleanup_status)}</p>
      {job.status === "SUCCEEDED" && (
        <Notice tone={job.is_delivery ? "success" : "info"}>
          {job.is_delivery
            ? renderLabel(job.delivery_phase)
            : "渲染成功。正式交付是否通过，请查看成片报告与当前交付结论。"}
        </Notice>
      )}
      <div className="flex flex-wrap gap-2">
        {["QUEUED", "RUNNING"].includes(job.status) && writable && (
          <Button
            variant="outline"
            disabled={busy}
            onClick={() => void onCancel(job)}
          >
            取消渲染
          </Button>
        )}
        {["FAILED", "CANCELLED"].includes(job.status) && writable && (
          <Button
            variant="outline"
            disabled={busy || !canRetry}
            onClick={() => void onRetry()}
          >
            重新渲染
          </Button>
        )}
      </div>
      {attempts.error && <Notice tone="error">{attempts.error}</Notice>}
      <details className="min-w-0">
        <summary>执行尝试与检查记录（{attempts.data?.length ?? 0}）</summary>
        <div className="mt-3 flex min-w-0 flex-col gap-3">
          {attempts.data?.map((attempt) => (
            <div
              key={attempt.attempt_no}
              className="min-w-0 rounded-lg border p-3 text-sm"
            >
              <p>
                尝试 {attempt.attempt_no} · {renderLabel(attempt.status)} ·{" "}
                {attempt.progress}%
              </p>
              <p>
                阶段：{renderLabel(attempt.phase)}；清理：
                {cleanupLabel(attempt.cleanup_status)}
              </p>
              {attempt.error_code && (
                <p className="text-destructive">错误：{attempt.error_code}</p>
              )}
              {attempt.cleanup_error_code && (
                <p className="text-destructive">
                  清理错误：{attempt.cleanup_error_code}
                </p>
              )}
            </div>
          ))}
        </div>
      </details>
    </section>
  );
}
