"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { message } from "@/lib/api/client";

export default function useRenderDetail<T>(
  scope: string,
  readValue: (signal: AbortSignal) => Promise<T>,
  unavailable: (cause: unknown) => void,
  autoload = true,
) {
  const [data, setValue] = useState<T | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const pending = useRef<AbortController | null>(null);
  const refreshQueued = useRef(false);
  const live = useRef(false);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const request = useCallback(
    async function readDetail(keepData: boolean) {
      pending.current?.abort();
      const controller = new AbortController();
      pending.current = controller;
      setLoading(true);
      if (!keepData) {
        setError("");
        setValue(null);
      }
      try {
        const next = await readValue(controller.signal);
        if (
          controller.signal.aborted ||
          !live.current ||
          currentScope.current !== scope
        )
          return;
        setError("");
        setValue(next);
        return next;
      } catch (cause) {
        if (
          controller.signal.aborted ||
          !live.current ||
          currentScope.current !== scope
        )
          return;
        setValue(null);
        setError(message(cause));
        unavailable(cause);
      } finally {
        if (pending.current === controller) {
          pending.current = null;
          const refreshAgain = refreshQueued.current;
          refreshQueued.current = false;
          if (live.current && currentScope.current === scope) {
            if (refreshAgain) void readDetail(true);
            else setLoading(false);
          }
        }
      }
    },
    [scope, readValue, unavailable],
  );
  const load = useCallback(() => {
    refreshQueued.current = false;
    return request(false);
  }, [request]);
  const refresh = useCallback(() => {
    if (pending.current && !pending.current.signal.aborted) {
      // A newer invalidation may postdate the read already in flight.
      refreshQueued.current = true;
      return;
    }
    void request(true);
  }, [request]);
  useEffect(() => {
    live.current = true;
    setValue(null);
    setError("");
    setLoading(false);
    if (autoload) void load();
    return () => {
      live.current = false;
      refreshQueued.current = false;
      pending.current?.abort();
    };
  }, [load, autoload]);
  const setData = useCallback((next: T | null) => {
    pending.current?.abort();
    pending.current = null;
    refreshQueued.current = false;
    setLoading(false);
    setError("");
    setValue(next);
  }, []);
  return { data, setData, loading, error, load, refresh };
}
