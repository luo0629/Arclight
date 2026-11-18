import "client-only";

import { api, apiBlob, json } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import { sha256 } from "@/features/audio/api";
import type { VideoCandidate, VideoAsset } from "./types";

export const candidatePath = (projectId: string, candidateId: string) => {
  if (![projectId, candidateId].every(isResourceId))
    throw new Error("视频候选标识无效。");
  return "/projects/" + projectId + "/generation-candidates/" + candidateId;
};
export async function readVideoCandidate(
  projectId: string,
  candidateId: string,
  signal: AbortSignal,
) {
  const row = await api<VideoCandidate>(candidatePath(projectId, candidateId), {
    signal,
  });
  if (
    row.id !== candidateId ||
    row.project_id !== projectId ||
    row.media_kind !== "VIDEO"
  )
    throw new Error("视频候选与当前项目或精确版本不一致。");
  if (row.version && row.version.id !== row.asset_version_id)
    throw new Error("视频资产版本回执不一致。");
  return row;
}
export async function readVideoAsset(
  projectId: string,
  assetId: string,
  signal: AbortSignal,
) {
  if (!isResourceId(assetId)) throw new Error("视频资产标识无效。");
  const row = await api<VideoAsset>(
    "/projects/" + projectId + "/assets/" + assetId,
    { signal },
  );
  if (row.id !== assetId || row.project_id !== projectId)
    throw new Error("视频资产不属于当前项目。");
  return row;
}
export async function loadVideoBlob(
  projectId: string,
  candidate: VideoCandidate,
  signal: AbortSignal,
) {
  const path = candidatePath(projectId, candidate.id) + "/content";
  const video = candidate.media_check?.video;
  if (
    candidate.project_id !== projectId ||
    candidate.content_path !== path ||
    candidate.media_check?.schema_version !== "candidate-video-check/1" ||
    candidate.media_check.status !== "PASSED" ||
    candidate.media_check.candidate_id !== candidate.id ||
    candidate.media_check.input_manifest_id !== candidate.input_manifest_id ||
    !video ||
    !video.full_decode_verified ||
    candidate.version?.checksum !== video.checksum ||
    !Number.isSafeInteger(video.byte_size) ||
    video.byte_size <= 0
  )
    throw new Error("视频缺少与精确版本一致的完整媒体检查。");
  const blob = await apiBlob(path, signal);
  signal.throwIfAborted();
  if (
    blob.type.split(";")[0] !== video.media_type ||
    blob.size !== video.byte_size ||
    (await sha256(await blob.arrayBuffer())) !== video.checksum
  )
    throw new Error("视频完整性检查未通过，已停止播放和下载。");
  signal.throwIfAborted();
  return blob;
}
export async function uploadVideo(
  projectId: string,
  file: File,
  source: string,
  rights: string,
  signal: AbortSignal,
) {
  if (!file.name.toLowerCase().endsWith(".mp4") || file.size < 1)
    throw new Error("请选择非空的 MP4 视频。");
  const checksum = await sha256(await file.arrayBuffer());
  const path = "/projects/" + projectId + "/uploads";
  const ticket = await api<{
    upload_id: string;
    url: string;
    method: string;
    body: string;
    headers: Record<string, string>;
  }>(path, {
    ...json("POST", {
      purpose: "VIDEO",
      original_name: file.name,
      media_type: "video/mp4",
      byte_size: file.size,
      checksum,
      expires_in: 300,
    }),
    signal,
  });
  const url = new URL(ticket.url, window.location.origin);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.hash ||
    url.search ||
    !/^\/file-transfers\/[^/]+$/.test(url.pathname) ||
    ticket.method !== "POST" ||
    ticket.body !== "raw" ||
    ticket.headers?.["Content-Type"] !== "video/mp4"
  )
    throw new Error("视频上传授权无效，请重新申请。");
  const uploaded = await fetch(url, {
    method: "POST",
    body: file,
    headers: { "Content-Type": "video/mp4" },
    signal,
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
  });
  if (!uploaded.ok) throw new Error("视频上传失败，请重试。");
  const saved = await api<{
    id: string;
    project_id: string;
    checksum: string;
    byte_size: number;
    media_type: string;
    rights_confirmed: boolean;
  }>(path + "/" + ticket.upload_id + "/complete", {
    ...json("POST", {
      source_note: source.trim(),
      rights_note: rights.trim(),
      rights_confirmed: true,
    }),
    signal,
  });
  if (
    saved.project_id !== projectId ||
    saved.checksum !== checksum ||
    saved.byte_size !== file.size ||
    saved.media_type !== "video/mp4" ||
    !saved.rights_confirmed
  )
    throw new Error("视频上传回执与文件不一致，请刷新后核对。");
  return saved;
}
