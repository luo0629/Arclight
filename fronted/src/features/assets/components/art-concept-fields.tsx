"use client";

import {
  Field,
  FieldGroup,
  FieldLabel,
  FieldLegend,
  FieldSet,
} from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { ChoiceSelect } from "@/components/choice-select";
import { useState } from "react";
import { Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";

export type ConceptPlan = {
  target_type: "CHARACTER" | "SCENE" | "PROP" | "STYLE";
  spec: {
    provider: "mock" | "openai";
    model: string;
    model_config_version: string;
    prompt: string;
    prompt_version: string;
    batch_size: number;
    output_spec: { width: number; height: number; format: string };
  };
  maximum_rounds: number;
  maximum_candidates: number;
  maximum_cost: string;
};

function initialPlan(): ConceptPlan {
  return {
    target_type: "STYLE",
    spec: {
      provider: "mock",
      model: "mock-image-v1",
      model_config_version: "concept-ui/1",
      prompt: "探索视觉方向，生成可比较的概念草图。",
      prompt_version: "concept-ui/1",
      batch_size: 2,
      output_spec: { width: 1024, height: 1024, format: "png" },
    },
    maximum_rounds: 2,
    maximum_candidates: 4,
    maximum_cost: "2",
  };
}

export default function ArtConceptFields({
  projectId,
  value,
  onChange,
  disabled,
}: {
  projectId: string;
  value: ConceptPlan | null;
  onChange: (value: ConceptPlan | null) => void;
  disabled: boolean;
}) {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  async function chooseProvider(provider: string) {
    if (!value) return;
    setError("");
    if (provider === "mock") {
      onChange({
        ...value,
        spec: {
          ...value.spec,
          provider: "mock",
          model: "mock-image-v1",
          model_config_version: "concept-ui/1",
        },
      });
      return;
    }
    setLoading(true);
    try {
      const config = await api<{
        enabled: boolean;
        model: string;
        configuration_version: string;
      }>(
        `/projects/${projectId}/generation-jobs/providers/openai/configuration`,
      );
      if (!config.enabled)
        throw new Error("图像服务尚未启用，请先配置图像服务与可信报价。");
      onChange({
        ...value,
        spec: {
          ...value.spec,
          provider: "openai",
          model: config.model,
          model_config_version: config.configuration_version,
        },
      });
    } catch (error) {
      setError(message(error));
    } finally {
      setLoading(false);
    }
  }
  function quantity(batch: number, rounds: number) {
    if (!value) return;
    const boundedRounds = Math.min(rounds, Math.floor(8 / batch));
    onChange({
      ...value,
      spec: { ...value.spec, batch_size: batch },
      maximum_rounds: boundedRounds,
      maximum_candidates: batch * boundedRounds,
    });
  }
  return (
    <FieldSet disabled={disabled || loading}>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <FieldLegend>概念图比较</FieldLegend>
      <Field orientation="horizontal">
        <input
          id="art-concepts"
          type="checkbox"
          checked={!!value}
          disabled={disabled}
          onChange={(event) =>
            onChange(event.target.checked ? initialPlan() : null)
          }
        />
        <FieldLabel htmlFor="art-concepts">生成概念图并比较修订</FieldLabel>
      </Field>
      {value && (
        <FieldGroup>
          <Field>
            <FieldLabel htmlFor="art-concept-provider">概念图服务</FieldLabel>
            <ChoiceSelect
              id="art-concept-provider"
              value={value.spec.provider}
              disabled={disabled}
              onValueChange={(provider) => void chooseProvider(provider)}
              options={[
                { value: "mock", label: "本地 Mock（合成图片）" },
                { value: "openai", label: "已配置的 OpenAI 图像服务" },
              ]}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="art-concept-model">概念图模型</FieldLabel>
            <Input
              id="art-concept-model"
              required
              maxLength={160}
              value={value.spec.model}
              disabled={disabled}
              onChange={(event) =>
                onChange({
                  ...value,
                  spec: { ...value.spec, model: event.target.value },
                })
              }
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="art-concept-target">概念图类型</FieldLabel>
            <ChoiceSelect
              id="art-concept-target"
              value={value.target_type}
              disabled={disabled}
              onValueChange={(target) =>
                onChange({
                  ...value,
                  target_type: target as ConceptPlan["target_type"],
                })
              }
              options={[
                { value: "STYLE", label: "风格" },
                { value: "CHARACTER", label: "角色" },
                { value: "SCENE", label: "场景" },
                { value: "PROP", label: "道具" },
              ]}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="art-concept-count">每轮候选数量</FieldLabel>
            <Input
              id="art-concept-count"
              type="number"
              min={1}
              max={4}
              required
              value={value.spec.batch_size}
              disabled={disabled}
              onChange={(event) => {
                const count = Number(event.target.value);
                if (count >= 1 && count <= 4)
                  quantity(count, value.maximum_rounds);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="art-concept-rounds">最多概念图轮次</FieldLabel>
            <Input
              id="art-concept-rounds"
              type="number"
              min={1}
              max={Math.min(4, Math.floor(8 / value.spec.batch_size))}
              required
              value={value.maximum_rounds}
              disabled={disabled}
              onChange={(event) => {
                const rounds = Number(event.target.value);
                if (rounds >= 1 && rounds <= 4)
                  quantity(value.spec.batch_size, rounds);
              }}
            />
          </Field>
          <Field>
            <FieldLabel htmlFor="art-concept-budget">
              概念图总预算（USD）
            </FieldLabel>
            <Input
              id="art-concept-budget"
              type="number"
              min={0}
              max={10000}
              step="0.000001"
              required
              value={value.maximum_cost}
              disabled={disabled}
              onChange={(event) =>
                onChange({ ...value, maximum_cost: event.target.value })
              }
            />
          </Field>
          {(["width", "height"] as const).map((dimension) => (
            <Field key={dimension}>
              <FieldLabel htmlFor={`art-concept-${dimension}`}>
                概念图{dimension === "width" ? "宽度" : "高度"}（像素）
              </FieldLabel>
              <Input
                id={`art-concept-${dimension}`}
                type="number"
                min={1}
                max={8192}
                required
                value={value.spec.output_spec[dimension]}
                disabled={disabled}
                onChange={(event) =>
                  onChange({
                    ...value,
                    spec: {
                      ...value.spec,
                      output_spec: {
                        ...value.spec.output_spec,
                        [dimension]: Number(event.target.value),
                      },
                    },
                  })
                }
              />
            </Field>
          ))}
          <p className="text-sm text-muted-foreground">
            最多 {value.maximum_candidates}{" "}
            张候选。开始时核验能力与可信报价并冻结计划；每轮生成使用原预算，正式参考须经人工批准。
          </p>
        </FieldGroup>
      )}
    </FieldSet>
  );
}
