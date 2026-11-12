"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useSearchParams } from "next/navigation";
import { isResourceId } from "@/lib/routes";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { renderingApi } from "../api";
import type {
  HumanReview,
  RenderAdmission,
  RenderJob,
  RenderManifest,
} from "../types";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation, {
  pendingCreationKey,
} from "../hooks/use-render-mutation";
import RenderReviewActions from "./render-review-actions";
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
  initialJob,
}: {
  manifest: RenderManifest;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  refresh: number;
  onRendered: () => void;
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
  const [review, setReview] = useState<HumanReview | null>(null);
  const requestedReview = useSearchParams().get("review");
  const [reviewError, setReviewError] = useState("");
  useEffect(() => {
    if (!requestedReview || !isResourceId(requestedReview)) return;
    const controller = new AbortController();
    setReview(null);
    setReviewError("");
    void renderingApi
      .getHumanReview(requestedReview, controller.signal)
      .then((next) => {
        if (controller.signal.aborted) return;
        if (
          next.id !== requestedReview ||
          next.snapshot.kind !== "EXPORT" ||
          next.snapshot.object_id !== manifest.id ||
          next.snapshot.project_id !== manifest.project_id
        )
          throw new Error("审核请求与当前渲染清单不一致。");
        setReview(next);
      })
      .catch((cause: unknown) => {
        if (!controller.signal.aborted) {
          setReviewError(message(cause));
          unavailable(cause);
        }
      });
    return () => controller.abort();
  }, [requestedReview, manifest.id, manifest.project_id, unavailable]);
  const [admission, setAdmission] = useState<RenderAdmission | null>(null);
  const [admissionError, setAdmissionError] = useState("");
  const mutation = useRenderMutation(manifest.id, writable, unavailable);
  const formal = manifest.snapshot.output.name === "FORMAL";
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
  async function submit(retryOf?: string) {
    if (formal && (!admission || (review && review.status !== "RESUMED")))
      return;
    const pending = pendingCreationKey(
      `job:${manifest.id}:${retryOf ?? "new"}`,
    );
    await mutation.mutate(
      async (signal) => {
        try {
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
        } catch (cause) {
          if (!signal.aborted && formal) setAdmission(null);
          throw cause;
        }
      },
      (job) => {
        jobs.prepend(job);
        setSelectedId(job.id);
      },
      "渲染已提交。刷新页面后仍可从本清单的任务记录继续查看。",
    );
  }
  async function checkAdmission() {
    setAdmissionError("");
    await mutation.mutate(
      async (signal) => {
        try {
          return await renderingApi.getAdmission(manifest.id, signal);
        } catch (cause) {
          if (!signal.aborted) {
            setAdmission(null);
            setAdmissionError(message(cause));
          }
          throw cause;
        }
      },
      setAdmission,
      "本清单当前准入检查通过，实际执行时仍会再次复验。",
      false,
    );
  }
  async function requestExport() {
    if (!owner) return;
    setAdmission(null);
    await mutation.mutate(
      (signal) => renderingApi.exportReview(manifest.id, signal),
      setReview,
      "正式导出审核已登记，请核对精确清单后作出决定。",
    );
  }
  async function decide(decision: "APPROVE" | "REJECT", comment: string) {
    if (!review || !owner) return;
    setAdmission(null);
    await mutation.mutate(
      async (signal) => {
        await renderingApi.decideReview(review, decision, comment, signal);
        return renderingApi.getHumanReview(review.id, signal);
      },
      setReview,
      "正式导出决定已保存，请单独恢复本次导出。",
    );
  }
  async function resume() {
    if (!review || !owner) return;
    await mutation.mutate(
      async (signal) => {
        await renderingApi.resumeReview(review, signal);
        const nextReview = await renderingApi.getHumanReview(review.id, signal);
        let nextAdmission: RenderAdmission | null = null;
        let nextError = "";
        if (
          nextReview.status === "RESUMED" &&
          nextReview.decision?.decision_json.decision === "APPROVE"
        ) {
          try {
            nextAdmission = await renderingApi.getAdmission(
              manifest.id,
              signal,
            );
          } catch (cause) {
            if (signal.aborted) throw cause;
            nextError = message(cause);
          }
        }
        return { nextReview, nextAdmission, nextError };
      },
      (next) => {
        setReview(next.nextReview);
        setAdmission(next.nextAdmission);
        setAdmissionError(next.nextError);
      },
      "正式导出审核已恢复，当前准入结论已重新核对。",
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
  const canSubmit =
    writable &&
    !mutation.busy &&
    (!formal || !!admission) &&
    (!review ||
      (review.status === "RESUMED" &&
        review.decision?.decision_json.decision === "APPROVE"));
  return (
    <section aria-label="渲染任务" className="flex min-w-0 flex-col gap-4">
      <div className="flex flex-wrap gap-2">
        <Button disabled={!canSubmit} onClick={() => void submit()}>
          提交渲染
        </Button>
        {formal && writable && (
          <Button
            variant="outline"
            disabled={mutation.busy}
            onClick={() => void checkAdmission()}
          >
            检查正式导出条件
          </Button>
        )}
        {formal && owner && writable && (
          <Button
            variant="outline"
            disabled={mutation.busy}
            onClick={() => void requestExport()}
          >
            申请正式导出审核
          </Button>
        )}
        <Button
          variant="ghost"
          disabled={jobs.loading || mutation.busy}
          onClick={() => void jobs.load()}
        >
          刷新渲染任务
        </Button>
      </div>
      {formal && (
        <p className="text-sm text-muted-foreground">
          正式渲染需要拥有者批准此精确清单并独立恢复。准入检查通过只允许执行，正式交付还需成片机器报告与必要的警告确认。
        </p>
      )}
      {admission && (
        <Notice tone="success">
          本清单当前准入已通过。执行时仍将复验素材、批准与权限。
        </Notice>
      )}
      {admissionError && (
        <Notice tone="error">正式导出条件未满足：{admissionError}</Notice>
      )}
      {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
      {reviewError && <Notice tone="error">{reviewError}</Notice>}
      {review && (
        <RenderReviewActions
          key={review.id}
          review={review}
          purpose="export"
          allowed={owner && writable}
          busy={mutation.busy}
          onDecision={decide}
          onResume={resume}
        />
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
        {jobRows.map((job) => (
          <Button
            key={job.id}
            variant="outline"
            className="h-auto min-w-0 flex-wrap justify-between gap-2 px-3 py-3 text-left whitespace-normal"
            aria-label={`查看渲染任务 ${job.id}`}
            aria-pressed={selected?.id === job.id}
            onClick={() => setSelectedId(job.id)}
          >
            <span className="min-w-0">
              <strong>{renderLabel(job.status)}</strong>
              <span className="ml-2">{job.progress}%</span>
              <span className="mt-1 block break-all text-xs text-muted-foreground">
                {job.id}
              </span>
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
          canRetry={canSubmit}
          unavailable={unavailable}
          onCancel={cancel}
          onRetry={() => submit(selected.id)}
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
      <p className="break-all text-xs text-muted-foreground">
        任务 {job.id} · 修订 {job.revision}
      </p>
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
              {attempt.inspection && (
                <details className="mt-2 min-w-0">
                  <summary>本次技术检查</summary>
                  <pre>{JSON.stringify(attempt.inspection, null, 2)}</pre>
                </details>
              )}
            </div>
          ))}
        </div>
      </details>
    </section>
  );
}
