"use client";

import { useEffect, useState } from "react";
import { Button } from "@/components/ui/button";
import { Notice } from "@/components/ui-kit";
import { apiBlob, message } from "@/lib/api/client";

export default function VersionImage({
  path,
  compact = false,
  alt = "精确资产版本原图",
  onUnavailable,
}: {
  path: string;
  compact?: boolean;
  alt?: string;
  onUnavailable?: (error: unknown) => void;
}) {
  const [url, setUrl] = useState("");
  const [error, setError] = useState("");
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl = "";
    setUrl("");
    setError("");
    void apiBlob(path, controller.signal)
      .then((blob) => {
        if (!controller.signal.aborted) {
          objectUrl = URL.createObjectURL(blob);
          setUrl(objectUrl);
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted) {
          setError(message(error));
          onUnavailable?.(error);
        }
      });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [path, attempt, onUnavailable]);
  return (
    <div>
      {url && (
        <img
          src={url}
          alt={alt}
          className={
            compact
              ? "h-24 w-full rounded-lg object-contain"
              : "max-h-80 max-w-full rounded-lg object-contain"
          }
        />
      )}
      {error && (
        <Notice role="alert" tone="error">
          {error}
          <Button
            variant="outline"
            onClick={() => setAttempt((value) => value + 1)}
          >
            重试读取图片
          </Button>
        </Notice>
      )}
    </div>
  );
}
