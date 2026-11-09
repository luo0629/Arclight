"use client";

import { useId, useState } from "react";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { Notice } from "@/components/ui-kit";
import { label, type HumanReview } from "../types";

export type Mutate = (
  path: string,
  method: "POST" | "PATCH",
  payload: unknown,
  success: string,
) => Promise<boolean>;

export default function HumanReviewActions({
  row,
  allowed,
  busy,
  mutate,
}: {
  row: HumanReview;
  allowed: boolean;
  busy: boolean;
  mutate: Mutate;
}) {
  const input = useId();
  const [comment, setComment] = useState("");
  const endpoint = `/human-reviews/${row.id}`;
  const canDecide = allowed && row.status === "WAITING_REVIEW" && !row.decision;
  async function decide(decision: "APPROVE" | "REJECT") {
    if (
      !canDecide ||
      !comment.trim() ||
      (decision === "APPROVE" && !row.can_approve)
    )
      return;
    await mutate(
      endpoint + "/decisions",
      "POST",
      {
        version_id: row.snapshot.version_id,
        request_sha256: row.request_sha256,
        decision,
        comment: comment.trim(),
      },
      "人工决定已保存。请核对收据，再单独恢复执行。",
    );
  }
  return (
    <section
      aria-label="精确版本人工处理"
      className="flex min-w-0 flex-col gap-3 rounded-lg border p-4"
    >
      <h4 className="font-semibold">{label(row.status)}</h4>
      <p className="break-all text-xs text-muted-foreground">
        审核请求：{row.id} · 精确版本：{row.snapshot.version_id}
      </p>
      {row.blocking_code && (
        <Notice tone="error">
          当前请求受阻：{row.blocking_code}。请刷新并核对版本与权限。
        </Notice>
      )}
      {row.decision && (
        <div className="flex flex-col gap-2 text-sm">
          <p>决定：{label(row.decision.decision_json.decision)}</p>
          <p className="whitespace-pre-wrap">
            {row.decision.decision_json.comment}
          </p>
          <p className="break-all text-xs text-muted-foreground">
            审核人：{row.decision.actor_id} ·{" "}
            <time dateTime={row.decision.created_at}>
              {new Date(row.decision.created_at).toLocaleString()}
            </time>
          </p>
        </div>
      )}
      {canDecide && (
        <FieldGroup>
          <Field data-disabled={busy}>
            <FieldLabel htmlFor={input}>人工处理意见（必填）</FieldLabel>
            <Textarea
              id={input}
              maxLength={2000}
              value={comment}
              disabled={busy}
              onChange={(event) => setComment(event.target.value)}
            />
            <FieldDescription>
              说明本次精确图片的接受或拒绝依据。例外决定与版本批准分别记录。
            </FieldDescription>
          </Field>
          <Field orientation="horizontal" className="flex-wrap">
            <Button
              disabled={busy || !comment.trim() || !row.can_approve}
              onClick={() => void decide("APPROVE")}
            >
              接受本次例外
            </Button>
            <Button
              variant="outline"
              disabled={busy || !comment.trim()}
              onClick={() => void decide("REJECT")}
            >
              拒绝本次例外
            </Button>
          </Field>
        </FieldGroup>
      )}
      {canDecide && !row.can_approve && (
        <Notice>
          本轮尚未完成必需审查，只能拒绝本次请求；人工决定不能替代审查结论。
        </Notice>
      )}
      {row.status === "DECIDED" && row.decision && (
        <>
          <p className="text-sm">
            决定已持久化。恢复会复验精确版本与当前权限，并沿用原执行记录。
          </p>
          <Button
            variant="outline"
            disabled={busy || !allowed}
            onClick={() =>
              void mutate(
                endpoint + "/resume",
                "POST",
                {
                  decision_id: row.decision!.id,
                  request_sha256: row.request_sha256,
                },
                "原执行已恢复。版本批准与正式选用仍需分别确认。",
              )
            }
          >
            恢复原执行
          </Button>
        </>
      )}
      {row.resume && (
        <p className="text-sm">
          恢复结果：{label(row.resume.result_json.status ?? "RESUMED")}
        </p>
      )}
      {!allowed && (
        <p className="text-sm text-muted-foreground">
          人工处理需要当前项目 Owner 权限和最新审查。
        </p>
      )}
      <details className="min-w-0 text-xs">
        <summary className="cursor-pointer">精确请求与恢复凭据</summary>
        <dl className="mt-3 grid grid-cols-1 gap-1 [&_dd]:break-all [&_dt]:text-muted-foreground">
          <dt>请求摘要</dt>
          <dd>{row.request_sha256}</dd>
          <dt>输入／图片摘要</dt>
          <dd>
            {row.snapshot.input_sha256} / {row.snapshot.content_sha256}
          </dd>
          <dt>原 run／thread</dt>
          <dd>
            {row.snapshot.run_id ?? "无"} / {row.snapshot.thread_id}
          </dd>
          <dt>决定 ID</dt>
          <dd>{row.decision?.id ?? "尚无决定"}</dd>
          <dt>恢复收据</dt>
          <dd>{row.resume?.id ?? "尚未恢复"}</dd>
        </dl>
      </details>
    </section>
  );
}
