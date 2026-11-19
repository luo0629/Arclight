import "client-only";

import { api, apiBlob, ApiError, json } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import { sha256 } from "@/features/audio/api";
import type {
  LipSyncOptions,
  LipSyncRequest,
  VideoCandidate,
  VideoAsset,
} from "./types";

export const candidatePath = (projectId: string, candidateId: string) => {
  if (![projectId, candidateId].every(isResourceId))
    throw new Error("视频候选标识无效。");
  return "/projects/" + projectId + "/generation-candidates/" + candidateId;
};
export async function readVideoCandidate(
  projectId: string,
  candidateId: string,
  signal?: AbortSignal,
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
export async function readLipSyncOptions(
  projectId: string,
  candidateId: string,
  signal: AbortSignal,
) {
  const row = await api<LipSyncOptions>(
    candidatePath(projectId, candidateId) + "/lip-sync",
    { signal },
  );
  if (row.candidate_id !== candidateId)
    throw new Error("台词音频选项不属于当前视频候选。");
  return row;
}
export async function saveLipSync(
  projectId: string,
  source: VideoCandidate,
  payload: LipSyncRequest,
  signal: AbortSignal,
) {
  const row = await api<VideoCandidate>(
    candidatePath(projectId, source.id) + "/lip-sync",
    {
      ...json("POST", payload),
      signal,
    },
  );
  if (
    row.project_id !== projectId ||
    row.id === source.id ||
    row.replaces_candidate_id !== source.id ||
    row.lip_sync?.source_video_version_id !== source.asset_version_id ||
    row.lip_sync?.source_video_sha256 !== source.version?.checksum ||
    row.lip_sync?.output_sha256 !== row.version?.checksum
  )
    throw new Error("校正结果与原精确候选不匹配，请刷新候选历史核对。");
  return row;
}
export async function readVideoAsset(
  projectId: string,
  assetId: string,
  signal?: AbortSignal,
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
type VideoUploadTicket = {
  upload_id: string;
  url: string;
  method: string;
  body: string;
  headers: Record<string, string>;
};
type UploadedVideo = {
  id: string;
  project_id: string;
  checksum: string;
  byte_size: number;
  media_type: string;
  original_name: string;
  source_note: string;
  rights_note: string;
  rights_confirmed: boolean;
};

export async function uploadVideo(
  projectId: string,
  file: File,
  source: string,
  rights: string,
  signal: AbortSignal,
) {
  if (!file.name.toLowerCase().endsWith(".mp4") || file.size < 1)
    throw new Error("请选择非空的 MP4 视频。");
  const bytes = await file.arrayBuffer();
  signal.throwIfAborted();
  const checksum = await sha256(bytes);
  signal.throwIfAborted();
  const path = "/projects/" + projectId + "/uploads";
  const upload = {
    purpose: "VIDEO",
    original_name: file.name,
    media_type: "video/mp4",
    byte_size: file.size,
    checksum,
    expires_in: 300,
  };
  const declaration = {
    source_note: source.trim(),
    rights_note: rights.trim(),
    rights_confirmed: true,
  };
  let ticket: VideoUploadTicket | null = null;
  let useLocalUpload = false;
  try {
    ticket = await api<VideoUploadTicket>(path, {
      ...json("POST", upload),
      signal,
    });
  } catch (error) {
    if (
      !(error instanceof ApiError) ||
      error.status !== 503 ||
      error.code !== "PRESIGNED_UNAVAILABLE"
    )
      throw error;
    useLocalUpload = true;
  }
  let saved: UploadedVideo;
  if (!useLocalUpload) {
    if (!ticket) throw new Error("视频上传授权无效，请重新申请。");
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
    saved = await api<UploadedVideo>(
      path + "/" + ticket.upload_id + "/complete",
      {
        ...json("POST", declaration),
        signal,
      },
    );
  } else {
    signal.throwIfAborted();
    const content = new Uint8Array(bytes);
    let binary = "";
    for (let offset = 0; offset < content.length; offset += 8192)
      binary += String.fromCharCode(...content.subarray(offset, offset + 8192));
    saved = await api<UploadedVideo>(path + "/local", {
      ...json("POST", {
        ...upload,
        ...declaration,
        content_base64: btoa(binary),
      }),
      signal,
    });
  }
  signal.throwIfAborted();
  if (
    !isResourceId(saved.id) ||
    saved.project_id !== projectId ||
    saved.checksum !== checksum ||
    saved.byte_size !== file.size ||
    saved.media_type !== "video/mp4" ||
    saved.original_name !== file.name ||
    saved.source_note !== declaration.source_note ||
    saved.rights_note !== declaration.rights_note ||
    saved.rights_confirmed !== true
  )
    throw new Error("视频上传回执与文件不一致，请刷新后核对。");
  return saved;
}
