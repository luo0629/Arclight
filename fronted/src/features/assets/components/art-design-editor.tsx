"use client";

import { useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Field, FieldGroup, FieldLabel } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import { ChoiceSelect } from "@/components/choice-select";
import { Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";
import type { VisualAsset } from "@/types/api";
import {
  artKinds,
  bibleFields,
  type ArtDesign,
  type ArtTarget,
  type BibleBaseline,
} from "@/features/assets/types/art-proposals";

export function newArtDesign(
  kind: Exclude<keyof typeof artKinds, "VISUAL_BIBLE"> = "CHARACTER",
): ArtDesign {
  return {
    design_id: crypto.randomUUID(),
    kind,
    target: { mode: "NEW" },
    name: "",
    metadata: { description: "" },
  };
}

export default function ArtDesignEditor({
  design,
  index,
  projectId,
  assets,
  onChange,
}: {
  design: ArtDesign;
  index: string;
  projectId: string;
  assets: VisualAsset[];
  onChange: (next: ArtDesign) => void;
}) {
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);
  const request = useRef(0);
  const basePath = `/projects/${projectId}/art-proposals/baselines`;
  async function kind(value: keyof typeof artKinds) {
    const sequence = ++request.current;
    setError("");
    if (value !== "VISUAL_BIBLE") {
      setLoading(false);
      onChange({ ...newArtDesign(value), design_id: design.design_id });
      return;
    }
    setLoading(true);
    try {
      const baseline = await api<BibleBaseline>(basePath + "/visual-bible");
      if (sequence === request.current)
        onChange({
          kind: value,
          design_id: design.design_id,
          version_id: baseline.version_id,
          version_no: baseline.version_no,
          content_sha256: baseline.content_sha256,
          fields:
            design.kind === "VISUAL_BIBLE"
              ? design.fields
              : { ...baseline.fields },
        });
    } catch (error) {
      if (sequence === request.current) setError(message(error));
    } finally {
      if (sequence === request.current) setLoading(false);
    }
  }
  async function target(value: string) {
    if (design.kind === "VISUAL_BIBLE") return;
    const sequence = ++request.current;
    setError("");
    if (value === "NEW") {
      onChange({ ...newArtDesign(design.kind), design_id: design.design_id });
      return;
    }
    const asset = assets.find((row) => row.id === value);
    if (!asset) return;
    setLoading(true);
    try {
      const baseline = await api<{
        target: ArtTarget;
        content: { name: string; metadata: VisualAsset["metadata"] };
      }>(`${basePath}/assets/${asset.id}`);
      if (sequence === request.current)
        onChange({
          kind: asset.asset_type,
          design_id: design.design_id,
          name: baseline.content.name,
          target: baseline.target,
          metadata: baseline.content.metadata,
        });
    } catch (error) {
      if (sequence === request.current) setError(message(error));
    } finally {
      if (sequence === request.current) setLoading(false);
    }
  }
  return (
    <fieldset disabled={loading} className="min-w-0 border-t pt-4">
      <legend className="text-sm font-semibold">设定 {index}</legend>
      {error && (
        <Notice tone="error" role="alert">
          {error}
        </Notice>
      )}
      <FieldGroup>
        <Field>
          <FieldLabel htmlFor={`kind-${design.design_id}`}>
            设定类型 {index}
          </FieldLabel>
          <ChoiceSelect
            id={`kind-${design.design_id}`}
            value={design.kind}
            onValueChange={(value) => void kind(value as keyof typeof artKinds)}
            options={Object.entries(artKinds).map(([value, label]) => ({
              value,
              label,
            }))}
          />
        </Field>
        {design.kind === "VISUAL_BIBLE" ? (
          <>
            <p className="text-sm text-muted-foreground">
              基于视觉规范 v{design.version_no}
              。锁定字段的变更需要Owner逐项确认。
            </p>
            {Object.entries(bibleFields).map(([key, label]) => (
              <Field key={key}>
                <FieldLabel htmlFor={`${design.design_id}-${key}`}>
                  {label}提案
                </FieldLabel>
                <Textarea
                  id={`${design.design_id}-${key}`}
                  maxLength={
                    key === "art_style"
                      ? 10000
                      : ["prohibited", "negative_prompt"].includes(key)
                        ? 4000
                        : 2000
                  }
                  value={design.fields[key as keyof typeof bibleFields] ?? ""}
                  onChange={(event) =>
                    onChange({
                      ...design,
                      fields: { ...design.fields, [key]: event.target.value },
                    })
                  }
                />
              </Field>
            ))}
          </>
        ) : (
          <>
            <Field>
              <FieldLabel htmlFor={`target-${design.design_id}`}>
                变更对象 {index}
              </FieldLabel>
              <ChoiceSelect
                id={`target-${design.design_id}`}
                value={
                  design.target.mode === "NEW" ? "NEW" : design.target.asset_id
                }
                onValueChange={(value) => void target(value)}
                options={[
                  { value: "NEW", label: "新建设定" },
                  ...assets
                    .filter(
                      (row) =>
                        row.asset_type === design.kind &&
                        row.status !== "ARCHIVED",
                    )
                    .map((row) => ({
                      value: row.id,
                      label:
                        row.name + (row.status === "LOCKED" ? " · 已锁定" : ""),
                    })),
                ]}
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={`name-${design.design_id}`}>
                设定名称 {index}
              </FieldLabel>
              <Input
                id={`name-${design.design_id}`}
                required
                maxLength={160}
                value={design.name}
                onChange={(event) =>
                  onChange({ ...design, name: event.target.value })
                }
              />
            </Field>
            <Field>
              <FieldLabel htmlFor={`desc-${design.design_id}`}>
                设定简介 {index}
              </FieldLabel>
              <Textarea
                id={`desc-${design.design_id}`}
                maxLength={4000}
                value={design.metadata?.description ?? ""}
                onChange={(event) =>
                  onChange({
                    ...design,
                    metadata: {
                      ...design.metadata,
                      description: event.target.value,
                    },
                  })
                }
              />
            </Field>
            <p className="text-sm text-muted-foreground">
              表单保留未编辑的详细设定与精确图片引用，正式变更以保存后的逐项比较为准。
            </p>
          </>
        )}
        {loading && <p role="status">正在读取精确基准…</p>}
      </FieldGroup>
      {design.kind === "VISUAL_BIBLE" && (
        <Button
          type="button"
          variant="ghost"
          onClick={() => void kind("VISUAL_BIBLE")}
        >
          重新读取规范基准
        </Button>
      )}
      {design.kind !== "VISUAL_BIBLE" && design.target.mode === "EXISTING" && (
        <Button
          type="button"
          variant="ghost"
          disabled={loading}
          onClick={async () => {
            if (design.target.mode !== "EXISTING") return;
            setLoading(true);
            setError("");
            try {
              const baseline = await api<{ target: ArtTarget }>(
                `${basePath}/assets/${design.target.asset_id}`,
              );
              onChange({ ...design, target: baseline.target });
            } catch (error) {
              setError(message(error));
            } finally {
              setLoading(false);
            }
          }}
        >
          更新对象基准并保留提议
        </Button>
      )}
    </fieldset>
  );
}
