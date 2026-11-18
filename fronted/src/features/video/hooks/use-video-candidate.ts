"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, message } from "@/lib/api/client";
import { readVideoAsset, readVideoCandidate } from "../api";
import type { VideoAsset, VideoCandidate } from "../types";

export default function useVideoCandidate(
  projectId: string,
  candidateId: string,
  unavailable: (cause: unknown) => void,
) {
  const [data, setData] = useState<VideoCandidate | null>(null);
  const [asset, setAsset] = useState<VideoAsset | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [fresh, setFresh] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const live = useRef(false);
  const read = useRef<AbortController | null>(null);
  const write = useRef<AbortController | null>(null);
  const operating = useRef(false);
  const sequence = useRef(0);
  const fail = useCallback(
    (cause: unknown) => {
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        setData(null);
        setAsset(null);
        unavailable(cause);
      }
      setError(message(cause));
    },
    [unavailable],
  );
  const load = useCallback(async () => {
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    const current = ++sequence.current;
    setLoading(true);
    setFresh(false);
    setError("");
    try {
      const row = await readVideoCandidate(
        projectId,
        candidateId,
        controller.signal,
      );
      const nextAsset = row.version
        ? await readVideoAsset(
            projectId,
            row.version.asset_id,
            controller.signal,
          )
        : null;
      if (
        !live.current ||
        controller.signal.aborted ||
        sequence.current !== current
      )
        return false;
      setData(row);
      setAsset(nextAsset);
      setFresh(true);
      return true;
    } catch (cause) {
      if (
        live.current &&
        !controller.signal.aborted &&
        sequence.current === current
      )
        fail(cause);
      return false;
    } finally {
      if (
        live.current &&
        !controller.signal.aborted &&
        sequence.current === current
      )
        setLoading(false);
    }
  }, [projectId, candidateId, fail]);
  useEffect(() => {
    live.current = true;
    setData(null);
    setAsset(null);
    void load();
    return () => {
      live.current = false;
      sequence.current++;
      read.current?.abort();
      write.current?.abort();
    };
  }, [load]);
  async function perform<T>(
    command: (signal: AbortSignal) => Promise<T>,
    success: string,
  ): Promise<T | null> {
    if (!fresh || loading || operating.current) return null;
    operating.current = true;
    setBusy(true);
    setError("");
    setNotice("");
    read.current?.abort();
    sequence.current++;
    const controller = new AbortController();
    write.current = controller;
    try {
      const result = await command(controller.signal);
      if (!live.current || controller.signal.aborted) return null;
      setNotice(success);
      await load();
      return result;
    } catch (cause) {
      if (live.current && !controller.signal.aborted) {
        setFresh(false);
        fail(cause);
      }
      return null;
    } finally {
      operating.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }
  return { data, asset, loading, busy, fresh, error, notice, load, perform };
}
