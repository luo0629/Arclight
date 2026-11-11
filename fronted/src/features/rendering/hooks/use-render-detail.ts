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
  const live = useRef(false);
  const currentScope = useRef(scope);
  currentScope.current = scope;
  const load = useCallback(async () => {
    pending.current?.abort();
    const controller = new AbortController();
    pending.current = controller;
    setLoading(true);
    setError("");
    setValue(null);
    try {
      const next = await readValue(controller.signal);
      if (
        controller.signal.aborted ||
        !live.current ||
        currentScope.current !== scope
      )
        return;
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
        if (live.current) setLoading(false);
      }
    }
  }, [scope, readValue, unavailable]);
  useEffect(() => {
    live.current = true;
    setValue(null);
    setError("");
    setLoading(false);
    if (autoload) void load();
    return () => {
      live.current = false;
      pending.current?.abort();
    };
  }, [load, autoload]);
  const setData = useCallback((next: T | null) => {
    pending.current?.abort();
    pending.current = null;
    setLoading(false);
    setError("");
    setValue(next);
  }, []);
  return { data, setData, loading, error, load };
}
