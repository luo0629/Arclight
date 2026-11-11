"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { ApiError, message } from "@/lib/api/client";
import type { Episode } from "@/types/api";
import { asMachineReport, renderingApi } from "../api";
import type { HumanReview, RenderDelivery } from "../types";
import useRenderList from "../hooks/use-render-list";
import useRenderDetail from "../hooks/use-render-detail";
import useRenderMutation from "../hooks/use-render-mutation";
import DeliveryPreview from "./delivery-preview";
import RenderReport from "./render-report";
import RenderReviewActions from "./render-review-actions";
import { bytes, date, duration, renderLabel } from "./render-format";

const idOf = (row: { id: string }) => row.id;
export default function DeliveryHistory({
  episode,
  owner,
  writable,
  unavailable,
  refresh,
}: {
  episode: Episode;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  refresh: number;
}) {
  const fetchPage = useCallback(
    (cursor: string | undefined, signal: AbortSignal) =>
      renderingApi.listDeliveries(episode.id, cursor, signal),
    [episode.id],
  );
  const rows = useRenderList<RenderDelivery, string>(
    episode.id,
    fetchPage,
    idOf,
    unavailable,
  );
  const [selectedId, setSelectedId] = useState("");
  const selected = selectedId
    ? rows.rows.find((row) => row.id === selectedId)
    : rows.rows[0];
  useEffect(() => {
    if (refresh) void rows.load();
  }, [refresh, rows.load]);
  return (
    <section
      aria-label="成片历史"
      className="flex min-w-0 flex-col gap-4 rounded-xl border bg-card p-4 sm:p-5"
    >
      <header className="flex flex-wrap items-center justify-between gap-2">
        <div>
          <h3>成片历史</h3>
          <p className="mt-1 text-sm text-muted-foreground">
            每次重渲染保留独立版本；渲染成功与正式交付分别显示。
          </p>
        </div>
        <Button
          variant="ghost"
          disabled={rows.loading}
          onClick={() => void rows.load()}
        >
          刷新成片历史
        </Button>
      </header>
      {rows.error && <Notice tone="error">{rows.error}</Notice>}
      {rows.loading && rows.rows.length === 0 && (
        <p role="status" className="text-sm text-muted-foreground">
          正在读取成片历史…
        </p>
      )}
      {!rows.loading && !rows.error && rows.rows.length === 0 && (
        <p className="py-4 text-sm text-muted-foreground">
          这一集还没有成片。请先选择时间轴并提交渲染。
        </p>
      )}
      {selectedId && !selected && !rows.loading && rows.rows.length > 0 && (
        <Notice>所选成片不在当前页，请加载更多或重新选择精确成片版本。</Notice>
      )}
      <div className="grid min-w-0 items-start gap-5 lg:grid-cols-[minmax(0,15rem)_minmax(0,1fr)]">
        <div className="flex min-w-0 flex-col gap-2">
          {rows.rows.map((row) => (
            <Button
              key={row.id}
              variant="outline"
              aria-pressed={selected?.id === row.id}
              aria-label={`查看成片版本 ${row.version_no}`}
              onClick={() => setSelectedId(row.id)}
              className="h-auto min-w-0 flex-col items-start gap-1 px-3 py-3 text-left whitespace-normal"
            >
              <span className="font-semibold">
                成片 v{row.version_no} · {renderLabel(row.profile)}
              </span>
              <span className="text-xs text-muted-foreground">
                {duration(row.duration_ms)} / {bytes(row.byte_size)}
              </span>
              <span className="text-xs">
                {renderLabel(row.acceptance.phase)}
                {row.stale ? " · 素材已失效" : ""}
              </span>
              <time
                className="text-xs text-muted-foreground"
                dateTime={row.created_at}
              >
                {date(row.created_at)}
              </time>
            </Button>
          ))}
          {rows.hasMore && (
            <Button
              variant="outline"
              disabled={rows.loading}
              onClick={() => void rows.load(true)}
            >
              加载更多成片
            </Button>
          )}
        </div>
        {selected && (
          <DeliveryDetail
            key={selected.id}
            episode={episode}
            artifactId={selected.id}
            owner={owner}
            writable={writable}
            unavailable={unavailable}
            refresh={refresh}
          />
        )}
      </div>
    </section>
  );
}

