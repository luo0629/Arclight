import "client-only";

import { api, json } from "@/lib/api/client";
import { sha256 } from "@/features/audio/api";

export type SubtitleFont = {
  next_offset?: number;
  id: string;
  project_id: string;
  original_name: string;
  media_type: string;
  rights_confirmed: boolean;
  source_note: string;
  rights_note: string;
};
export async function listFonts(
  projectId: string,
  offset: number | undefined,
  signal: AbortSignal,
) {
  const files = await api<SubtitleFont[]>(
    `/projects/${encodeURIComponent(projectId)}/files?limit=30&offset=${offset ?? 0}`,
    { signal },
  );
  if (files.some((file) => file.project_id !== projectId))
    throw new Error("字体清单与当前项目不一致。");
  return files.map((file, index) => ({
    ...file,
    next_offset: (offset ?? 0) + index + 1,
  }));
}

export async function uploadFont(
  projectId: string,
  file: File,
  source: string,
  rights: string,
  signal: AbortSignal,
) {
  const extension = file.name.split(".").at(-1)?.toLowerCase();
  const mediaType =
    extension === "ttf" ? "font/ttf" : extension === "otf" ? "font/otf" : "";
  if (!mediaType || file.size < 1 || file.size > 8 * 1024 * 1024)
    throw new Error("请选择 8 MiB 以内的非空 TTF 或 OTF 字体。");
  const path = `/projects/${encodeURIComponent(projectId)}/uploads`;
  const ticket = await api<{
    upload_id: string;
    url: string;
    method: string;
    body: string;
    headers: Record<string, string>;
  }>(path, {
    ...json("POST", {
      purpose: "FONT",
      original_name: file.name,
      media_type: mediaType,
      byte_size: file.size,
      checksum: await sha256(await file.arrayBuffer()),
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
    ticket.headers?.["Content-Type"] !== mediaType
  )
    throw new Error("字体上传地址无效，请重新申请。");
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": mediaType },
    body: file,
    signal,
    credentials: "omit",
    redirect: "error",
    cache: "no-store",
  });
  if (!response.ok)
    throw new Error(`字体上传失败（${response.status}），请重试。`);
  const saved = await api<SubtitleFont>(
    `${path}/${encodeURIComponent(ticket.upload_id)}/complete`,
    {
      ...json("POST", {
        source_note: source.trim(),
        rights_note: rights.trim(),
        rights_confirmed: true,
      }),
      signal,
    },
  );
  if (
    saved.project_id !== projectId ||
    saved.media_type !== mediaType ||
    !saved.rights_confirmed
  )
    throw new Error("字体文件回执不一致，请刷新后核对。");
  return saved;
}
