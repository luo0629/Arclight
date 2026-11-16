"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, message } from "@/lib/api/client";
import { useProjectEvents } from "@/features/projects/project-events";
import type { ProducerProgress } from "../producer-types";

export default function useProducerProgress({
  projectId,
  episodeId,
  runId,
  revision,
  status,
}: {
  projectId: string;
  episodeId: string;
  runId: string;
  revision: number;
  status: string;
}) {
  const scope = `${projectId}:${episodeId}:${runId}`;
  const [data, setData] = useState<ProducerProgress | null>(null);
  const [loading, setLoading] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(false);
  const currentScope = useRef(scope);
  const epoch = useRef(0);
  const read = useRef<AbortController | null>(null);
  const latest = useRef<ProducerProgress | null>(null);
  currentScope.current = scope;

  const refresh = useCallback(async () => {
    if (!live.current || currentScope.current !== scope) return false;
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    const sequence = ++epoch.current;
    const isCurrent = () =>
      live.current &&
      currentScope.current === scope &&
      epoch.current === sequence &&
      !controller.signal.aborted;
    setLoading(true);
    setFresh(false);
    setError("");
    try {
      const result = await api<ProducerProgress>(`/runs/${runId}/producer`, {
        signal: controller.signal,
        cache: "no-store",
      });
      if (!isCurrent()) return false;
      if (
        result.run_id !== runId ||
        result.episode_id !== episodeId ||
        result.project_id !== projectId
      )
        throw new Error("生产记录不属于当前项目或剧集。");
      if (latest.current && result.revision < latest.current.revision)
        throw new Error("收到较旧的生产状态，请重新刷新。");
      latest.current = result;
      setData(result);
      setFresh(true);
      return true;
    } catch (cause) {
      if (isCurrent()) setError(message(cause));
      return false;
    } finally {
      if (isCurrent()) setLoading(false);
    }
  }, [scope, runId, episodeId, projectId]);

  useEffect(() => {
    live.current = true;
    latest.current = null;
    setData(null);
    setFresh(false);
    void refresh();
    return () => {
      live.current = false;
      epoch.current++;
      read.current?.abort();
    };
  }, [scope, refresh]);

  useEffect(() => {
    void refresh();
  }, [revision, status, refresh]);

  useProjectEvents(() => {
    void refresh();
  });

  const active = data?.status ?? status;
  useEffect(() => {
    if (!["QUEUED", "RUNNING", "WAITING_REVIEW"].includes(active)) return;
    const timer = setInterval(() => void refresh(), 3000);
    return () => clearInterval(timer);
  }, [active, refresh]);

  return { data, loading, fresh, error, refresh };
}
