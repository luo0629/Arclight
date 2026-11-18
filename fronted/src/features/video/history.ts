import "client-only";

import { api, json } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import type { AssetVersion } from "@/types/api";

type HistoricalInput = {
  source_asset_version_id: string;
  checksum: string;
  dependency_type: string;
  selectors?: string[];
};
export type VideoRestoration = {
  schema: 1;
  source_version_id: string;
  expected_asset_revision: number;
  expected_version_revision: number;
  original_manifest_id: string;
  manifest_key: string;
  creation_key: string;
  dependency_manifest_id: string | null;
  comment: string;
  inputs: HistoricalInput[];
};

export function readVideoRestoration(
  key: string,
  versionId?: string,
): VideoRestoration | null {
  const saved = sessionStorage.getItem(key);
  if (!saved) return null;
  const row = JSON.parse(saved) as VideoRestoration;
  if (
    row.schema !== 1 ||
    ![
      row.source_version_id,
      row.original_manifest_id,
      row.manifest_key,
      row.creation_key,
    ].every(isResourceId) ||
    (versionId !== undefined && row.source_version_id !== versionId) ||
    (row.dependency_manifest_id !== null &&
      !isResourceId(row.dependency_manifest_id)) ||
    !Number.isSafeInteger(row.expected_asset_revision) ||
    !Number.isSafeInteger(row.expected_version_revision) ||
    !row.comment?.trim() ||
    !Array.isArray(row.inputs) ||
    !row.inputs.length ||
    !row.inputs.every(
      (input) =>
        isResourceId(input.source_asset_version_id) &&
        /^[a-f0-9]{64}$/i.test(input.checksum),
    )
  )
    throw new Error("历史视频另存记录无效，请先核对服务器结果。");
  return row;
}

export async function restoreVideoHistory({
  endpoint,
  storageKey,
  version,
  assetRevision,
  comment,
  signal,
  changed,
}: {
  endpoint: string;
  storageKey: string;
  version: AssetVersion;
  assetRevision: number;
  comment: string;
  signal: AbortSignal;
  changed: (operation: VideoRestoration | null) => void;
}) {
  const persist = (operation: VideoRestoration) => {
    sessionStorage.setItem(storageKey, JSON.stringify(operation));
    changed(operation);
  };
  let operation = readVideoRestoration(storageKey, version.id);
  if (!operation) {
    const original = await api<{
      original_manifest_id: string;
      inputs: HistoricalInput[];
    }>(endpoint + "/rebuild-inputs", { signal });
    if (
      !isResourceId(original.original_manifest_id) ||
      original.original_manifest_id !== version.dependency_manifest_id ||
      !Array.isArray(original.inputs) ||
      !original.inputs.length ||
      !original.inputs.every((input) =>
        isResourceId(input.source_asset_version_id),
      )
    )
      throw new Error("历史视频的精确输入清单不一致，已停止另存。");
    operation = {
      schema: 1,
      source_version_id: version.id,
      expected_asset_revision: assetRevision,
      expected_version_revision: version.revision,
      original_manifest_id: original.original_manifest_id,
      inputs: original.inputs,
      manifest_key: crypto.randomUUID(),
      creation_key: crypto.randomUUID(),
      dependency_manifest_id: null,
      comment: comment.trim(),
    };
    persist(operation);
  }
  if (!operation.dependency_manifest_id) {
    const manifest = await api<{ id: string }>(endpoint + "/rebuild-manifest", {
      ...json("POST", {
        creation_key: operation.manifest_key,
        expected_revision: operation.expected_version_revision,
        replacements: {},
      }),
      signal,
    });
    if (!isResourceId(manifest.id))
      throw new Error("历史视频重建清单回执无效。");
    operation = { ...operation, dependency_manifest_id: manifest.id };
    persist(operation);
  }
  const result = await api<AssetVersion>(endpoint + "/restore-as-new", {
    ...json("POST", {
      expected_revision: operation.expected_asset_revision,
      creation_key: operation.creation_key,
      comment: operation.comment,
      dependency_manifest_id: operation.dependency_manifest_id,
    }),
    signal,
  });
  if (
    result.asset_id !== version.asset_id ||
    result.restored_from_version_id !== version.id ||
    result.dependency_manifest_id !== operation.dependency_manifest_id ||
    result.checksum !== version.checksum ||
    result.status !== "DRAFT"
  )
    throw new Error("历史视频另存回执与原精确版本不一致，请核对服务器结果。");
  sessionStorage.removeItem(storageKey);
  changed(null);
  return result;
}
