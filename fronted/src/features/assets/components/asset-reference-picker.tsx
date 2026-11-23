"use client";

import { useEffect, useId } from "react";
import { ChoiceSelect } from "@/components/choice-select";
import { FormLabel } from "@/components/ui-kit";
import usePagedList from "@/hooks/use-paged-list";
import type { ExactAssetReference, ReferenceImage } from "@/types/api";

export function useReferenceImages(projectId: string, enabled: boolean) {
  const list = usePagedList<ReferenceImage>(
    `/projects/${projectId}/reference-images`,
  );
  useEffect(() => {
    if (enabled) void list.reload().catch(() => {});
  }, [enabled, list.reload]);
  return list;
}

export default function AssetReferencePicker({
  label,
  kind,
  rows,
  value,
  disabled,
  optional,
  onChange,
}: {
  label: string;
  kind: string;
  rows: ReferenceImage[];
  value: ExactAssetReference | null;
  disabled?: boolean;
  optional?: boolean;
  onChange: (value: ExactAssetReference | null) => void;
}) {
  const id = useId();
  const matches = rows.filter((row) => row.asset_type === kind);
  const current = matches.find(
    (row) =>
      row.id === value?.asset_version_id && row.asset_id === value.asset_id,
  );
  const available = matches.filter(
    (row) => !row.stale && ["APPROVED", "LOCKED"].includes(row.status),
  );
  const describe = (row: ReferenceImage) =>
    `${row.asset_name} · v${row.version_no}`;
  const options = available.map((row) => ({
    value: row.id,
    label: describe(row),
  }));
  if (
    value?.asset_version_id &&
    !options.some((row) => row.value === value.asset_version_id)
  ) {
    options.push({
      value: value.asset_version_id,
      label: current
        ? `${describe(current)}（当前不可用）`
        : "已绑定的版本（当前列表未显示）",
    });
  }
  if (optional) options.unshift({ value: "NONE", label: "未关联" });
  return (
    <FormLabel htmlFor={id}>
      {label}
      <ChoiceSelect
        id={id}
        aria-label={label}
        disabled={disabled || !options.length}
        value={value?.asset_version_id || (optional ? "NONE" : "")}
        placeholder="选择已批准的资产版本"
        options={options}
        onValueChange={(selected) => {
          const row = available.find((item) => item.id === selected);
          if (row)
            onChange({ asset_id: row.asset_id, asset_version_id: row.id });
          else if (optional && selected === "NONE") onChange(null);
        }}
      />
    </FormLabel>
  );
}
