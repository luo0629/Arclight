"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Badge } from "@/components/ui/badge";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import {
  artKinds,
  artStatus,
  changeLabel,
  valueText,
  type ArtRecord,
  type ArtVersion,
} from "@/features/assets/types/art-proposals";

export default function ArtProposalDetail({
  row,
  writable,
  owner,
  busy,
  onAction,
  onEdit,
  onVersion,
}: {
  row: ArtRecord;
  writable: boolean;
  owner: boolean;
  busy: boolean;
  onAction: (suffix: string, body: object) => void;
  onEdit: () => void;
  onVersion: (id: string) => void;
}) {
  const proposal = row.version.proposal_json;
  const report = row.version.comparison_json;
  const [chosen, setChosen] = useState(
    row.review_confirmation?.option_id ??
      proposal.preferred_option_id ??
      proposal.options[0]?.option_id ??
      "",
  );
  const [confirmed, setConfirmed] = useState<Set<string>>(new Set());
  const [comment, setComment] = useState("");
  const history = usePagedList<ArtVersion>(
    `/projects/${row.project_id}/art-proposals/${row.id}/versions`,
  );
  const [showHistory, setShowHistory] = useState(false);
  useEffect(() => {
    if (showHistory) void history.reload().catch(() => {});
  }, [showHistory, history.reload]);
  const option = proposal.options.find((option) => option.option_id === chosen);
  const changes =
    report.options.find((option) => option.option_id === chosen)?.changes ?? [];
  const historical = row.version.version_no !== row.current_version_no;
  const waiting =
    row.review?.status === "WAITING_REVIEW" && !historical && !row.archived;
  const allConfirmed =
    changes.length > 0 &&
    changes.every((change) =>
      confirmed.has(change.design_id + ":" + change.field_path),
    );
  function decide(value: "APPROVE" | "REJECT") {
    onAction("decisions", {
      version_id: row.version.id,
      expected_revision: row.revision,
      request_sha256: row.review?.request_sha256,
      decision: value,
      comment,
      ...(value === "APPROVE" ? { confirmation: row.review_confirmation } : {}),
    });
  }
  return (
    <article aria-label="美术提案详情" className="flex min-w-0 flex-col gap-4">
      <header className="flex flex-wrap items-center gap-2">
        <h3 className="text-lg font-semibold">{proposal.visual_goal}</h3>
        <Badge variant="secondary">{artStatus(row)}</Badge>
        <span className="text-sm text-muted-foreground">
          提案 v{row.version.version_no}
        </span>
      </header>
      {historical && (
        <Notice tone="info">
          正在查看历史版本，当前版本为 v{row.current_version_no}。
        </Notice>
      )}
      {row.review?.blocking_code && (
        <Notice tone="error">
          基准、权限或输入已变化，原审核不能继续。请重新读取当前内容并保存新修订。
        </Notice>
      )}
      <div className="flex flex-wrap gap-2">
        {writable && !row.archived && (
          <Button variant="outline" disabled={busy} onClick={onEdit}>
            修订当前提案
          </Button>
        )}
        <Button
          variant="ghost"
          disabled={busy}
          onClick={() => setShowHistory(!showHistory)}
        >
          {showHistory ? "收起提案历史" : "查看提案历史"}
        </Button>
        {writable && (
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => {
              if (
                window.confirm(
                  row.archived
                    ? "恢复这份美术提案？"
                    : "归档这份美术提案？历史内容将保留。",
                )
              )
                onAction("", {
                  expected_revision: row.revision,
                  archived: !row.archived,
                });
            }}
          >
            {row.archived ? "恢复美术提案" : "归档美术提案"}
          </Button>
        )}
      </div>
      {showHistory && (
        <section aria-label="提案历史版本" className="flex flex-col gap-2">
          <div className="flex flex-wrap gap-2">
            {history.rows.map((version) => (
              <Button
                key={version.id}
                variant="outline"
                disabled={busy}
                onClick={() => onVersion(version.id)}
              >
                查看提案 v{version.version_no}
              </Button>
            ))}
          </div>
          <ListPagination
            {...history}
            count={history.rows.length}
            label="美术提案历史"
          />
        </section>
      )}
      <section className="flex min-w-0 flex-col gap-2">
        <h4 className="font-semibold">来源与约束</h4>
        {proposal.sources.map((source) => (
          <p
            key={source.source_id}
            className="break-words text-sm text-muted-foreground"
          >
            {source.kind === "MANUAL"
              ? source.statement
              : `精确${source.resource_type}版本：${source.resource_id}`}
          </p>
        ))}
        {proposal.constraints?.map((text) => (
          <p key={text} className="text-sm">
            {text}
          </p>
        ))}
        {proposal.references?.map((reference) => (
          <p key={reference.reference_id} className="text-sm">
            参考用途：{reference.purpose} · 精确版本{" "}
            {reference.asset.asset_version_id}
          </p>
        ))}
      </section>
      {proposal.questions?.map((question) => (
        <Notice key={question.question_id} tone="info">
          未决问题：{question.question}
        </Notice>
      ))}
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor="art-review-option">比较方案</FieldLabel>
          <ChoiceSelect
            id="art-review-option"
            value={chosen}
            disabled={busy || !!row.review}
            onValueChange={(value) => {
              setChosen(value);
              setConfirmed(new Set());
            }}
            options={proposal.options.map((option) => ({
              value: option.option_id,
              label: option.title,
            }))}
          />
        </Field>
      </FieldGroup>
      {option && (
        <>
          <p className="whitespace-pre-wrap text-sm">{option.rationale}</p>
          {option.prompts.map((prompt) => (
            <div key={prompt.fragment_id} className="min-w-0">
              <p className="text-sm font-semibold">
                {prompt.kind === "POSITIVE" ? "正向提示词" : "负向提示词"}
              </p>
              <p className="whitespace-pre-wrap break-words text-sm text-muted-foreground">
                {prompt.text}
              </p>
            </div>
          ))}
        </>
      )}
      <section
        aria-label="正式变更比较"
        className="flex min-w-0 flex-col gap-3"
      >
        <h4 className="font-semibold">逐项变更</h4>
        {changes.length === 0 && (
          <p className="text-sm text-muted-foreground">
            所选方案没有内容变化。
          </p>
        )}
        {changes.map((change) => {
          const key = change.design_id + ":" + change.field_path;
          const label =
            artKinds[change.target_type] + " / " + changeLabel(change);
          return (
            <div key={key} className="min-w-0 border-t pt-3">
              <div className="flex flex-wrap items-center gap-2">
                <p className="text-sm font-semibold">{label}</p>
                {change.protection !== "NONE" && (
                  <Badge variant="outline">
                    {change.protection === "LOCKED" ? "已锁定" : "已批准"}
                  </Badge>
                )}
              </div>
              <dl className="mt-2 grid min-w-0 gap-2 text-sm md:grid-cols-2">
                <div className="min-w-0">
                  <dt className="text-muted-foreground">当前内容</dt>
                  <dd className="whitespace-pre-wrap break-words">
                    {valueText(change.before)}
                  </dd>
                </div>
                <div className="min-w-0">
                  <dt className="text-muted-foreground">提议内容</dt>
                  <dd className="whitespace-pre-wrap break-words">
                    {valueText(change.proposed)}
                  </dd>
                </div>
              </dl>
              {waiting && owner && (
                <Field orientation="horizontal" className="mt-2">
                  <input
                    id={`confirm-${key}`}
                    type="checkbox"
                    disabled={busy}
                    checked={confirmed.has(key)}
                    onChange={(event) => {
                      const next = new Set(confirmed);
                      if (event.target.checked) next.add(key);
                      else next.delete(key);
                      setConfirmed(next);
                    }}
                  />
                  <FieldLabel htmlFor={`confirm-${key}`}>
                    确认变更：{label}
                  </FieldLabel>
                </Field>
              )}
            </div>
          );
        })}
      </section>
      {writable && !row.review && !row.archived && !historical && (
        <Button
          disabled={busy || !changes.length || !!proposal.questions?.length}
          onClick={() =>
            onAction("submit", {
              version_id: row.version.id,
              expected_revision: row.revision,
              option_id: chosen,
              comparison_sha256: report.comparison_sha256,
            })
          }
        >
          提交美术提案审核
        </Button>
      )}
      {waiting && owner && (
        <>
          <FieldGroup>
            <Field>
              <FieldLabel htmlFor="art-review-comment">美术审核意见</FieldLabel>
              <Textarea
                id="art-review-comment"
                maxLength={2000}
                value={comment}
                disabled={busy}
                onChange={(event) => setComment(event.target.value)}
              />
            </Field>
          </FieldGroup>
          <div className="flex flex-wrap gap-2">
            <Button
              disabled={busy || !allConfirmed}
              onClick={() => decide("APPROVE")}
            >
              批准并生成正式版本
            </Button>
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => decide("REJECT")}
            >
              拒绝美术提案
            </Button>
          </div>
        </>
      )}
      {waiting && !owner && (
        <Notice tone="info">等待项目Owner审核；批准前正式资产保持原值。</Notice>
      )}
      {owner && row.review?.status === "DECIDED" && row.review.decision && (
        <Button
          disabled={busy || row.archived || historical}
          onClick={() =>
            onAction("resume", {
              decision_id: row.review?.decision?.id,
              request_sha256: row.review?.request_sha256,
            })
          }
        >
          使已保存决定生效
        </Button>
      )}
      {row.review?.decision && (
        <Notice tone="info">
          {row.review.decision.decision_json.decision === "APPROVE"
            ? "批准"
            : "拒绝"}
          意见：{row.review.decision.decision_json.comment || "未填写意见"} ·{" "}
          {new Date(row.review.decision.created_at).toLocaleString()} · 操作者{" "}
          {row.review.decision.actor_id}
        </Notice>
      )}
      {row.outputs.map((output) => (
        <Notice key={output.id} tone="success">
          已生成{artKinds[output.target_type]}正式版本 · 修订{" "}
          {output.version_no} · 版本{" "}
          {output.visual_bible_version_id ?? output.id}
        </Notice>
      ))}
    </article>
  );
}
