"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, json, message } from "@/lib/api/client";
import type { ContinuityComparison } from "../types";

export default function useContinuityComparison(
  endpoint: string,
  onChanged: () => Promise<void>,
) {
  const [data, setData] = useState<ContinuityComparison | null>(null);
  const current = useRef<ContinuityComparison | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [imageError, setImageError] = useState("");
  const read = useRef<AbortController | null>(null);
  const write = useRef<AbortController | null>(null);
  const live = useRef(false);
  const submitting = useRef(false);
  const update = useCallback((next: ContinuityComparison | null) => {
    current.current = next;
    setData(next);
  }, []);
  const unavailable = useCallback(
    (cause: unknown) => {
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        read.current?.abort();
        update(null);
        setLoading(false);
        setError(message(cause));
      } else {
        setImageError(
          "原图读取未完成，版本操作已暂停。请刷新后重新核对：" + message(cause),
        );
      }
    },
    [update],
  );
  const load = useCallback(
    async (mode: "reset" | "reviews" | "humans" = "reset") => {
      read.current?.abort();
      const controller = new AbortController();
      read.current = controller;
      setLoading(true);
      setError("");
      const previous = current.current;
      if (mode === "reset") {
        update(null);
        setImageError("");
      }
      const offset = mode === "reviews" ? (previous?.next_offset ?? 0) : 0;
      const humanOffset =
        mode === "humans" ? (previous?.next_human_offset ?? 0) : 0;
      try {
        let next = await api<ContinuityComparison>(
          `${endpoint}?limit=20&offset=${offset}&human_offset=${humanOffset}`,
          { signal: controller.signal },
        );
        if (controller.signal.aborted || !live.current) return false;
        if (
          mode !== "reset" &&
          previous &&
          next.latest_review_id !== previous.latest_review_id
        ) {
          next = await api<ContinuityComparison>(endpoint, {
            signal: controller.signal,
          });
          if (controller.signal.aborted || !live.current) return false;
          setNotice("已出现新一轮审查，已刷新为最新记录，请重新核对后操作。");
        } else if (mode === "reviews" && previous) {
          next = {
            ...next,
            reviews: [...previous.reviews, ...next.reviews],
            human_reviews: previous.human_reviews,
            next_human_offset: previous.next_human_offset,
          };
        } else if (mode === "humans" && previous) {
          next = {
            ...next,
            reviews: previous.reviews,
            next_offset: previous.next_offset,
            human_reviews: [...previous.human_reviews, ...next.human_reviews],
          };
        }
        update(next);
        return true;
      } catch (cause) {
        if (!controller.signal.aborted && live.current) {
          // A failed refresh cannot leave stale permissions or actionable records on screen.
          update(null);
          setError(message(cause));
        }
        return false;
      } finally {
        if (read.current === controller && live.current) setLoading(false);
      }
    },
    [endpoint, update],
  );
  useEffect(() => {
    live.current = true;
    void load();
    const refresh = () => {
      if (!submitting.current) void load();
    };
    window.addEventListener("focus", refresh);
    return () => {
      live.current = false;
      read.current?.abort();
      write.current?.abort();
      window.removeEventListener("focus", refresh);
    };
  }, [load]);
  async function mutate(
    path: string,
    method: "POST" | "PATCH",
    payload: unknown,
    success: string,
  ) {
    if (!current.current || submitting.current || loading) return false;
    submitting.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    read.current?.abort();
    const controller = new AbortController();
    write.current = controller;
    try {
      await api(path, { ...json(method, payload), signal: controller.signal });
      if (controller.signal.aborted || !live.current) return false;
      const refreshed = await load();
      if (refreshed && live.current) {
        setNotice(success);
        await onChanged();
      }
      return refreshed;
    } catch (cause) {
      if (controller.signal.aborted || !live.current) return false;
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status))
        unavailable(cause);
      else {
        await load();
        if (live.current) setError(message(cause));
      }
      return false;
    } finally {
      submitting.current = false;
      if (live.current) setBusy(false);
    }
  }
  return {
    data,
    loading,
    busy,
    error,
    imageError,
    notice,
    load,
    mutate,
    unavailable,
  };
}
