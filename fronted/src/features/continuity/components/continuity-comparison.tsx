"use client";

import { useEffect, useId, useState } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import Dialog from "@/components/shared/app-dialog";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import {
  Empty,
  EmptyDescription,
  EmptyHeader,
  EmptyTitle,
} from "@/components/ui/empty";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Separator } from "@/components/ui/separator";
import { Textarea } from "@/components/ui/textarea";
import { LoadingState, Notice, StatusBadge } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import ImpactButton from "@/features/dependencies/components/impact-button";
import useContinuityComparison from "../hooks/use-continuity-comparison";
import { label, roundLabel, type ComparisonImage } from "../types";
import HumanReviewActions from "./human-review-actions";
import ReviewReport from "./review-report";

type Resource = { assetId: string; versionId: string };

function ComparisonBody({
  projectId,
  resource,
  onNavigate,
  onChanged,
  onBusy,
}: {
  projectId: string;
  resource: Resource;
  onNavigate: (resource: Resource) => void;
  onChanged: () => Promise<void>;
  onBusy: (busy: boolean) => void;
}) {
  const endpoint = `/projects/${projectId}/assets/${resource.assetId}/versions/${resource.versionId}`;
  const {
    data,
    loading,
    busy,
    error,
    imageError,
    notice,
    load,
    mutate,
    unavailable,
  } = useContinuityComparison(endpoint + "/continuity", onChanged);
  const [baseId, setBaseId] = useState("");
  const [reviewId, setReviewId] = useState("");
  const [comment, setComment] = useState("");
  const reasonId = useId();
  useEffect(() => {
    onBusy(busy);
    return () => onBusy(false);
  }, [busy, onBusy]);
  const selected =
    data?.reviews.find((item) => item.record.review.result_id === reviewId) ??
    data?.reviews[0];
  const latest =
    !selected || selected.record.review.result_id === data?.latest_review_id;
  const disabled = busy || loading || !latest || Boolean(imageError);
  const base =
    data?.images.find(
      (item) =>
        item.version.id === baseId && item.version.id !== resource.versionId,
    ) ?? data?.images.find((item) => item.version.id !== resource.versionId);
  async function change(status: string, impact_digest?: string) {
    if (!data || disabled) return false;
    return mutate(
      endpoint + "/state",
      "PATCH",
      {
        expected_revision: data.version.revision,
        status,
        comment: comment.trim(),
        impact_digest,
      },
      "版本状态已更新。请继续核对正式选用。",
    );
  }
  function imageFigure(image: ComparisonImage) {
    return (
      <figure className="flex min-w-0 flex-col gap-3 rounded-lg border p-4">
        <figcaption className="flex flex-col gap-1">
          <strong>{roundLabel(image)}</strong>
          <span className="text-sm text-muted-foreground">
            {image.asset_name}
          </span>
        </figcaption>
        <VersionImage
          key={image.image_path}
          path={image.image_path}
          alt={`${roundLabel(image)} · ${image.version.id}`}
          onUnavailable={unavailable}
        />
        <p className="break-all text-xs text-muted-foreground">
          精确版本：{image.version.id}
          <br />
          SHA-256：{image.version.checksum}
        </p>
        <Button
          variant="outline"
          disabled={busy || loading}
          onClick={() =>
            onNavigate({ assetId: image.asset_id, versionId: image.version.id })
          }
        >
          检查此轮图片与报告
        </Button>
      </figure>
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-6">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <p className="text-sm text-muted-foreground">
          逐张核对原图、依据和精确参考，再处理人工请求与正式选用。
        </p>
        <Button
          variant="outline"
          disabled={busy || loading}
          onClick={() => void load()}
        >
          刷新审查与权限
        </Button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
      {imageError && <Notice tone="error">{imageError}</Notice>}
      {notice && <Notice tone="success">{notice}</Notice>}
      {loading && <LoadingState>正在读取精确图片与审查记录…</LoadingState>}
      {data && (
        <>
          <section
            aria-label="当前候选状态"
            className="flex min-w-0 flex-col gap-3"
          >
            <div className="flex flex-wrap items-center gap-2">
              <h3 className="font-semibold">
                {data.asset.name} · v{data.version.version_no}
              </h3>
              <StatusBadge status={data.version.status} />
              {data.version.stale && (
                <Badge variant="destructive">已失效</Badge>
              )}
              {data.asset.current_version_id === data.version.id && (
                <Badge variant="secondary">正式选用</Badge>
              )}
            </div>
            <p className="text-sm">
              媒体检查：
              {data.candidate?.media_check
                ? label(data.candidate.media_check.status)
                : "缺少检查"}
              {data.candidate?.media_check?.error_code
                ? ` · ${data.candidate.media_check.error_code}`
                : ""}
            </p>
            <p className="break-all text-xs text-muted-foreground">
              候选：{data.candidate?.id ?? "未登记"} · 精确版本：
              {data.version.id}
            </p>
            {data.actions.blocker ? (
              <Notice>
                <p>{data.actions.blocker.message}</p>
                <span className="break-all text-xs">
                  {data.actions.blocker.code}
                </span>
              </Notice>
            ) : (
              <p className="text-sm">
                当前审查门禁已满足。版本状态与最终选用由服务端再次复验。
              </p>
            )}
            {!data.actions.owner && (
              <p className="text-sm text-muted-foreground">
                当前角色可查看；人工决定、关键版本批准和正式选用由 Owner 操作。
              </p>
            )}
            {["LOCKED", "ARCHIVED"].includes(data.asset.status) && (
              <p className="text-sm text-muted-foreground">
                资产已锁定或归档，版本操作已禁用。
              </p>
            )}
            {!data.actions.writable && (
              <p className="text-sm text-muted-foreground">
                当前项目或角色处于只读状态。
              </p>
            )}
          </section>
          <section
            aria-label="逐图比较"
            className="flex min-w-0 flex-col gap-4"
          >
            <h3 className="font-semibold">原图与修订图</h3>
            <FieldGroup>
              <Field>
                <FieldLabel htmlFor={reasonId + "-base"}>
                  比较基准图片
                </FieldLabel>
                <ChoiceSelect
                  id={reasonId + "-base"}
                  value={base?.version.id ?? ""}
                  disabled={busy || loading || !base}
                  onValueChange={setBaseId}
                  placeholder="尚无其他轮次"
                  options={data.images
                    .filter((item) => item.version.id !== resource.versionId)
                    .map((item) => ({
                      value: item.version.id,
                      label: `${roundLabel(item)} · ${item.asset_name}`,
                    }))}
                />
              </Field>
            </FieldGroup>
            <div
              className="grid min-w-0 grid-cols-1 gap-4 md:grid-cols-2"
              data-testid="continuity-image-pair"
            >
              {base ? (
                imageFigure(base)
              ) : (
                <Empty className="border">
                  <EmptyHeader>
                    <EmptyTitle role="heading" aria-level={4}>
                      尚无其他图片轮次
                    </EmptyTitle>
                    <EmptyDescription>
                      修订产生新候选后，可在此对照原图与修订图。
                    </EmptyDescription>
                  </EmptyHeader>
                </Empty>
              )}
              <figure className="flex min-w-0 flex-col gap-3 rounded-lg border p-4">
                <figcaption className="font-semibold">
                  当前检查的精确图片 · v{data.version.version_no}
                </figcaption>
                <VersionImage
                  key={data.image_path}
                  path={data.image_path}
                  alt={`当前检查图片 · ${data.version.id}`}
                  onUnavailable={unavailable}
                />
                <p className="break-all text-xs text-muted-foreground">
                  精确版本：{data.version.id}
                  <br />
                  SHA-256：{data.version.checksum}
                </p>
              </figure>
            </div>
            {data.images_truncated && (
              <Notice>
                比较列表已显示前 100 张图片；当前检查的精确图片始终保留。
              </Notice>
            )}
          </section>
          <Separator />
          <section
            aria-label="审查历史"
            className="flex min-w-0 flex-col gap-4"
          >
            <h3 className="font-semibold">逐轮审查</h3>
            {selected ? (
              <>
                <FieldGroup>
                  <Field>
                    <FieldLabel htmlFor={reasonId + "-review"}>
                      查看审查轮次
                    </FieldLabel>
                    <ChoiceSelect
                      id={reasonId + "-review"}
                      value={selected.record.review.result_id}
                      disabled={busy || loading}
                      onValueChange={setReviewId}
                      options={data.reviews.map((item) => ({
                        value: item.record.review.result_id,
                        label: `第 ${item.record.sequence} 次 · ${label(item.record.review.conclusion)}${item.record.review.result_id === data.latest_review_id ? " · 最新" : " · 历史"}`,
                      }))}
                    />
                  </Field>
                </FieldGroup>
                {!latest && (
                  <Notice>
                    正在查看历史审查。请切回最新审查后操作；旧结论不能替代当前门禁。
                  </Notice>
                )}
                <ReviewReport item={selected} onUnavailable={unavailable} />
              </>
            ) : (
              <Empty>
                <EmptyHeader>
                  <EmptyTitle role="heading" aria-level={4}>
                    尚无连续性审查记录
                  </EmptyTitle>
                  <EmptyDescription>
                    缺少必需审查时，候选图片不能批准或正式选用。
                  </EmptyDescription>
                </EmptyHeader>
              </Empty>
            )}
            {data.next_offset !== null && (
              <Button
                variant="outline"
                disabled={busy || loading}
                onClick={() => void load("reviews")}
              >
                加载更早审查
              </Button>
            )}
          </section>
          <Separator />
          <section
            aria-label="修订与反馈轮次"
            className="flex min-w-0 flex-col gap-4"
          >
            <h3 className="font-semibold">修订与反馈</h3>
            {!data.revisions.length && (
              <p className="text-sm text-muted-foreground">尚无修订请求。</p>
            )}
            {data.revisions.map(({ record, feedback }) => (
              <details
                key={record.revision_id}
                className="min-w-0 rounded-lg border p-4"
              >
                <summary className="cursor-pointer text-sm font-medium">
                  修订第 {record.round} 轮 / 最多 {record.max_rounds} 轮 ·{" "}
                  {label(record.status)}
                </summary>
                <div className="mt-3 flex min-w-0 flex-col gap-3 text-sm">
                  <p>
                    {label(record.request.action)} ·{" "}
                    <time dateTime={record.created_at}>
                      {new Date(record.created_at).toLocaleString()}
                    </time>
                  </p>
                  <p className="break-all text-xs text-muted-foreground">
                    修订：{record.revision_id}
                    <br />
                    来源审查：{record.request.review_id}
                    <br />
                    来源图片：{record.target.asset_version_id}
                    <br />
                    媒体任务：{record.job_id ?? "未创建（已达上限）"}
                  </p>
                  <p className="break-all text-xs text-muted-foreground">
                    问题：{record.request.issue_ids.join("、")}
                    <br />
                    依据：{record.evidence_ids.join("、")}
                  </p>
                  {!feedback.length && (
                    <p className="text-muted-foreground">尚无本轮反馈决定。</p>
                  )}
                  {feedback.map((item) => (
                    <div
                      key={item.feedback_id}
                      className="flex min-w-0 flex-col gap-1"
                    >
                      <p>
                        <strong>
                          反馈第 {item.sequence} 次 · {label(item.action)}
                        </strong>{" "}
                        · {label(item.source)}
                      </p>
                      <p>{item.reason}</p>
                      <p className="break-all text-xs text-muted-foreground">
                        {label(item.status)} · 审查 {item.review_id} ·{" "}
                        {new Date(item.created_at).toLocaleString()}
                      </p>
                    </div>
                  ))}
                </div>
              </details>
            ))}
          </section>
          <Separator />
          <section
            aria-label="人工决定与恢复"
            className="flex min-w-0 flex-col gap-4"
          >
            <h3 className="font-semibold">人工处理</h3>
            {!data.human_reviews.length && (
              <p className="text-sm text-muted-foreground">
                此精确图片尚无人工请求。缺少证据时请等待取证或审查流程交接。
              </p>
            )}
            {data.human_reviews.map((row) => (
              <HumanReviewActions
                key={row.id}
                row={row}
                allowed={data.actions.owner && data.actions.writable && latest}
                busy={busy || loading}
                mutate={mutate}
              />
            ))}
            {data.next_human_offset !== null && (
              <Button
                variant="outline"
                disabled={busy || loading}
                onClick={() => void load("humans")}
              >
                加载更早人工请求
              </Button>
            )}
          </section>
          <Separator />
          <section
            aria-label="版本审核与正式选用"
            className="flex min-w-0 flex-col gap-4"
          >
            <h3 className="font-semibold">版本审核与正式选用</h3>
            <p className="text-sm text-muted-foreground">
              人工例外恢复后仍需明确提交审核、批准版本，再正式选用。每次操作都复验当前权限、精确证据与版本状态。
            </p>
            <FieldGroup>
              <Field data-disabled={disabled}>
                <FieldLabel htmlFor={reasonId}>
                  版本审核意见（拒绝时必填）
                </FieldLabel>
                <Textarea
                  id={reasonId}
                  value={comment}
                  maxLength={1000}
                  disabled={disabled || !data.actions.writable}
                  onChange={(event) => setComment(event.target.value)}
                />
              </Field>
            </FieldGroup>
            <div className="flex flex-wrap gap-2">
              <Button
                variant="outline"
                disabled={disabled || !data.actions.can_submit}
                onClick={() => void change("READY_FOR_REVIEW")}
              >
                提交版本审核
              </Button>
              <Button
                variant="outline"
                disabled={disabled || !data.actions.can_approve}
                onClick={() => void change("APPROVED")}
              >
                批准此精确版本
              </Button>
              <ImpactButton
                variant="outline"
                endpoint={`/projects/${projectId}/dependency-impact?asset_version_id=${data.version.id}&change_kind=LIFECYCLE`}
                disabled={
                  disabled || !data.actions.can_reject || !comment.trim()
                }
                onConfirm={(digest) => change("REJECTED", digest)}
              >
                拒绝此精确版本
              </ImpactButton>
              <ImpactButton
                endpoint={
                  data.asset.current_version_id
                    ? `/projects/${projectId}/dependency-impact?asset_version_id=${data.asset.current_version_id}`
                    : undefined
                }
                disabled={
                  disabled ||
                  !data.actions.can_select ||
                  data.asset.current_version_id === data.version.id
                }
                onConfirm={(impact_digest) =>
                  mutate(
                    endpoint + "/select",
                    "POST",
                    {
                      expected_revision: data.asset.revision,
                      expected_lock_version: data.asset.lock_version,
                      impact_digest,
                    },
                    "已正式选用此精确版本。",
                  )
                }
              >
                正式选用此图片
              </ImpactButton>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function ComparisonDialog({
  projectId,
  initial,
  onDismiss,
  onChanged,
}: {
  projectId: string;
  initial: Resource;
  onDismiss: () => void;
  onChanged: () => Promise<void>;
}) {
  const title = useId();
  const [resource, setResource] = useState(initial);
  const [busy, setBusy] = useState(false);
  return (
    <Dialog
      labelledBy={title}
      busy={busy}
      onDismiss={onDismiss}
      className="w-[calc(100vw-2rem)] sm:max-w-6xl"
    >
      <div className="flex items-center justify-between gap-3">
        <h2 id={title} className="text-lg font-semibold">
          逐图比较与连续性审核
        </h2>
        <Button variant="outline" disabled={busy} onClick={onDismiss}>
          关闭比较
        </Button>
      </div>
      <ComparisonBody
        key={`${projectId}:${resource.assetId}:${resource.versionId}`}
        projectId={projectId}
        resource={resource}
        onNavigate={setResource}
        onChanged={onChanged}
        onBusy={setBusy}
      />
    </Dialog>
  );
}

export default function ContinuityComparisonButton({
  projectId,
  assetId,
  versionId,
  onChanged,
}: {
  projectId: string;
  assetId: string;
  versionId: string;
  onChanged: () => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button variant="outline" onClick={() => setOpen(true)}>
        逐图比较与审核
      </Button>
      {open && (
        <ComparisonDialog
          projectId={projectId}
          initial={{ assetId, versionId }}
          onDismiss={() => setOpen(false)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}
