"use client";

import { useId, useState } from "react";
import Dialog from "@/components/shared/app-dialog";
import { Button } from "@/components/ui/button";
import {
  Field,
  FieldDescription,
  FieldGroup,
  FieldLabel,
} from "@/components/ui/field";
import { Textarea } from "@/components/ui/textarea";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";
import { Notice, StatusBadge } from "@/components/ui-kit";
import VersionImage from "@/features/assets/components/version-image";
import useShotRevision from "../hooks/use-shot-revision";
import type { ProductionCandidate } from "../shot-production-types";
import {
  revisionOperations,
  type RevisionOperation,
} from "../shot-revision-types";

type RevisionProps = {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shotId: string;
  shotNo: number;
  candidate: ProductionCandidate;
  onChanged: () => Promise<void>;
  onUnavailable: (cause: unknown) => void;
};

function RevisionBody(props: RevisionProps & { onDismiss: () => void }) {
  const id = useId();
  const state = useShotRevision(
    props.projectId,
    props.episodeId,
    props.storyboardId,
    props.shotId,
    props.candidate,
    props.onChanged,
    props.onUnavailable,
  );
  const frozen = state.busy || state.blocked || Boolean(state.pending);
  return (
    <Dialog
      labelledBy={id}
      busy={state.busy}
      onDismiss={props.onDismiss}
      className="sm:max-w-3xl"
    >
      <div className="flex items-start justify-between gap-3">
        <div>
          <h2 id={id} className="text-lg font-semibold">
            单镜头局部修订
          </h2>
          <p className="text-sm text-muted-foreground">
            镜头 {props.shotNo} · 来源 v{props.candidate.version_no}
          </p>
        </div>
        <Button variant="ghost" disabled={state.busy} onClick={props.onDismiss}>
          关闭局部修订
        </Button>
      </div>
      <Notice>
        按指定要求整图重生成，原图仅供比较，其他区域也可能变化。当前不提供遮罩编辑或像素保持；每次只生成一张新候选，重新审查后再决定是否选用。
      </Notice>
      <div className="grid min-w-0 gap-5 sm:grid-cols-[minmax(0,1fr)_minmax(0,1.4fr)]">
        <div className="flex min-w-0 flex-col gap-3">
          <VersionImage
            path={props.candidate.image_path}
            alt={`修订来源镜头 ${props.shotNo} 原图`}
            onUnavailable={state.unavailable}
          />
          <StatusBadge status={props.candidate.status} />
          <p className="text-sm text-muted-foreground">
            锁定规范与已批准参考继续生效；替换受保护场景或服装时，请先另存并批准参考及分镜。
          </p>
        </div>
        <FieldGroup>
          <Field data-disabled={frozen}>
            <FieldLabel id={`${id}-operation`}>修订范围</FieldLabel>
            <ToggleGroup
              type="single"
              variant="outline"
              value={state.operation}
              disabled={frozen}
              aria-labelledby={`${id}-operation`}
              className="flex flex-wrap justify-start"
              onValueChange={(value) => {
                if (Object.hasOwn(revisionOperations, value))
                  state.edit(value as RevisionOperation, state.instruction);
              }}
            >
              {Object.entries(revisionOperations).map(([value, label]) => (
                <ToggleGroupItem key={value} value={value} aria-label={label}>
                  {label}
                </ToggleGroupItem>
              ))}
            </ToggleGroup>
          </Field>
          <Field data-disabled={frozen}>
            <FieldLabel htmlFor={`${id}-instruction`}>具体修订要求</FieldLabel>
            <Textarea
              id={`${id}-instruction`}
              value={state.instruction}
              disabled={frozen}
              maxLength={2000}
              rows={5}
              placeholder="说明希望怎样调整，保留哪些人物、动作和画面关系。"
              onChange={(event) =>
                state.edit(state.operation, event.target.value)
              }
            />
            <FieldDescription>
              仅追加本次范围内的要求，保留原身份、参考及负面约束。最多 2000 字。
            </FieldDescription>
          </Field>
          {!state.pending && (
            <Button
              variant="outline"
              disabled={frozen || !state.instruction.trim()}
              onClick={() => void state.prepare()}
            >
              {state.busy ? "正在核对修订" : "预览修订与预算"}
            </Button>
          )}
        </FieldGroup>
      </div>
      {state.error && (
        <Notice tone="error" role="alert">
          {state.error}
        </Notice>
      )}
      {state.preview && (
        <section
          className="flex min-w-0 flex-col gap-3 rounded-lg border p-4"
          aria-label="单镜头修订预览与结果"
        >
          <h3 className="font-semibold">
            {revisionOperations[state.preview.intent.operation]} · 1 张新候选
          </h3>
          <p className="break-words text-sm">
            原服务：{state.preview.source.provider} /{" "}
            {state.preview.source.model}
          </p>
          <p className="text-sm">
            预算上限估算：{state.preview.quote.maximum_cost}{" "}
            {state.preview.quote.currency}
          </p>
          <p className="text-sm text-muted-foreground">
            {state.preview.mode_description}
          </p>
          {state.preview.preserved_constraints.map((line) => (
            <p key={line} className="text-sm text-muted-foreground">
              {line}
            </p>
          ))}
          {state.pending?.phase === "accepted" ? (
            <>
              <p role="status" className="break-all text-sm">
                已受理修订任务
                。完成后刷新镜头候选并比较审核；失败时在任务中心重试原任务，未知结果先核验。
              </p>
              <Button
                variant="outline"
                disabled={state.busy || state.blocked}
                onClick={state.restart}
              >
                重新填写修订要求
              </Button>
            </>
          ) : (
            <>
              {state.pending && (
                <Notice>
                  提交结果待确认。继续时重放原请求与原幂等键，不创建另一任务。
                </Notice>
              )}
              <Button
                disabled={state.busy || state.blocked}
                onClick={() => void state.submit()}
              >
                {state.busy
                  ? "正在提交修订"
                  : state.pending
                    ? "确认原修订提交结果"
                    : "确认生成修订候选"}
              </Button>
            </>
          )}
        </section>
      )}
    </Dialog>
  );
}

export default function ShotRevisionButton(
  props: RevisionProps & { disabled: boolean },
) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <Button
        variant="outline"
        disabled={props.disabled}
        onClick={() => setOpen(true)}
        aria-label={`局部修订镜头 ${props.shotNo}`}
      >
        局部修订
      </Button>
      {open && (
        <RevisionBody
          key={props.candidate.id}
          {...props}
          onDismiss={() => setOpen(false)}
        />
      )}
    </>
  );
}
