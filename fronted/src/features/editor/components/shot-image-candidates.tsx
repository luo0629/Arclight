"use client";

import { useId } from "react";
import {
  NativeSelect,
  NativeSelectOption,
} from "@/components/ui/native-select";
import { FormLabel, Notice, StatusBadge } from "@/components/ui-kit";
import { Badge } from "@/components/ui/badge";
import VersionImage from "@/features/assets/components/version-image";
import ContinuityComparisonButton from "@/features/continuity/components/continuity-comparison";
import {
  candidateImagePath,
  type ProductionShot,
} from "../shot-production-types";
import ShotRevisionButton from "./shot-revision";

export default function ShotImageCandidates({
  projectId,
  episodeId,
  storyboardId,
  shot,
  selectedId,
  disabled,
  writable,
  onSelect,
  onChanged,
  onUnavailable,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shot: ProductionShot;
  selectedId: string;
  disabled: boolean;
  writable: boolean;
  onSelect: (id: string) => void;
  onChanged: () => Promise<void>;
  onUnavailable: (cause: unknown) => void;
}) {
  const id = useId();
  const candidateIndex = shot.candidates.findIndex(
    (row) => row.id === selectedId,
  );
  const candidate = shot.candidates[candidateIndex];
  const path = candidate && candidateImagePath(projectId, candidate);

  if (!shot.candidates.length)
    return <p className="text-sm text-muted-foreground">暂无可用图片候选</p>;

  return (
    <div className="flex min-w-0 flex-col gap-3">
      <FormLabel htmlFor={id}>
        镜头 {shot.shot_no} 审核候选
        <NativeSelect
          id={id}
          aria-label={`镜头 ${shot.shot_no} 审核候选`}
          value={candidate?.id ?? ""}
          disabled={disabled}
          onChange={(event) => onSelect(event.target.value)}
        >
          <NativeSelectOption value="">选择并查看精确候选</NativeSelectOption>
          {shot.candidates.map((row, index) => (
            <NativeSelectOption key={row.id} value={row.id}>
              候选 {index + 1} · v{row.version_no} · {row.status}
              {row.version_id === shot.selected_image_version_id &&
                " · 当前选用"}
            </NativeSelectOption>
          ))}
        </NativeSelect>
      </FormLabel>
      {candidate && !path && (
        <Notice tone="error" role="alert">
          候选图片与当前精确版本不一致，请刷新后核对。
        </Notice>
      )}
      {candidate && path && (
        <>
          <VersionImage
            key={candidate.version_id}
            path={path}
            compact
            alt={`镜头 ${shot.shot_no} 候选 ${candidateIndex + 1} · v${candidate.version_no}`}
            onUnavailable={onUnavailable}
          />
          <div className="flex flex-wrap items-center gap-2">
            <StatusBadge status={candidate.status} />
            {candidate.version_id === shot.selected_image_version_id && (
              <Badge variant="secondary">当前选用</Badge>
            )}
            <p className="text-xs text-muted-foreground">
              候选版本 v{candidate.version_no}
            </p>
          </div>
          {candidate.blocker && (
            <p className="text-sm text-destructive">
              {candidate.blocker.message}
            </p>
          )}
          <div className="flex flex-wrap gap-2">
            <ContinuityComparisonButton
              projectId={projectId}
              assetId={candidate.asset_id}
              versionId={candidate.version_id}
              onChanged={onChanged}
            />
            <ShotRevisionButton
              projectId={projectId}
              episodeId={episodeId}
              storyboardId={storyboardId}
              shotId={shot.id}
              shotNo={shot.shot_no}
              candidate={candidate}
              disabled={disabled || !writable}
              onChanged={onChanged}
              onUnavailable={onUnavailable}
            />
          </div>
        </>
      )}
      {shot.candidates_truncated && (
        <p className="text-xs text-muted-foreground">
          此处最多显示 16 张候选，完整历史可在逐图比较或资产中心查看。
        </p>
      )}
    </div>
  );
}
