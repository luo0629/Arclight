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
import { useEffect, useState } from "react";
import { Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";

export type ConceptPlan = {
  target_type: "CHARACTER" | "SCENE" | "PROP" | "STYLE";
  spec: {
    provider: string;
    model: string;
    model_config_version: string;
    prompt: string;
    prompt_version: string;
    batch_size: number;
    output_spec: { width: number; height: number; format: string };
  };
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
  const provider = value?.spec.provider;
  const model = value?.spec.model;
  const capabilityKey = JSON.stringify([projectId, provider, model]);
  const [batchLimit, setBatchLimit] = useState<{
    key: string;
    maximum: number;
  }>();
  const maxBatch = Math.min(
    4,
    batchLimit?.key === capabilityKey
      ? batchLimit.maximum
      : (value?.spec.batch_size ?? 1),
  );
  useEffect(() => {
    if (!provider || !model || batchLimit?.key === capabilityKey) return;
    const controller = new AbortController();
    void api<{ max_batch_size: number }>(
      `/projects/${projectId}/generation-jobs/providers/${encodeURIComponent(provider)}/capabilities?model=${encodeURIComponent(model)}`,
      { signal: controller.signal },
    )
      .then(({ max_batch_size }) => {
        if (!controller.signal.aborted)
          setBatchLimit({ key: capabilityKey, maximum: max_batch_size });
      })
      .catch(() => {
        // A saved batch can be displayed as-is; do not infer a larger capacity.
      });
    return () => controller.abort();
  }, [projectId, provider, model, batchLimit?.key, capabilityKey]);
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
        provider: string;
        enabled: boolean;
        message?: string;
        capabilities?: { max_batch_size: number };
        model: string;
        configuration_version: string;
      }>(`/projects/${projectId}/generation-jobs/services/image/configuration`);
      if (!config.enabled)
        throw new Error(
          config.message || "图像服务尚未启用，请先配置图像服务与可信报价。",
        );
      if (!config.capabilities)
        throw new Error("图像服务没有声明批量生成能力。");
      const maximum = Math.min(4, config.capabilities.max_batch_size);
      const batch = Math.min(maximum, value.spec.batch_size);
      setBatchLimit({
        key: JSON.stringify([projectId, config.provider, config.model]),
        maximum,
      });
      onChange({
        ...value,
        spec: {
          ...value.spec,
          provider: config.provider,
          batch_size: batch,
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
  function quantity(batch: number) {
    if (value)
      onChange({ ...value, spec: { ...value.spec, batch_size: batch } });
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
              value={value.spec.provider === "mock" ? "mock" : "configured"}
              disabled={disabled}
              onValueChange={(provider) => void chooseProvider(provider)}
              options={[
                { value: "mock", label: "本地 Mock（合成图片）" },
                { value: "configured", label: "我的图片服务" },
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
              disabled={disabled || value.spec.provider !== "mock"}
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
              max={maxBatch}
              required
              value={value.spec.batch_size}
              disabled={disabled}
              onChange={(event) => {
                const count = Number(event.target.value);
                if (count >= 1 && count <= maxBatch) quantity(count);
              }}
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
            候选图片用于美术比较，正式使用前需要审核。
          </p>
        </FieldGroup>
      )}
    </FieldSet>
  );
}
