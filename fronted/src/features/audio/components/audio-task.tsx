"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice, StatusBadge } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { episodePath } from "@/lib/routes";
import useRenderDetail from "@/features/rendering/hooks/use-render-detail";
import useRenderMutation from "@/features/rendering/hooks/use-render-mutation";
import { audioApi } from "../api";
import type { AudioResult, AudioTask } from "../types";
import { AudioFeedback, emptyRights, RightsFields } from "./audio-fields";

const terminal = new Set([
  "SUCCEEDED",
  "FAILED",
  "CANCELLED",
  "RESULT_UNKNOWN",
  "RECONCILING",
  "MANUAL_REVIEW",
]);
export default function AudioTaskDetail({
  projectId,
  episodeId,
  snapshotId,
  lineId,
  jobId,
  scope,
  writable,
  onRegistered,
  onUpdated,
  unavailable,
}: {
  projectId: string;
  episodeId: string;
  snapshotId: string;
  lineId: string;
  jobId: string;
  scope: string;
  writable: boolean;
  onRegistered: () => void;
  onUpdated: (row: AudioTask) => void;
  unavailable: (cause: unknown) => void;
}) {
  const fetchTask = useCallback(
    (signal: AbortSignal) => audioApi.task(snapshotId, lineId, jobId, signal),
    [snapshotId, lineId, jobId],
  );
  const task = useRenderDetail(scope, fetchTask, unavailable);
  const [pollError, setPollError] = useState("");
  const [rights, setRights] = useState(emptyRights);
  const [registered, setRegistered] = useState<AudioResult | null>(null);
  const mutation = useRenderMutation(scope, writable, unavailable);
  const target = task.data?.target_asset_id ?? "";
  const fetchAsset = useCallback(
    (signal: AbortSignal) => audioApi.asset(projectId, target, signal),
    [projectId, target],
  );
  const asset = useRenderDetail(
    `${scope}:${target}`,
    fetchAsset,
    unavailable,
    !!target,
  );
  const status = task.data?.status;
  useEffect(() => {
    if (task.data) onUpdated(task.data);
  }, [task.data, onUpdated]);
  useEffect(() => {
    if (!status || terminal.has(status)) return;
    const controller = new AbortController();
    let reading = false;
    const timer = window.setInterval(() => {
      if (reading || document.hidden) return;
      reading = true;
      void fetchTask(controller.signal)
        .then((row) => {
          if (!controller.signal.aborted) {
            setPollError("");
            task.setData(row);
          }
        })
        .catch((cause: unknown) => {
          if (!controller.signal.aborted) {
            setPollError(message(cause));
            unavailable(cause);
          }
        })
        .finally(() => {
          reading = false;
        });
    }, 3000);
    return () => {
      controller.abort();
      window.clearInterval(timer);
    };
  }, [status, fetchTask, task.setData, unavailable]);
  async function register() {
    if (
      !task.data ||
      task.data.status !== "SUCCEEDED" ||
      !asset.data ||
      !rights.confirmed ||
      !rights.source.trim() ||
      !rights.rights.trim()
    )
      return;
    await mutation.mutate(
      (signal) =>
        audioApi.register(
          snapshotId,
          lineId,
          jobId,
          {
            expected_revision: asset.data!.revision,
            source_note: rights.source.trim(),
            rights_note: rights.rights.trim(),
            rights_confirmed: true,
          },
          signal,
        ),
      (result) => {
        setRegistered(result);
        onRegistered();
        void task.load();
        void asset.load();
      },
      "音频已通过服务端检查并登记为新候选，请试听后审核和选用。",
    );
  }
  return (
    <section
      aria-label="本句配音任务"
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <h4 className="font-semibold">本句配音任务</h4>
        {task.data && <StatusBadge status={task.data.status} />}
      </div>
      <p className="break-all text-xs text-muted-foreground">任务：{jobId}</p>
      <AudioFeedback error={task.error || pollError || asset.error} />
      {task.data?.error_code && (
        <Notice tone="error">
          本句任务未完成：{task.data.error_code}。请到任务中心查看原因并处理。
        </Notice>
      )}
      {status &&
        ["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"].includes(status) && (
          <Notice>
            供应商结果尚未核清，请先到任务中心对账。这里不会自动重新生成或登记音频。
          </Notice>
        )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={task.loading || mutation.busy}
          onClick={() => {
            setPollError("");
            void task.load();
            void asset.load();
          }}
        >
          刷新本句任务
        </Button>
        <Button variant="ghost" size="sm" asChild>
          <Link
            href={`${episodePath(projectId, episodeId, "tasks")}&generation_job=${encodeURIComponent(jobId)}`}
          >
            在任务中心查看
          </Link>
        </Button>
      </div>
      {task.data?.asset_version_id && (
        <p role="status" className="text-sm">
          本任务结果已登记。可在本句音频历史中试听和审核。
        </p>
      )}
      {writable && status === "SUCCEEDED" && !task.data?.asset_version_id && (
        <form
          className="flex flex-col gap-4"
          onSubmit={(event) => {
            event.preventDefault();
            void register();
          }}
        >
          <p className="text-sm text-muted-foreground">
            生成成功后仍须确认来源与授权，并通过音频机器检查，才能登记为候选。
          </p>
          <RightsFields
            value={rights}
            onChange={setRights}
            disabled={mutation.busy}
          />
          <Button
            type="submit"
            disabled={
              mutation.busy ||
              !asset.data ||
              asset.loading ||
              asset.data.status === "LOCKED" ||
              !rights.confirmed ||
              !rights.source.trim() ||
              !rights.rights.trim()
            }
          >
            {mutation.busy ? "正在检查并登记…" : "确认授权并登记本句结果"}
          </Button>
        </form>
      )}
      {registered && (
        <p role="status" className="text-sm">
          机器检查：{registered.validation.codec} ·{" "}
          {registered.validation.sample_rate} Hz ·{" "}
          {registered.validation.channels} 声道 ·{" "}
          {registered.validation.duration_ms} 毫秒。
        </p>
      )}
      {registered?.validation.silence.silent && (
        <Notice>
          机器检查发现这份音频为静音。请试听并核对结果；这不会替代渲染清单中的逐句静音确认。
        </Notice>
      )}
      <AudioFeedback {...mutation} />
    </section>
  );
}
