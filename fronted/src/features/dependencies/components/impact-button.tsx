"use client";

import { useEffect, useId, useRef, useState, type ComponentProps } from "react";
import { Button } from "@/components/ui/button";
import Dialog from "@/components/shared/app-dialog";
import { Notice } from "@/components/ui-kit";
import { api, message } from "@/lib/api/client";

export type DependencyImpact = {
  digest: string;
  summary: Record<"REVIEW" | "GENERATE" | "RENDER", number>;
  affected: {
    asset_version_id: string;
    asset_name: string;
    asset_type: string;
    version_no: number;
    source_asset_version_id: string;
    dependency_type: string;
    depth: number;
    action: "REVIEW" | "GENERATE" | "RENDER";
    stale: boolean;
  }[];
};
const actions = {
  REVIEW: "重新审核",
  GENERATE: "重新生成",
  RENDER: "重新渲染",
};

export default function ImpactButton({
  endpoint,
  onConfirm,
  children,
  disabled,
  ...props
}: {
  endpoint?: string;
  onConfirm: (digest?: string) => Promise<unknown>;
} & Omit<ComponentProps<typeof Button>, "onClick">) {
  const title = useId();
  const [preview, setPreview] = useState<DependencyImpact | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const submitting = useRef(false);
  const request = useRef<AbortController | null>(null);
  useEffect(() => {
    request.current = null;
    submitting.current = false;
    setBusy(false);
    setPreview(null);
    setError("");
    return () => request.current?.abort();
  }, [endpoint]);
  async function confirm(digest?: string, controller = request.current) {
    try {
      const result = await onConfirm(digest);
      if (controller?.signal.aborted) return;
      if (result === false) {
        setPreview(null);
        setError("变更未完成，请查看错误并重新分析影响后重试。");
      } else setPreview(null);
    } catch (error) {
      if (!controller?.signal.aborted) {
        setPreview(null);
        setError(message(error));
      }
    }
  }
  async function inspect() {
    if (submitting.current || disabled) return;
    submitting.current = true;
    setBusy(true);
    setError("");
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      if (!endpoint) await confirm(undefined, controller);
      else {
        const result = await api<DependencyImpact>(endpoint, {
          signal: controller.signal,
        });
        if (controller.signal.aborted) return;
        if (result.affected.length) setPreview(result);
        else await confirm(result.digest, controller);
      }
    } catch (error) {
      if (!controller.signal.aborted) setError(message(error));
    } finally {
      if (request.current === controller) {
        submitting.current = false;
        if (!controller.signal.aborted) setBusy(false);
      }
    }
  }
  return (
    <>
      <Button
        {...props}
        disabled={disabled || busy}
        onClick={() => void inspect()}
      >
        {busy ? "正在分析或提交…" : children}
      </Button>
      {error && (
        <Notice tone="error">
          {error}
          <Button
            variant="outline"
            disabled={busy || disabled}
            onClick={() => void inspect()}
          >
            重试影响分析
          </Button>
        </Notice>
      )}
      {preview && (
        <Dialog
          labelledBy={title}
          busy={busy}
          onDismiss={() => setPreview(null)}
        >
          <h2 id={title} className="text-lg font-semibold">
            确认变更影响
          </h2>
          <p>
            以下精确版本及其下游将失效。历史内容与来源快照保留，确认后需按清单处理。
          </p>
          <p>
            重新审核 {preview.summary.REVIEW} · 重新生成{" "}
            {preview.summary.GENERATE} · 重新渲染 {preview.summary.RENDER}
          </p>
          <ul className="flex flex-col gap-3" aria-label="受影响的下游版本">
            {preview.affected.map((item) => (
              <li
                key={item.asset_version_id}
                className="min-w-0 break-all rounded-lg border p-3 text-sm"
              >
                <strong>
                  {item.asset_name} · v{item.version_no} ·{" "}
                  {actions[item.action]}
                </strong>
                <p>
                  {item.asset_type} · {item.asset_version_id}
                  {item.stale ? " · 已失效" : ""}
                </p>
                <p>
                  来源 {item.source_asset_version_id} · {item.dependency_type} ·
                  第 {item.depth} 层
                </p>
              </li>
            ))}
          </ul>
          <p className="text-sm text-muted-foreground">
            这里只确认影响范围，不会自动调用生成服务。依赖变化后须重新分析。
          </p>
          <div className="flex flex-wrap gap-2">
            <Button
              variant="outline"
              disabled={busy}
              onClick={() => setPreview(null)}
            >
              取消变更
            </Button>
            <Button
              disabled={busy || disabled}
              onClick={() => {
                if (submitting.current) return;
                submitting.current = true;
                setBusy(true);
                const controller = request.current;
                void confirm(preview.digest, controller).finally(() => {
                  if (request.current === controller) {
                    submitting.current = false;
                    if (!controller?.signal.aborted) setBusy(false);
                  }
                });
              }}
            >
              确认影响并继续
            </Button>
          </div>
        </Dialog>
      )}
    </>
  );
}
