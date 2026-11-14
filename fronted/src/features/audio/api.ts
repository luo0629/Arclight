import "client-only";

import { api, apiBlob, json } from "@/lib/api/client";
import type { AssetVersion } from "@/types/api";
import type {
  AudioAsset,
  AudioFile,
  AudioHistory,
  AudioImportResult,
  AudioLine,
  AudioResult,
  AudioSnapshot,
  AudioTask,
  AudioVoice,
  RightsDeclaration,
  SpeechConfiguration,
} from "./types";

const id = encodeURIComponent;
const page = (offset = 0) => `?limit=30&offset=${offset}`;
const project = (value: string) => `/projects/${id(value)}`;
const line = (snapshot: string, value: string) =>
  `/audio-snapshots/${id(snapshot)}/lines/${id(value)}`;
const versions = (projectId: string, assetId: string) =>
  `${project(projectId)}/assets/${id(assetId)}/versions`;
const read = <T>(path: string, signal: AbortSignal) => api<T>(path, { signal });
const write = <T>(
  path: string,
  body: unknown,
  signal: AbortSignal,
  method = "POST",
) => api<T>(path, { ...json(method, body), signal });

export const audioApi = {
  snapshots: (
    episodeId: string,
    offset: number | undefined,
    signal: AbortSignal,
  ) =>
    read<AudioSnapshot[]>(
      `/episodes/${id(episodeId)}/audio-snapshots${page(offset)}&newest_first=true`,
      signal,
    ),
  createSnapshot: (
    episodeId: string,
    scriptId: string,
    key: string,
    signal: AbortSignal,
  ) =>
    write<AudioSnapshot>(
      `/episodes/${id(episodeId)}/audio-snapshots`,
      { creation_key: key, script_version_id: scriptId },
      signal,
    ),
  lines: (snapshot: string, offset: number | undefined, signal: AbortSignal) =>
    read<AudioLine[]>(
      `/audio-snapshots/${id(snapshot)}/lines${page(offset)}`,
      signal,
    ),
  voices: (
    projectId: string,
    speaker: string,
    offset: number | undefined,
    signal: AbortSignal,
  ) =>
    read<AudioVoice[]>(
      `${project(projectId)}/audio-voices${page(offset)}&speaker=${id(speaker)}`,
      signal,
    ),
  speech: (projectId: string, signal: AbortSignal) =>
    read<SpeechConfiguration>(
      `${project(projectId)}/generation-jobs/services/speech/configuration`,
      signal,
    ),
  createVoice: (projectId: string, body: unknown, signal: AbortSignal) =>
    write<AudioVoice>(`${project(projectId)}/audio-voices`, body, signal),
  history: (
    snapshot: string,
    lineId: string,
    offset: number | undefined,
    signal: AbortSignal,
  ) =>
    read<AudioHistory[]>(
      `${line(snapshot, lineId)}/history${page(offset)}&newest_first=true`,
      signal,
    ),
  tasks: (
    snapshot: string,
    lineId: string,
    offset: number | undefined,
    signal: AbortSignal,
  ) =>
    read<AudioTask[]>(`${line(snapshot, lineId)}/tts${page(offset)}`, signal),
  async task(
    snapshot: string,
    lineId: string,
    jobId: string,
    signal: AbortSignal,
  ) {
    const row = await read<AudioTask>(
      `${line(snapshot, lineId)}/tts/${id(jobId)}`,
      signal,
    );
    if (
      row.snapshot_id !== snapshot ||
      row.line_id !== lineId ||
      row.job_id !== jobId
    )
      throw new Error("配音任务来源与本句不一致，请重新读取。");
    return row;
  },
  createTask: (
    snapshot: string,
    lineId: string,
    body: unknown,
    regenerate: boolean,
    signal: AbortSignal,
  ) =>
    write<AudioTask>(
      `${line(snapshot, lineId)}/tts${regenerate ? "/regenerate" : ""}`,
      body,
      signal,
    ),
  register: (
    snapshot: string,
    lineId: string,
    jobId: string,
    body: unknown,
    signal: AbortSignal,
  ) =>
    write<AudioResult>(
      `${line(snapshot, lineId)}/tts/${id(jobId)}/result`,
      body,
      signal,
    ),
  import: (
    snapshot: string,
    lineId: string,
    body: unknown,
    signal: AbortSignal,
  ) =>
    write<AudioImportResult>(`${line(snapshot, lineId)}/imports`, body, signal),
  async asset(projectId: string, assetId: string, signal: AbortSignal) {
    const row = await read<AudioAsset>(
      `${project(projectId)}/assets/${id(assetId)}`,
      signal,
    );
    if (
      row.id !== assetId ||
      row.project_id !== projectId ||
      row.asset_type !== "AUDIO"
    )
      throw new Error("该素材不是本项目的台词音频。");
    return row;
  },
  version: (
    projectId: string,
    assetId: string,
    versionId: string,
    signal: AbortSignal,
  ) =>
    read<AssetVersion>(
      `${versions(projectId, assetId)}/${id(versionId)}`,
      signal,
    ),
  changeState: (
    projectId: string,
    assetId: string,
    versionId: string,
    body: unknown,
    signal: AbortSignal,
  ) =>
    write<AssetVersion>(
      `${versions(projectId, assetId)}/${id(versionId)}/state`,
      body,
      signal,
      "PATCH",
    ),
  select: (
    projectId: string,
    assetId: string,
    versionId: string,
    body: unknown,
    signal: AbortSignal,
  ) =>
    write<AudioAsset>(
      `${versions(projectId, assetId)}/${id(versionId)}/select`,
      body,
      signal,
    ),
  lock: (
    projectId: string,
    assetId: string,
    versionId: string,
    body: unknown,
    unlock: boolean,
    signal: AbortSignal,
  ) =>
    write<AudioAsset>(
      `${versions(projectId, assetId)}/${id(versionId)}/${unlock ? "unlock" : "lock"}`,
      body,
      signal,
    ),
  files: (projectId: string, offset: number | undefined, signal: AbortSignal) =>
    read<AudioFile[]>(`${project(projectId)}/files${page(offset)}`, signal),
};

