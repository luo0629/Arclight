"use client";

import { useId } from "react";
import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldDescription,
  FieldSet,
  FieldLegend,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ToggleGroup, ToggleGroupItem } from "@/components/ui/toggle-group";

export type CreationOptions = {
  professional: boolean;
  review: boolean;
  revisionRounds: string;
  cost: string;
  seconds: string;
  calls: string;
};
export const initialCreationOptions: CreationOptions = {
  professional: false,
  review: false,
  revisionRounds: "3",
  cost: "",
  seconds: "300",
  calls: "32",
};
export function validCreationOptions(value: CreationOptions) {
  return (
    !value.professional ||
    (Number.isInteger(Number(value.seconds)) &&
      Number(value.seconds) >= 1 &&
      Number(value.seconds) <= 86400 &&
      Number.isInteger(Number(value.calls)) &&
      Number(value.calls) >= 1 &&
      Number(value.calls) <= 1024 &&
      (!value.review ||
        (value.revisionRounds.trim() !== "" &&
          Number.isInteger(Number(value.revisionRounds)) &&
          Number(value.revisionRounds) >= 0 &&
          Number(value.revisionRounds) <= 8)) &&
      (!value.cost ||
        (/^\d+(\.\d{1,6})?$/.test(value.cost) && Number(value.cost) < 1e12)))
  );
}
export function creationPayload(value: CreationOptions) {
  return value.professional
    ? {
        professional: true,
        ...(value.review
          ? { revision_rounds: Number(value.revisionRounds) }
          : {}),
        agent_limits: {
          maximum_cost: value.cost || null,
          max_duration_seconds: Number(value.seconds),
          max_model_calls: Number(value.calls),
          max_steps: Number(value.calls),
        },
      }
    : {};
}
export function isCreationOptions(value: unknown): value is CreationOptions {
  if (!value || typeof value !== "object") return false;
  const row = value as Record<string, unknown>;
  return (
    typeof row.professional === "boolean" &&
    typeof row.review === "boolean" &&
    ["revisionRounds", "cost", "seconds", "calls"].every(
      (key) => typeof row[key] === "string",
    )
  );
}
export default function CreationOptionsFields({
  value,
  onChange,
  disabled,
  allowReview = true,
  rewrite = false,
}: {
  value: CreationOptions;
  onChange: (value: CreationOptions) => void;
  disabled: boolean;
  allowReview?: boolean;
  rewrite?: boolean;
}) {
  const id = useId();
  return (
    <FieldSet disabled={disabled}>
      <FieldLegend variant="label">创作方式</FieldLegend>
      <FieldGroup>
        <Field>
          <ToggleGroup
            className="w-full flex-wrap"
            type="single"
            variant="outline"
            aria-label="创作方式"
            value={
              value.professional
                ? !rewrite && value.review
                  ? "reviewed"
                  : "professional"
                : "standard"
            }
            onValueChange={(mode) => {
              if (mode)
                onChange({
                  ...value,
                  professional: mode !== "standard",
                  review:
                    mode === "reviewed" || (rewrite && mode === "professional"),
                });
            }}
          >
            <ToggleGroupItem value="standard">
              {rewrite ? "直接改写" : "直接起稿"}
            </ToggleGroupItem>
            <ToggleGroupItem value="professional">
              {rewrite ? "调查、规划与评价改写" : "调查、规划与分段创作"}
            </ToggleGroupItem>
            {allowReview && !rewrite && (
              <ToggleGroupItem value="reviewed">
                创作、评价与局部修订
              </ToggleGroupItem>
            )}
          </ToggleGroup>
          {value.professional && (
            <FieldDescription>
              {value.review
                ? "结合剧情评价和规则检查，按限定范围修订；无法解决的问题会请你决定。最终稿仍须审核。"
                : "先调查并保存计划，再逐段创作；遇到问题会等待你的回答。完整稿仍须审核。"}
            </FieldDescription>
          )}
        </Field>
        {value.professional && (
          <>
            {value.review && (
              <Field>
                <FieldLabel htmlFor={id + "-rounds"}>
                  最多自动修订轮数
                </FieldLabel>
                <Input
                  id={id + "-rounds"}
                  type="number"
                  min={0}
                  max={8}
                  step={1}
                  value={value.revisionRounds}
                  onChange={(e) =>
                    onChange({ ...value, revisionRounds: e.target.value })
                  }
                />
                <FieldDescription>
                  修订后重新检查；额度用完会保留问题并请你决定。
                  {rewrite && "首次按你的指令改写不计入修订轮数。"}
                </FieldDescription>
              </Field>
            )}
            <Field>
              <FieldLabel htmlFor={id + "-cost"}>
                本次可选费用上限（USD）
              </FieldLabel>
              <Input
                id={id + "-cost"}
                type="number"
                min="0"
                step="0.000001"
                placeholder="留空使用供应商余额或额度"
                value={value.cost}
                onChange={(e) => onChange({ ...value, cost: e.target.value })}
              />
              <FieldDescription>
                设置上限需要可信报价；当前费用以调用记录为准。
              </FieldDescription>
            </Field>
            <Field>
              <FieldLabel htmlFor={id + "-seconds"}>
                总执行时限（秒）
              </FieldLabel>
              <Input
                id={id + "-seconds"}
                type="number"
                min={1}
                max={86400}
                step={1}
                value={value.seconds}
                onChange={(e) =>
                  onChange({ ...value, seconds: e.target.value })
                }
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={id + "-calls"}>最多模型调用次数</FieldLabel>
              <Input
                id={id + "-calls"}
                type="number"
                min={1}
                max={1024}
                step={1}
                value={value.calls}
                onChange={(e) => onChange({ ...value, calls: e.target.value })}
              />
              <FieldDescription>
                调查、创作与后续分镜共用本次限制；重试和人工回答不会重置。
              </FieldDescription>
            </Field>
          </>
        )}
      </FieldGroup>
    </FieldSet>
  );
}
