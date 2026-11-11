"use client";

import { useEffect, useRef, useState } from "react";
import { ApiError, message } from "@/lib/api/client";

const pendingKeys = new Map<string, string>();
export function pendingCreationKey(scope: string) {
  const storage = "arclight:render-creation:" + scope;
  let key: string | null = pendingKeys.get(scope) ?? null;
  try {
    key = key ?? sessionStorage.getItem(storage);
  } catch {
    /* The in-memory key still protects this request. */
  }
  if (!key) {
    key = crypto.randomUUID();
    try {
      sessionStorage.setItem(storage, key);
    } catch {
      /* Storage can be disabled. */
    }
  }
  pendingKeys.set(scope, key);
  return {
    key,
    clear: () => {
      pendingKeys.delete(scope);
      try {
        sessionStorage.removeItem(storage);
      } catch {
        /* No persistent key exists. */
      }
    },
  };
}

export default function useRenderMutation(
  scope: string,
  writable: boolean,
  unavailable: (cause: unknown) => void,
) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const write = useRef<AbortController | null>(null);
  const live = useRef(false);
  const submitting = useRef(false);
  const currentScope = useRef(scope);
  const currentWritable = useRef(writable);
  currentScope.current = scope;
  currentWritable.current = writable;
  useEffect(() => {
    live.current = true;
    setError("");
    setNotice("");
    setBusy(false);
    return () => {
      live.current = false;
      write.current?.abort();
    };
  }, [scope, writable]);
  async function mutate<T>(
    action: (signal: AbortSignal) => Promise<T>,
    onSuccess: (result: T) => void | Promise<void>,
    success: string,
    reportPermissionFailure = true,
  ) {
    if (submitting.current || !currentWritable.current) return;
    submitting.current = true;
    const controller = new AbortController();
    write.current = controller;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const result = await action(controller.signal);
      if (
        controller.signal.aborted ||
        !live.current ||
        currentScope.current !== scope ||
        !currentWritable.current
      )
        return;
      await onSuccess(result);
      if (
        !controller.signal.aborted &&
        live.current &&
        currentScope.current === scope
      )
        setNotice(success);
    } catch (cause) {
      if (
        controller.signal.aborted ||
        !live.current ||
        currentScope.current !== scope
      )
        return;
      if (
        reportPermissionFailure &&
        cause instanceof ApiError &&
        [401, 403, 404].includes(cause.status)
      )
        unavailable(cause);
      setError(message(cause));
    } finally {
      submitting.current = false;
      if (write.current === controller) {
        write.current = null;
        if (live.current) setBusy(false);
      }
    }
  }
  return { busy, error, notice, mutate };
}
