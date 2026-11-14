"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { loadAudio } from "../api";

export default function AudioPlayer({
  projectId,
  fileId,
  unavailable,
  expectedSha256,
  expectedVersionId,
  labelPrefix = "本句",
}: {
  projectId: string;
  fileId: string;
  unavailable: (cause: unknown) => void;
  expectedSha256?: string;
  expectedVersionId?: string;
  labelPrefix?: string;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  const preview = useRef("");
  useEffect(
    () => () => {
      pending.current?.abort();
      if (preview.current) URL.revokeObjectURL(preview.current);
    },
    [],
  );
  async function load() {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(true);
    setError("");
    if (preview.current) URL.revokeObjectURL(preview.current);
    preview.current = "";
    setUrl("");
    try {
      const blob = await loadAudio(
        projectId,
        fileId,
        controller.signal,
        expectedSha256,
        expectedVersionId,
      );
      if (controller.signal.aborted) return;
      preview.current = URL.createObjectURL(blob);
      setUrl(preview.current);
    } catch (cause) {
      if (!controller.signal.aborted) {
        setError(message(cause));
        unavailable(cause);
      }
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        if (!controller.signal.aborted) setBusy(false);
      }
    }
  }
  return (
    <section
      aria-label={`${labelPrefix}音频试听`}
      className="flex min-w-0 flex-col gap-3"
    >
      <Button variant="outline" disabled={busy} onClick={() => void load()}>
        {busy
          ? "正在读取音频…"
          : url
            ? `重新读取${labelPrefix}音频`
            : `读取${labelPrefix}试听`}
      </Button>
      {error && <Notice tone="error">{error}</Notice>}
      {url && (
        <audio
          src={url}
          controls
          preload="metadata"
          aria-label={`${labelPrefix}音频播放器`}
          className="w-full min-w-0"
          onError={() => {
            if (preview.current) URL.revokeObjectURL(preview.current);
            preview.current = "";
            setUrl("");
            setError(`浏览器无法解码${labelPrefix}音频，请检查格式后重试。`);
          }}
        />
      )}
    </section>
  );
}
