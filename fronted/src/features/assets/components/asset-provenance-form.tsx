"use client";

import { useEffect, useState } from "react";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { Field, FieldLabel } from "@/components/ui/field";
import ListPagination from "@/components/shared/list-pagination";
import usePagedList from "@/hooks/use-paged-list";
import type { ExactAssetReference, ReferenceImage } from "@/types/api";

export type GenerationDeclaration = {
  provider: string;
  model: string;
  prompt: string;
  negative_prompt: string;
  prompt_version: string | null;
  seed: number | null;
  parameters: Record<string, string | number | boolean>;
  references: ExactAssetReference[];
};

export const emptyGeneration: GenerationDeclaration = {
  provider: "",
  model: "",
  prompt: "",
  negative_prompt: "",
  prompt_version: null,
  seed: null,
  parameters: {},
  references: [],
};

export default function AssetProvenanceForm({
  projectId,
  value,
  onChange,
}: {
  projectId: string;
  value: GenerationDeclaration;
  onChange: (value: GenerationDeclaration) => void;
}) {
  const list = usePagedList<ReferenceImage>(
    `/projects/${projectId}/reference-images`,
  );
  const [parameters, setParameters] = useState("");
  useEffect(() => {
    void list.reload().catch(() => {});
  }, [list.reload]);
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <p className="text-sm text-muted-foreground">
        以下为人工声明的外部生成信息，未经本系统验证；图片尺寸从原图读取。未提供的历史信息不会补造。
      </p>
      {(
        [
          ["provider", "生成服务", 160],
          ["model", "生成模型", 160],
          ["prompt_version", "提示词版本", 160],
        ] as const
      ).map(([key, label, max]) => (
        <Field key={key}>
          <FieldLabel htmlFor={`generation-${key}`}>{label}</FieldLabel>
          <Input
            id={`generation-${key}`}
            maxLength={max}
            required={key !== "prompt_version"}
            value={value[key] ?? ""}
            onChange={(event) =>
              onChange({ ...value, [key]: event.target.value || null })
            }
          />
        </Field>
      ))}
      {(
        [
          ["prompt", "生成提示词", 12000],
          ["negative_prompt", "负面提示词", 4000],
        ] as const
      ).map(([key, label, max]) => (
        <Field key={key}>
          <FieldLabel htmlFor={`generation-${key}`}>{label}</FieldLabel>
          <Textarea
            id={`generation-${key}`}
            required={key === "prompt"}
            maxLength={max}
            value={value[key]}
            onChange={(event) =>
              onChange({ ...value, [key]: event.target.value })
            }
          />
        </Field>
      ))}
      <Field>
        <FieldLabel htmlFor="generation-seed">生成 seed</FieldLabel>
        <Input
          id="generation-seed"
          type="number"
          min={0}
          max={Number.MAX_SAFE_INTEGER}
          step={1}
          value={value.seed ?? ""}
          onChange={(event) =>
            onChange({
              ...value,
              seed:
                event.target.value === "" ? null : Number(event.target.value),
            })
          }
        />
      </Field>
      <Field>
        <FieldLabel htmlFor="generation-parameters">
          生成参数（每行 名称=值）
        </FieldLabel>
        <Textarea
          id="generation-parameters"
          value={parameters}
          maxLength={6000}
          onChange={(event) => {
            const raw = event.target.value;
            setParameters(raw);
            const pairs = raw
              .split("\n")
              .filter((line) => line.trim())
              .map((line) => {
                const at = line.indexOf("=");
                return at > 0
                  ? [line.slice(0, at).trim(), line.slice(at + 1)]
                  : null;
              });
            const valid =
              pairs.every((pair) => pair !== null) && pairs.length <= 30;
            event.target.setCustomValidity(
              valid ? "" : "请每行填写 名称=值，最多30项",
            );
            if (valid)
              onChange({
                ...value,
                parameters: Object.fromEntries(pairs as string[][]),
              });
          }}
        />
      </Field>
      <p className="text-sm">生成参考图片（固定精确版本）</p>
      {list.rows
        .filter(
          (row) => !row.stale && ["APPROVED", "LOCKED"].includes(row.status),
        )
        .map((row) => (
          <label key={row.id} className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={value.references.some(
                (ref) => ref.asset_version_id === row.id,
              )}
              onChange={(event) =>
                onChange({
                  ...value,
                  references: event.target.checked
                    ? [
                        ...value.references,
                        { asset_id: row.asset_id, asset_version_id: row.id },
                      ]
                    : value.references.filter(
                        (ref) => ref.asset_version_id !== row.id,
                      ),
                })
              }
            />
            {row.asset_name} · v{row.version_no}
          </label>
        ))}
      <ListPagination {...list} count={list.rows.length} label="生成参考版本" />
    </div>
  );
}
