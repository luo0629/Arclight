"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { loadVideoBlob } from "../api";
import type { VideoCandidate } from "../types";

export default function VideoPreview({
  projectId,
  candidate,
  unavailable,
}: {
  projectId: string;
  candidate: VideoCandidate;
  unavailable: (cause: unknown) => void;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(false);
  const pending = useRef<AbortController | null>(null);
  const preview = useRef("");
  const downloads = useRef(new Map<string, number>());
  useEffect(() => {
    live.current = true;
    setUrl("");
    setBusy(false);
    setError("");
    return () => {
      live.current = false;
      pending.current?.abort();
      pending.current = null;
      if (preview.current) URL.revokeObjectURL(preview.current);
      preview.current = "";
      downloads.current.forEach((timer, value) => {
        window.clearTimeout(timer);
        URL.revokeObjectURL(value);
      });
      downloads.current.clear();
    };
  }, [
    projectId,
    candidate.id,
    candidate.version?.id,
    candidate.version?.checksum,
  ]);
  async function read(download: boolean) {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    try {
      const blob = await loadVideoBlob(projectId, candidate, controller.signal);
      if (!live.current || controller.signal.aborted) return;
      const next = URL.createObjectURL(blob);
      if (download) {
        const anchor = document.createElement("a");
        anchor.href = next;
        anchor.download =
          "shot-video-v" + candidate.version?.version_no + ".mp4";
        anchor.click();
        const timer = window.setTimeout(() => {
          URL.revokeObjectURL(next);
          downloads.current.delete(next);
        }, 1000);
        downloads.current.set(next, timer);
      } else {
        if (preview.current) URL.revokeObjectURL(preview.current);
        preview.current = next;
        setUrl(next);
      }
    } catch (cause) {
      if (!live.current || controller.signal.aborted) return;
      if (preview.current) URL.revokeObjectURL(preview.current);
      preview.current = "";
      setUrl("");
      setError(message(cause));
      unavailable(cause);
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        if (live.current) setBusy(false);
      }
    }
  }
  return (
    <section
      aria-label="视频候选预览与下载"
      className="flex min-w-0 flex-col gap-3"
    >
      {url && (
        <video
          src={url}
          controls
          playsInline
          preload="metadata"
          aria-label={"视频候选 v" + candidate.version?.version_no + " 预览"}
          className="max-h-80 w-full rounded-lg bg-black"
          onError={() =>
            setError("浏览器无法播放此视频，请核对编码或下载检查。")
          }
        />
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={busy || !candidate.content_path}
          onClick={() => void read(false)}
        >
          {busy ? "正在读取视频…" : "读取视频候选预览"}
        </Button>
        <Button
          variant="outline"
          disabled={busy || !candidate.content_path}
          onClick={() => void read(true)}
        >
          下载候选 MP4
        </Button>
      </div>
      <p className="text-xs text-muted-foreground">
        每次读取均重新核验权限、精确文件及
        SHA-256。媒体检查通过仍须视频时序审查。
      </p>
      {error && (
        <Notice role="alert" tone="error">
          {error}
        </Notice>
      )}
    </section>
  );
}
