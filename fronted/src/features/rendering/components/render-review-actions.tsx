"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import type { HumanReview } from "../types";
import { renderLabel } from "./render-format";

export default function RenderReviewActions({
  review,
  allowed,
  busy,
  onDecision,
  onResume,
}: {
  review: HumanReview;
  allowed: boolean;
  busy: boolean;
  onDecision: (
    decision: "APPROVE" | "REJECT",
    comment: string,
  ) => Promise<void>;
  onResume: () => Promise<void>;
}) {
  const input = useId();
  const [comment, setComment] = useState("");
  const [confirmed, setConfirmed] = useState(false);
  const automatic = Boolean(review.automatic_production_id);
  const canDecide =
    !automatic &&
    allowed &&
    review.status === "WAITING_REVIEW" &&
    !review.decision;
  return (
    <section
      className="flex min-w-0 flex-col gap-3 rounded-xl border p-4"
      aria-label={automatic ? "短剧制作检查" : "交付警告人工确认"}
    >
      <h4>{automatic ? "短剧制作检查" : "确认本次交付例外"}</h4>
      <p className="text-sm text-muted-foreground">
        {automatic
          ? "本环节由短剧制作流程处理。"
          : "决定仅对应当前报告中的全部警告，不适用于其他成片版本。"}
      </p>
      <p className="text-sm">审核状态：{renderLabel(review.status)}</p>
      {review.blocking_code &&
        review.blocking_code !== "SHORT_DRAMA_AUTOMATIC" && (
          <Notice tone="error">
            当前请求受阻：{review.blocking_code}。
            {!automatic && "请刷新并核对来源与权限。"}
          </Notice>
        )}
      {review.decision && (
        <div className="flex flex-col gap-1 text-sm">
          <p>
            已保存决定：
            {review.decision.decision_json.decision === "APPROVE"
              ? "接受"
              : "拒绝"}
          </p>
          <p className="whitespace-pre-wrap break-words">
            {review.decision.decision_json.comment}
          </p>
        </div>
      )}
      {canDecide && (
        <>
          <label
            htmlFor={input}
            className="flex flex-col gap-2 text-sm font-medium"
          >
            人工处理意见
            <Textarea
              aria-label="人工处理意见"
              id={input}
              value={comment}
              onChange={(event) => setComment(event.target.value)}
              maxLength={2000}
              disabled={busy}
              placeholder="说明接受或拒绝的依据，最多 2000 字"
            />
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(event) => setConfirmed(event.target.checked)}
              disabled={busy}
              className="mt-1 size-4 shrink-0 accent-primary"
            />
            <span>我已核对并接受全部警告</span>
          </label>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={
                busy || !!review.blocking_code || !confirmed || !comment.trim()
              }
              onClick={() => void onDecision("APPROVE", comment.trim())}
            >
              确认接受全部警告
            </Button>
            <Button
              variant="outline"
              disabled={busy || !comment.trim()}
              onClick={() => void onDecision("REJECT", comment.trim())}
            >
              拒绝本次警告
            </Button>
          </div>
          {review.blocking_code && (
            <Notice>
              当前来源或必需检查不满足批准条件，人工确认不能豁免阻塞错误。
            </Notice>
          )}
        </>
      )}
      {!automatic && review.status === "DECIDED" && review.decision && (
        <>
          <p className="text-sm">
            决定已保存。请核对后单独恢复，服务端将再次检查权限和精确来源。
          </p>
          <Button
            variant="outline"
            disabled={busy || !allowed}
            onClick={() => void onResume()}
          >
            恢复交付确认
          </Button>
        </>
      )}
      {review.resume && (
        <Notice>
          恢复结果：
          {typeof review.resume.result_json.status === "string"
            ? review.resume.result_json.status
            : "已记录"}
        </Notice>
      )}
      {!automatic && !allowed && (
        <p className="text-sm text-muted-foreground">
          需要当前项目拥有者权限，且项目和剧集未归档。
        </p>
      )}
    </section>
  );
}
