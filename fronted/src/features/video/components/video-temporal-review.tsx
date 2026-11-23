"use client";

import { useEffect, useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import HumanReviewActions, {
  type Mutate,
} from "@/features/continuity/components/human-review-actions";
import type { HumanReview } from "@/features/continuity/types";
import { api, json, message } from "@/lib/api/client";
import { candidatePath } from "../api";
import type useVideoCandidate from "../hooks/use-video-candidate";
import type { VideoCandidate } from "../types";

export default function VideoTemporalReview({
  candidate,
  disabled,
  state,
  onChanged,
}: {
  candidate: VideoCandidate;
  disabled: boolean;
  state: ReturnType<typeof useVideoCandidate>;
  onChanged: () => Promise<void>;
}) {
  const id = useId();
  const [instruction, setInstruction] = useState("");
  const [updates, setUpdates] = useState(0);
  const [human, setHuman] = useState<HumanReview | null>(null);
  const [readError, setReadError] = useState("");
  const {
    admission,
    actions,
    video_source: origin,
    project_id: projectId,
  } = candidate;
  const requestId = admission?.human_review_request_id;
  const endpoint = candidatePath(projectId, candidate.id) + "/review";
  const unresolved =
    admission?.issues?.filter((issue) => !issue.resolved) ?? [];
  useEffect(() => {
    const controller = new AbortController();
    if (requestId) {
      void api<HumanReview>(`/human-reviews/${requestId}`, {
        signal: controller.signal,
      })
        .then((row) => {
          if (!controller.signal.aborted) {
            setHuman(row);
            setReadError("");
          }
        })
        .catch((cause) => {
          if (!controller.signal.aborted) setReadError(message(cause));
        });
    }
    return () => controller.abort();
  }, [requestId, updates]);

  const mutate: Mutate = async (path, method, payload, success) => {
    const result = await state.perform(
      (signal) => api(path, { ...json(method, payload), signal }),
      success,
    );
    if (!result) return false;
    setUpdates((value) => value + 1);
    await onChanged();
    return true;
  };

  async function revise() {
    if (!admission?.report_id || !instruction.trim() || !unresolved.length)
      return;
    const reportId = admission.report_id;
    const result = await state.perform(
      (signal) =>
        api(`/projects/${projectId}/continuity-revisions`, {
          ...json("POST", {
            review_id: reportId,
            issue_ids: unresolved.map((issue) => issue.issue_id),
            action: "REVISE_PROMPT",
            amendments: [
              { block: "shot_description", instruction: instruction.trim() },
            ],
          }),
          headers: {
            "Content-Type": "application/json",
            "Idempotency-Key": `video-review/${reportId}`,
          },
          signal,
        }),
      "已保存返修请求。新候选生成后将重新进行视频审查。",
    );
    if (result) await onChanged();
  }

  return (
    <section
      aria-label="视频时序审查准入"
      className="flex flex-col gap-2 text-xs"
    >
      <h5 className="font-semibold">视频时序审查</h5>
      {admission?.status === "ADMITTED" ? (
        <p>审查已通过准入 · {admission.conclusion}</p>
      ) : (
        <Notice>
          视频时序审查尚未满足，不能提交批准、正式选用或锁定。
          {admission?.blocked_code}
        </Notice>
      )}
      {actions?.can_start_review && origin && (
        <Button
          size="sm"
          variant="outline"
          disabled={disabled}
          className="self-start"
          onClick={() =>
            void mutate(
              endpoint,
              "POST",
              { storyboard_id: origin.storyboard_id, shot_id: origin.shot_id },
              "已提交本候选的视频时序审查。",
            )
          }
        >
          提交视频时序审查
        </Button>
      )}
      {admission?.review_status && (
        <p>
          审查运行 · {admission.review_status}
          {admission.review_error_code && ` · ${admission.review_error_code}`}
        </p>
      )}
      {admission?.review_status === "FAILED" &&
        admission.review_run_id &&
        actions?.can_replace && (
          <Button
            size="sm"
            variant="outline"
            disabled={disabled}
            className="self-start"
            onClick={() =>
              void mutate(
                `/runs/${admission.review_run_id}/retry`,
                "POST",
                {},
                "已重试原视频审查。",
              )
            }
          >
            重试原审查
          </Button>
        )}
      {admission?.summary && <p>{admission.summary}</p>}
      {admission?.sampled_frame_count != null && (
        <p>
          采样 {admission.sampled_frame_count} 帧 · 覆盖{" "}
          {admission.coverage_start_ms}–{admission.coverage_end_ms} ms
        </p>
      )}
      {admission?.limitations.map((limitation, index) => (
        <p key={index}>{limitation}</p>
      ))}
      {!!admission?.issues?.length && (
        <ul className="grid gap-1" aria-label="视频时序问题">
          {admission.issues.map((issue) => (
            <li key={issue.issue_id}>
              {issue.time_ms} ms · {issue.description}
            </li>
          ))}
        </ul>
      )}
      {!!admission?.samples?.length && (
        <details>
          <summary className="cursor-pointer">时间码与取证记录</summary>
          <ul className="grid gap-2 px-2 py-1">
            {admission.samples.map((sample) => (
              <li key={sample.time_ms} className="break-all">
                <p>
                  {sample.time_ms} ms · {sample.observation}
                </p>
              </li>
            ))}
          </ul>
        </details>
      )}
      {requestId && readError && <Notice tone="error">{readError}</Notice>}
      {human &&
        human.id === requestId &&
        human.snapshot.object_id === admission?.report_id &&
        human.snapshot.version_id === candidate.asset_version_id && (
          <HumanReviewActions
            row={{
              ...human,
              can_approve:
                !unresolved.length &&
                admission.blocked_code !== "VIDEO_REVIEW_TARGET_INVALID",
            }}
            allowed={!!actions?.can_decide_review}
            busy={disabled}
            mutate={mutate}
            mediaLabel="视频"
          />
        )}
      {admission?.revision_id && (
        <p>
          {admission.revision_status === "NEEDS_REVIEW"
            ? "已达到返修上限，需人工处理。"
            : "已提交视频返修任务"}
        </p>
      )}
      {actions?.can_revise && (
        <div className="grid gap-2">
          <label htmlFor={id}>本候选返修说明</label>
          <Textarea
            id={id}
            value={instruction}
            maxLength={2000}
            disabled={disabled}
            onChange={(event) => setInstruction(event.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={disabled || !instruction.trim()}
            className="justify-self-start"
            onClick={() => void revise()}
          >
            发起视频返修
          </Button>
        </div>
      )}
    </section>
  );
}
