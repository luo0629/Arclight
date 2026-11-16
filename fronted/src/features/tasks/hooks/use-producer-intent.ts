"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, message } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import type {
  ProducerAccepted,
  ProducerAnswerPayload,
  ProducerCommandPayload,
  ProducerStartPayload,
} from "../producer-types";

type NewIntent =
  | { kind: "START"; payload: ProducerStartPayload }
  | { kind: "COMMAND"; payload: ProducerCommandPayload }
  | { kind: "ANSWER"; loopId: string; payload: ProducerAnswerPayload };

export type ProducerIntent = {
  scope: string;
  key: string;
  kind: NewIntent["kind"];
  loopId?: string;
  body: string;
};

function storageKey(scope: string) {
  return `arclight:producer-intent:${scope}`;
}

function readIntent(scope: string, runId: string): ProducerIntent | null {
  const raw = sessionStorage.getItem(storageKey(scope));
  if (!raw) return null;
  const value: ProducerIntent = JSON.parse(raw);
  if (
    !value ||
    value.scope !== scope ||
    typeof value.key !== "string" ||
    !/^[A-Za-z0-9_.:-]{1,100}$/.test(value.key) ||
    typeof value.body !== "string" ||
    value.body.length > 16000 ||
    (runId
      ? !["COMMAND", "ANSWER"].includes(value.kind)
      : value.kind !== "START") ||
    (value.kind === "ANSWER" && (!value.loopId || !isResourceId(value.loopId)))
  )
    throw new Error("原操作记录无法读取，请保留当前会话并核验执行记录。");
  const body: unknown = JSON.parse(value.body);
  if (!body || typeof body !== "object" || Array.isArray(body))
    throw new Error("原操作内容无法读取，请先核验执行记录。");
  return value;
}

/** Keep the exact request across uncertain responses, refreshes and permission changes. */
export default function useProducerIntent({
  userId,
  projectId,
  episodeId,
  runId = "",
  writable,
}: {
  userId: string;
  projectId: string;
  episodeId: string;
  runId?: string;
  writable: boolean;
}) {
  const scope = `${userId}:${projectId}:${episodeId}:${runId || "start"}`;
  const [pending, setPending] = useState<ProducerIntent | null>(null);
  const [ready, setReady] = useState(false);
  const [mutating, setMutating] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const currentScope = useRef(scope);
  const allowed = useRef(writable);
  const live = useRef(false);
  const busy = useRef(false);
  const controller = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  currentScope.current = scope;
  allowed.current = writable;

  useEffect(() => {
    live.current = true;
    epoch.current++;
    busy.current = false;
    setMutating(false);
    setReady(false);
    setPending(null);
    setError("");
    setNotice("");
    try {
      setPending(readIntent(scope, runId));
      setReady(true);
    } catch (cause) {
      setError(message(cause));
    }
    return () => {
      live.current = false;
      epoch.current++;
      controller.current?.abort();
    };
  }, [scope, runId, writable]);

  const execute = useCallback(
    async (
      intent: ProducerIntent,
      replaying = false,
    ): Promise<ProducerAccepted | null> => {
      if (
        !live.current ||
        currentScope.current !== scope ||
        !allowed.current ||
        busy.current
      )
        return null;
      const operationEpoch = epoch.current;
      const isCurrent = () =>
        live.current &&
        currentScope.current === scope &&
        epoch.current === operationEpoch;
      busy.current = true;
      setMutating(true);
      setError("");
      setNotice("");
      const request = new AbortController();
      controller.current = request;
      try {
        const stored = readIntent(scope, runId);
        if (stored?.key !== intent.key || stored.body !== intent.body)
          throw new Error("原操作记录已变化，请刷新页面后核验。");
        const path =
          intent.kind === "START"
            ? `/episodes/${episodeId}/producer-runs`
            : intent.kind === "ANSWER"
              ? `/runs/${runId}/producer/${intent.loopId}/answers`
              : `/runs/${runId}/producer/commands`;
        const accepted = await api<ProducerAccepted>(path, {
          method: "POST",
          body: intent.body,
          headers: { "Idempotency-Key": intent.key },
          signal: request.signal,
        });
        if (
          !accepted ||
          !isResourceId(accepted.run_id) ||
          (runId && accepted.run_id !== runId)
        )
          throw new Error("操作响应无法核验，请刷新状态后重发原操作。");
        if (readIntent(scope, runId)?.key === intent.key)
          sessionStorage.removeItem(storageKey(scope));
        if (!isCurrent()) return null;
        setPending(null);
        setNotice("操作已提交，生产状态以服务端最新记录为准。");
        return accepted;
      } catch (cause) {
        // A definite rejection did not accept this request. Transport/server failures
        // retain both bytes and key; replay can only send that original request.
        const rejected =
          cause instanceof ApiError &&
          [400, 401, 403, 404, 409, 422].includes(cause.status);
        // A denied replay cannot establish what happened before permissions changed.
        // Revision conflicts are checked after the server's existing-key lookup.
        const release =
          rejected &&
          (!replaying ||
            [
              "REVISION_CONFLICT",
              "PRODUCER_PLAN_SUPERSEDED",
              "PRODUCER_COMMAND_PENDING",
            ].includes(cause.code));
        if (release) {
          try {
            if (readIntent(scope, runId)?.key === intent.key)
              sessionStorage.removeItem(storageKey(scope));
          } catch {
            // An unreadable stored record must continue to block replacement writes.
          }
        }
        if (isCurrent()) {
          if (release) setPending(null);
          setError(
            release
              ? message(cause)
              : `操作结果尚未确认，已保留原请求。请刷新状态，再重发原操作核验：${message(cause)}`,
          );
        }
        return null;
      } finally {
        if (isCurrent()) {
          busy.current = false;
          setMutating(false);
        }
      }
    },
    [scope, runId, episodeId],
  );

  const submit = useCallback(
    async (intent: NewIntent) => {
      if (!ready || busy.current || !allowed.current) return null;
      try {
        if (readIntent(scope, runId)) {
          setError("请先核验并重发原操作，不能更换尚未确认的请求。");
          return null;
        }
        const stored: ProducerIntent = {
          scope,
          key: crypto.randomUUID(),
          kind: intent.kind,
          ...(intent.kind === "ANSWER" ? { loopId: intent.loopId } : {}),
          body: JSON.stringify(intent.payload),
        };
        sessionStorage.setItem(storageKey(scope), JSON.stringify(stored));
        setPending(stored);
        return await execute(stored);
      } catch (cause) {
        setError(`无法保存原操作，未发送请求：${message(cause)}`);
        return null;
      }
    },
    [ready, scope, runId, execute],
  );

  return {
    ready,
    pending,
    mutating,
    error,
    notice,
    submit,
    replay: () => (pending ? execute(pending, true) : Promise.resolve(null)),
  };
}