function DeliveryDetail({
  episode,
  artifactId,
  owner,
  writable,
  unavailable,
  refresh,
}: {
  episode: Episode;
  artifactId: string;
  owner: boolean;
  writable: boolean;
  unavailable: (cause: unknown) => void;
  refresh: number;
}) {
  const fetchDetail = useCallback(
    (signal: AbortSignal) =>
      renderingApi.getDelivery(episode.id, artifactId, signal),
    [episode.id, artifactId],
  );
  const detail = useRenderDetail(artifactId, fetchDetail, unavailable);
  const fetchReport = useCallback(
    async (signal: AbortSignal) => {
      try {
        return await renderingApi.getReport(episode.id, artifactId, signal);
      } catch (cause) {
        if (cause instanceof ApiError && cause.status === 404)
          throw new ApiError(
            404,
            cause.code,
            "该成片没有可读取的机器报告。历史成片和授权预览仍可查看。",
            cause.requestId,
          );
        throw cause;
      }
    },
    [episode.id, artifactId],
  );
  const reportUnavailable = useCallback(
    (cause: unknown) => {
      if (!(cause instanceof ApiError && cause.status === 404))
        unavailable(cause);
    },
    [unavailable],
  );
  const report = useRenderDetail(
    artifactId,
    fetchReport,
    reportUnavailable,
    false,
  );
  const reportOpened = useRef(false);
  const [review, setReview] = useState<HumanReview | null>(null);
  const [reviewError, setReviewError] = useState("");
  const reviewRead = useRef<AbortController | null>(null);
  const reviewGeneration = useRef(0);
  const mutation = useRenderMutation(
    artifactId,
    writable && owner,
    unavailable,
  );
  const reviewId = detail.data?.acceptance.review_id;
  useEffect(() => {
    const generation = ++reviewGeneration.current;
    reviewRead.current?.abort();
    setReview(null);
    setReviewError("");
    if (!reviewId) return;
    const controller = new AbortController();
    reviewRead.current = controller;
    void renderingApi
      .getHumanReview(reviewId, controller.signal)
      .then((next) => {
        if (
          !controller.signal.aborted &&
          generation === reviewGeneration.current
        )
          setReview(next);
      })
      .catch((cause) => {
        if (
          !controller.signal.aborted &&
          generation === reviewGeneration.current
        ) {
          setReviewError(message(cause));
          unavailable(cause);
        }
      });
    return () => controller.abort();
  }, [reviewId, unavailable]);
  useEffect(() => {
    if (refresh) {
      void detail.load();
      if (reportOpened.current) void report.load();
    }
  }, [refresh, detail.load, report.load]);
  async function requestWarnings() {
    invalidateReviewRead();
    await mutation.mutate(
      (signal) =>
        renderingApi.requestWarningReview(episode.id, artifactId, signal),
      setReview,
      "交付警告已提交给拥有者，请逐项核对后作出决定。",
    );
  }
  function invalidateReviewRead() {
    ++reviewGeneration.current;
    reviewRead.current?.abort();
    setReviewError("");
  }
  async function decide(decision: "APPROVE" | "REJECT", comment: string) {
    if (!review) return;
    invalidateReviewRead();
    await mutation.mutate(
      async (signal) => {
        await renderingApi.decideReview(review, decision, comment, signal);
        return renderingApi.getHumanReview(review.id, signal);
      },
      setReview,
      "人工决定已保存。请核对决定，再单独恢复交付确认。",
    );
  }
  async function resume() {
    if (!review) return;
    invalidateReviewRead();
    await mutation.mutate(
      async (signal) => {
        await renderingApi.resumeReview(review, signal);
        const [nextReview, nextDetail, nextReport] = await Promise.all([
          renderingApi.getHumanReview(review.id, signal),
          renderingApi.getDelivery(episode.id, artifactId, signal),
          renderingApi.getReport(episode.id, artifactId, signal),
        ]);
        return { nextReview, nextDetail, nextReport };
      },
      (next) => {
        setReview(next.nextReview);
        detail.setData(next.nextDetail);
        report.setData(next.nextReport);
      },
      "交付确认已恢复，当前交付结论已重新读取。",
    );
  }
  const row = detail.data;
  const machine = report.data
    ? asMachineReport(report.data.report.details)
    : null;
  return (
    <section aria-label="当前成片版本" className="flex min-w-0 flex-col gap-4">
      {detail.loading && <p role="status">正在核对精确成片版本…</p>}
      {detail.error && (
        <Notice tone="error">
          {detail.error}
          <Button variant="outline" onClick={() => void detail.load()}>
            重试读取成片
          </Button>
        </Notice>
      )}
      {row && (
        <>
          <header>
            <h4>
              成片版本 v{row.version_no} · {renderLabel(row.profile)}
            </h4>
            <p className="mt-1 text-sm">
              {row.is_delivery && row.acceptance.is_delivery
                ? renderLabel(row.acceptance.phase)
                : "尚未满足正式交付条件"}
            </p>
          </header>
          {row.stale && (
            <Notice tone="error">
              此成片引用的素材已失效。历史文件保留，需核对上游后重新渲染。
            </Notice>
          )}
          {row.acceptance.blocking_issues.length > 0 && (
            <Notice tone="error">
              <ul className="list-inside list-disc">
                {row.acceptance.blocking_issues.map((issue) => (
                  <li key={issue}>{issue}</li>
                ))}
              </ul>
            </Notice>
          )}
          <DeliveryPreview
            key={row.id}
            projectId={episode.project_id}
            episodeId={episode.id}
            delivery={row}
            onUnavailable={unavailable}
          />
          <details className="min-w-0">
            <summary>成片与输入版本</summary>
            <dl className="mt-3 grid gap-2 text-xs [&_dd]:break-all [&_dt]:text-muted-foreground">
              <dt>成片版本 ID</dt>
              <dd>{row.asset_version_id}</dd>
              <dt>成片 SHA256</dt>
              <dd>{row.sha256}</dd>
              <dt>时间轴版本</dt>
              <dd>{row.timeline_id}</dd>
              <dt>时间轴 SHA256</dt>
              <dd>{row.timeline_sha256}</dd>
              <dt>渲染清单</dt>
              <dd>{row.manifest_id}</dd>
              <dt>清单 SHA256</dt>
              <dd>{row.manifest_sha256}</dd>
              <dt>依赖清单</dt>
              <dd>{row.dependency_manifest_id}</dd>
              <dt>精确素材来源</dt>
              <dd>
                <ul className="flex flex-col gap-2">
                  {row.sources.map((source) => (
                    <li key={source.asset_version_id}>
                      {source.asset_type} / {source.asset_version_id}
                      <br />
                      {source.sha256}
                    </li>
                  ))}
                </ul>
              </dd>
            </dl>
          </details>
          <Button
            className="self-start"
            variant="outline"
            disabled={report.loading || !row.report_file_id}
            onClick={() => {
              reportOpened.current = true;
              void report.load();
            }}
          >
            {report.loading ? "读取报告中…" : "查看报告"}
          </Button>
          {!row.report_file_id && (
            <p className="text-sm text-muted-foreground">
              该历史成片没有机器验收报告。
            </p>
          )}
          {report.error && <Notice tone="error">{report.error}</Notice>}
          {report.data && <RenderReport report={report.data} />}
          {machine &&
            machine.warnings.length > 0 &&
            !report.data?.acceptance.warnings_confirmed &&
            owner &&
            writable &&
            !row.stale &&
            !review && (
              <Button
                className="self-start"
                disabled={mutation.busy}
                onClick={() => void requestWarnings()}
              >
                申请交付警告确认
              </Button>
            )}
          {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
          {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
          {reviewError && <Notice tone="error">{reviewError}</Notice>}
          {review && (
            <RenderReviewActions
              key={review.id}
              review={review}
              purpose="warnings"
              allowed={owner && writable}
              busy={mutation.busy}
              onDecision={decide}
              onResume={resume}
            />
          )}
        </>
      )}
    </section>
  );
}
