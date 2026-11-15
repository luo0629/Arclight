"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/features/auth/auth-context";
import { api, ApiError, json, message } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import type { ProductionCandidate } from "../shot-production-types";
import {
  revisionOperations,
  type PendingRevision,
  type RevisionCommand,
  type RevisionOperation,
  type RevisionPreview,
  type RevisionReceipt,
  type RevisionSource,
} from "../shot-revision-types";

export default function useShotRevision(
  projectId: string,
  episodeId: string,
  storyboardId: string,
  shotId: string,
  candidate: ProductionCandidate,
  onChanged: () => Promise<void>,
  onUnavailable: (cause: unknown) => void,
) {
  const { user } = useAuth();
  const endpoint = `/projects/${projectId}/continuity-revisions/shots`;
  const storageKey = `arclight:shot-revision:v1:${user.id}:${projectId}:${episodeId}:${storyboardId}:${shotId}:${candidate.id}`;
  const [operation, setOperation] = useState<RevisionOperation>("RECOMPOSE");
  const [instruction, setInstruction] = useState("");
  const [preview, setPreview] = useState<RevisionPreview | null>(null);
  const [pending, setPending] = useState<PendingRevision | null>(null);
  const [busy, setBusy] = useState(false);
  const [blocked, setBlocked] = useState(false);
  const [error, setError] = useState("");
  const live = useRef(true);
  const operating = useRef(false);
  const saved = useRef<PendingRevision | null>(null);
  const permissionLost = useRef(false);

  const matches = useCallback(
    (source: RevisionSource) =>
      source.candidate_id === candidate.id &&
      source.asset_id === candidate.asset_id &&
      source.version_id === candidate.version_id &&
      source.episode_id === episodeId &&
      source.storyboard_id === storyboardId &&
      source.shot_id === shotId,
    [
      candidate.id,
      candidate.asset_id,
      candidate.version_id,
      episodeId,
      storyboardId,
      shotId,
    ],
  );
  const unavailable = useCallback(
    (cause: unknown) => {
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        permissionLost.current = true;
        saved.current = null;
        try {
          sessionStorage.removeItem(storageKey);
        } catch {
          /* already blocked */
        }
        if (live.current) {
          setPending(null);
          setPreview(null);
          setBlocked(true);
        }
      }
      if (live.current) onUnavailable(cause);
    },
    [storageKey, onUnavailable],
  );
  useEffect(() => {
    live.current = true;
    try {
      const value: PendingRevision | null = JSON.parse(
        sessionStorage.getItem(storageKey) ?? "null",
      );
      if (value !== null) {
        if (
          value.schema !== 1 ||
          value.user_id !== user.id ||
          !isResourceId(value.key) ||
          !["unknown", "accepted"].includes(value.phase) ||
          !matches(value.preview.source) ||
          value.preview.intent.candidate_id !== candidate.id ||
          value.preview.intent.storyboard_id !== storyboardId ||
          value.preview.intent.shot_id !== shotId ||
          value.preview.intent.operation !== value.command.operation ||
          value.preview.intent.instruction !== value.command.instruction ||
          value.command.candidate_id !== candidate.id ||
          value.command.storyboard_id !== storyboardId ||
          value.command.shot_id !== shotId ||
          value.command.mode !== "PROMPT_REGENERATION" ||
          !Object.hasOwn(revisionOperations, value.command.operation) ||
          typeof value.command.instruction !== "string" ||
          !value.command.instruction.trim() ||
          value.command.instruction.length > 2000 ||
          !/^[0-9a-f]{64}$/.test(value.command.confirmation_digest) ||
          value.command.confirmation_digest !==
            value.preview.confirmation_digest ||
          (value.phase === "accepted" &&
            (!value.receipt ||
              !isResourceId(value.receipt.job_id) ||
              !matches(value.receipt.source)))
        )
          throw new Error("本标签页的修订记录损坏，请先在任务中心核对原任务。");
        saved.current = value;
        setPending(value);
        setPreview(value.preview);
        setOperation(value.command.operation);
        setInstruction(value.command.instruction);
      }
    } catch (cause) {
      setBlocked(true);
      setError(message(cause));
    }
    return () => {
      live.current = false;
    };
  }, [storageKey, user.id, matches, candidate.id, storyboardId, shotId]);

  function persist(value: PendingRevision | null) {
    if (value && permissionLost.current) return;
    if (
      value?.phase === "accepted" &&
      JSON.parse(sessionStorage.getItem(storageKey) ?? "null")?.key !==
        value.key
    ) {
      permissionLost.current = true;
      if (live.current) setBlocked(true);
      throw new Error("修订记录已清除，请在任务中心核对已受理任务。");
    }
    if (value) sessionStorage.setItem(storageKey, JSON.stringify(value));
    else sessionStorage.removeItem(storageKey);
    saved.current = value;
    if (live.current) setPending(value);
  }
  function edit(nextOperation: RevisionOperation, nextInstruction: string) {
    if (operating.current || saved.current || blocked) return;
    setOperation(nextOperation);
    setInstruction(nextInstruction);
    setPreview(null);
    setError("");
  }
  async function prepare() {
    if (operating.current || saved.current || blocked || !instruction.trim())
      return;
    operating.current = true;
    setBusy(true);
    setError("");
    setPreview(null);
    try {
      const next = await api<RevisionPreview>(
        endpoint + "/preview",
        json("POST", {
          candidate_id: candidate.id,
          storyboard_id: storyboardId,
          shot_id: shotId,
          operation,
          instruction: instruction.trim(),
          mode: "PROMPT_REGENERATION",
        }),
      );
      if (
        !matches(next.source) ||
        !/^[0-9a-f]{64}$/.test(next.confirmation_digest) ||
        !/^\d+(\.\d{1,6})?$/.test(next.quote.maximum_cost) ||
        next.quote.currency !== "USD"
      )
        throw new Error("修订预览与当前精确候选或可信报价不一致。");
      if (live.current && !permissionLost.current) setPreview(next);
    } catch (cause) {
      if (live.current) setError(message(cause));
      unavailable(cause);
    } finally {
      operating.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function submit() {
    if (operating.current || blocked || saved.current?.phase === "accepted")
      return;
    const frozenPreview = saved.current?.preview ?? preview;
    if (!frozenPreview) return;
    const command: RevisionCommand = {
      candidate_id: candidate.id,
      storyboard_id: storyboardId,
      shot_id: shotId,
      operation: frozenPreview.intent.operation,
      instruction: frozenPreview.intent.instruction,
      mode: "PROMPT_REGENERATION",
      confirmation_digest: frozenPreview.confirmation_digest,
    };
    const record = saved.current ?? {
      schema: 1 as const,
      user_id: user.id,
      key: crypto.randomUUID(),
      command,
      preview: frozenPreview,
      phase: "unknown" as const,
    };
    operating.current = true;
    setBusy(true);
    setError("");
    let sent = false;
    try {
      persist(record);
      sent = true;
      const receipt = await api<RevisionReceipt>(endpoint, {
        ...json("POST", record.command),
        headers: { "Idempotency-Key": record.key },
      });
      if (
        !isResourceId(receipt.job_id) ||
        !matches(receipt.source) ||
        receipt.mode !== "PROMPT_REGENERATION"
      )
        throw new Error("修订回执与原请求不一致，请核对原任务。");
      if (permissionLost.current) return;
      persist({ ...record, phase: "accepted", receipt });
      if (live.current) {
        try {
          await onChanged();
        } catch (cause) {
          setError(message(cause));
          unavailable(cause);
        }
      }
    } catch (cause) {
      if (
        cause instanceof ApiError &&
        cause.status < 500 &&
        cause.code !== "IDEMPOTENCY_CONFLICT"
      ) {
        try {
          persist(null);
        } catch {
          if (live.current) setBlocked(true);
        }
        if (live.current) setPreview(null);
      } else if (!sent && live.current) setBlocked(true);
      if (live.current) setError(message(cause));
      unavailable(cause);
    } finally {
      operating.current = false;
      if (live.current) setBusy(false);
    }
  }
  function restart() {
    if (operating.current || blocked || saved.current?.phase !== "accepted")
      return;
    try {
      persist(null);
      setPreview(null);
      setError("");
    } catch (cause) {
      setBlocked(true);
      setError(message(cause));
    }
  }
  return {
    operation,
    instruction,
    preview,
    pending,
    busy,
    blocked,
    error,
    edit,
    prepare,
    submit,
    restart,
    unavailable,
  };
}
