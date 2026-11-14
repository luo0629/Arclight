"use client";

import { useCallback, useState } from "react";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Notice } from "@/components/ui-kit";
import { listFonts, uploadFont, type SubtitleFont } from "../font-api";
import useRenderList from "../hooks/use-render-list";
import useRenderMutation from "../hooks/use-render-mutation";
import { selectClass } from "./render-format";

const cursor = (file: SubtitleFont) => file.next_offset ?? 0;
export default function SubtitleFontPicker({
  projectId,
  value,
  onChange,
  disabled,
  unavailable,
}: {
  projectId: string;
  value: string;
  onChange: (id: string) => void;
  disabled: boolean;
  unavailable: (cause: unknown) => void;
}) {
  const fetchPage = useCallback(
    (offset: number | undefined, signal: AbortSignal) =>
      listFonts(projectId, offset, signal),
    [projectId],
  );
  const files = useRenderList<SubtitleFont, number>(
    projectId,
    fetchPage,
    cursor,
    unavailable,
  );
  const fonts = files.rows.filter(
    (file) =>
      ["font/ttf", "font/otf"].includes(file.media_type) &&
      file.rights_confirmed,
  );
  const [file, setFile] = useState<File | null>(null);
  const [source, setSource] = useState("");
  const [rights, setRights] = useState("");
  const [confirmed, confirm] = useState(false);
  const mutation = useRenderMutation(
    `font-upload:${projectId}`,
    !disabled,
    unavailable,
  );
  async function upload() {
    if (!file || !source.trim() || !rights.trim() || !confirmed) return;
    await mutation.mutate(
      (signal) => uploadFont(projectId, file, source, rights, signal),
      (saved) => {
        files.prepend(saved);
        onChange(saved.id);
        setFile(null);
        confirm(false);
      },
      "授权字体已上传并选用。",
    );
  }
  return (
    <div className="flex min-w-0 flex-col gap-3">
      <label className="flex flex-col gap-1 text-sm">
        字幕字体
        <select
          aria-label="字幕字体"
          className={selectClass}
          value={value}
          disabled={disabled || mutation.busy || files.loading}
          onChange={(e) => onChange(e.target.value)}
        >
          <option value="">请选择已授权字体</option>
          {fonts.map((font) => (
            <option key={font.id} value={font.id}>
              {font.original_name}
            </option>
          ))}
        </select>
      </label>
      {files.loading && <p role="status">正在读取项目字体…</p>}
      {files.error && (
        <Notice tone="error">
          {files.error}
          <Button variant="outline" onClick={() => void files.load()}>
            重新读取字体
          </Button>
        </Notice>
      )}
      {files.hasMore && (
        <Button
          variant="outline"
          disabled={files.loading}
          onClick={() => void files.load(true)}
        >
          加载更多项目文件查找字体
        </Button>
      )}
      <details>
        <summary>上传授权字体</summary>
        <fieldset
          disabled={disabled || mutation.busy}
          className="mt-3 flex min-w-0 flex-col gap-3"
        >
          <label className="text-sm">
            TTF / OTF 字体（最多 8 MiB）
            <Input
              aria-label="字体文件"
              type="file"
              accept=".ttf,.otf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </label>
          <label className="text-sm">
            字体来源
            <Input
              aria-label="字体来源"
              value={source}
              maxLength={2000}
              onChange={(e) => setSource(e.target.value)}
            />
          </label>
          <label className="text-sm">
            字体授权说明
            <Input
              aria-label="字体授权说明"
              value={rights}
              maxLength={2000}
              onChange={(e) => setRights(e.target.value)}
            />
          </label>
          <label className="flex items-start gap-2 text-sm">
            <input
              type="checkbox"
              checked={confirmed}
              onChange={(e) => confirm(e.target.checked)}
            />
            我确认该字体可用于本作品字幕与成片
          </label>
          <Button
            className="self-start"
            disabled={!file || !source.trim() || !rights.trim() || !confirmed}
            onClick={() => void upload()}
          >
            上传并选用字幕字体
          </Button>
        </fieldset>
      </details>
      {mutation.error && <Notice tone="error">{mutation.error}</Notice>}
      {mutation.notice && <Notice tone="success">{mutation.notice}</Notice>}
    </div>
  );
}
