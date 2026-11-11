"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { message } from "@/lib/api/client";
import { loadDeliveryBlob } from "../api";
import type { RenderDelivery } from "../types";
import { bytes } from "./render-format";

export default function DeliveryPreview({
  projectId,
  episodeId,
  delivery,
  onUnavailable,
}: {
  projectId: string;
  episodeId: string;
  delivery: RenderDelivery;
  onUnavailable: (cause: unknown) => void;
}) {
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const live = useRef(false);
  const pending = useRef<AbortController | null>(null);
  const preview = useRef("");
  const downloads = useRef(new Map<string, number>());
  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      pending.current?.abort();
      if (preview.current) URL.revokeObjectURL(preview.current);
      downloads.current.forEach((timer, download) => {
        window.clearTimeout(timer);
        URL.revokeObjectURL(download);
      });
      downloads.current.clear();
    };
  }, []);
  async function read(mode: "preview" | "download") {
    if (pending.current) return;
    const controller = new AbortController();
    pending.current = controller;
    setBusy(mode);
    setError("");
    try {
      const blob = await loadDeliveryBlob(
        projectId,
        episodeId,
        delivery,
        controller.signal,
      );
      if (controller.signal.aborted || !live.current) return;
      const next = URL.createObjectURL(blob);
      if (mode === "preview") {
        if (preview.current) URL.revokeObjectURL(preview.current);
        preview.current = next;
        setUrl(next);
      } else {
        const anchor = document.createElement("a");
        anchor.href = next;
        anchor.download = `episode-${episodeId}-v${delivery.version_no}-${delivery.profile.toLowerCase()}.mp4`;
        anchor.click();
        const timer = window.setTimeout(() => {
          URL.revokeObjectURL(next);
          downloads.current.delete(next);
        }, 1000);
        downloads.current.set(next, timer);
      }
    } catch (cause) {
      if (controller.signal.aborted || !live.current) return;
      if (preview.current) URL.revokeObjectURL(preview.current);
      preview.current = "";
      setUrl("");
      setError(message(cause));
      onUnavailable(cause);
    } finally {
      if (pending.current === controller) {
        pending.current = null;
        if (live.current) setBusy("");
      }
    }
  }
  return (
    <section
      aria-label="成片预览与下载"
      className="flex min-w-0 flex-col gap-3"
    >
      {url ? (
        <video
          src={url}
          controls
          playsInline
          preload="metadata"
          aria-label={`成片版本 ${delivery.version_no} 预览`}
          className="max-h-[32rem] w-full rounded-xl bg-black"
          onError={() =>
            setError(
              "浏览器无法播放此成片。请刷新报告并核对编码，或下载后检查。",
            )
          }
        />
      ) : (
        <div className="flex min-h-48 flex-col items-center justify-center gap-2 rounded-xl border border-dashed bg-muted/40 px-5 py-8 text-center">
          <h4>成片预览</h4>
          <p className="max-w-sm text-sm text-muted-foreground">
            按需读取 {bytes(delivery.byte_size)}
            ，每次读取和下载都会重新核验当前权限与文件。
          </p>
        </div>
      )}
      <div className="flex flex-wrap gap-2">
        <Button
          variant="outline"
          disabled={!!busy}
          onClick={() => void read("preview")}
        >
          {busy === "preview" ? "读取预览中…" : "读取成片预览"}
        </Button>
        <Button
          variant="outline"
          disabled={!!busy}
          onClick={() => void read("download")}
        >
          {busy === "download" ? "准备下载中…" : "下载 MP4"}
        </Button>
      </div>
      {error && <Notice tone="error">{error}</Notice>}
    </section>
  );
}