export const audioMediaTypes = [
  "audio/wav",
  "audio/mpeg",
  "audio/ogg",
  "audio/flac",
  "audio/mp4",
];
export async function sha256(data: ArrayBuffer) {
  return Array.from(
    new Uint8Array(await crypto.subtle.digest("SHA-256", data)),
    (value) => value.toString(16).padStart(2, "0"),
  ).join("");
}

export async function loadAudio(
  projectId: string,
  fileId: string,
  signal: AbortSignal,
  expectedSha256?: string,
  expectedVersionId?: string,
) {
  const path = `${project(projectId)}/files/${id(fileId)}`;
  const file = await read<AudioFile>(path, signal);
  if (
    file.id !== fileId ||
    file.project_id !== projectId ||
    (expectedSha256 !== undefined && file.checksum !== expectedSha256) ||
    (expectedVersionId !== undefined &&
      file.asset_version_id !== expectedVersionId) ||
    !audioMediaTypes.includes(file.media_type)
  )
    throw new Error("音频文件来源或格式不匹配。");
  const blob = await apiBlob(`${path}/content`, signal);
  if (
    blob.size !== file.byte_size ||
    blob.type.split(";")[0] !== file.media_type ||
    (await sha256(await blob.arrayBuffer())) !== file.checksum
  )
    throw new Error("音频完整性检查未通过，请重新读取。");
  return blob;
}

export async function uploadRecording(
  projectId: string,
  file: File,
  rights: RightsDeclaration,
  signal: AbortSignal,
) {
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  const mediaType = (
    { wav: "audio/wav", mp3: "audio/mpeg", ogg: "audio/ogg" } as Record<
      string,
      string
    >
  )[extension ?? ""];
  if (!mediaType || !file.size)
    throw new Error("请选择非空的 WAV、MP3 或 OGG 录音。");
  const ticket = await write<{
    upload_id: string;
    url: string;
    method: string;
    body: string;
    headers: Record<string, string>;
  }>(
    `${project(projectId)}/uploads`,
    {
      purpose: "AUDIO",
      original_name: file.name,
      media_type: mediaType,
      byte_size: file.size,
      checksum: await sha256(await file.arrayBuffer()),
      expires_in: 300,
    },
    signal,
  );
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
    ticket.headers?.["Content-Type"] !== mediaType
  )
    throw new Error("录音上传地址无效，请重新申请。");
  const uploaded = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": mediaType },
    body: file,
    signal,
    credentials: "omit",
    redirect: "error",
    cache: "no-store",
  });
  if (!uploaded.ok)
    throw new Error(`录音上传失败（${uploaded.status}），请重试。`);
  return write<AudioFile>(
    `${project(projectId)}/uploads/${id(ticket.upload_id)}/complete`,
    rights,
    signal,
  );
}
