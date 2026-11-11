"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { api, ApiError, json, message } from "@/lib/api/client";
import type { CostTotals } from "@/features/projects/components/project-costs";
import {
  MEDIA_COST_PAGE_SIZE,
  MEDIA_PAGE_SIZE,
  MEDIA_TERMINAL_STATUSES,
  MEDIA_UNKNOWN_STATUSES,
  type MediaAction,
  type MediaActionPayload,
  type MediaAttempt,
  type MediaCall,
  type MediaCostEntries,
  type MediaDeadLetter,
  type MediaDetail,
  type MediaJob,
  type MediaLane,
  type MediaQueue,
  type MediaSchedule,
  type ResolvePayload,
} from "../media-types";

type RawAttempt = Omit<MediaAttempt, "batchSize" | "outputHandles"> & {
  input_snapshot_json?: {
    context?: {
      generation?: { batch_size?: unknown };
      continuity_revision?: unknown;
    };
  };
  result_json?: { output_handles?: unknown } | null;
};
type PendingReplay = {
  projectId: string;
  sourceJobId: string;
  key: string;
  expected_revision: number;
  reason: string;
};
const active = (job: MediaJob) => !MEDIA_TERMINAL_STATUSES.includes(job.status);
const boundedOffset = (value: number, size: number) =>
  Number.isSafeInteger(value) && value >= 0
    ? Math.floor(value / size) * size
    : 0;

function publicAttempt(value: RawAttempt): MediaAttempt {
  const batchSize = value.input_snapshot_json?.context?.generation?.batch_size;
  const handles = value.result_json?.output_handles;
  return {
    id: value.id,
    job_id: value.job_id,
    attempt_no: value.attempt_no,
    input_manifest_id: value.input_manifest_id,
    provider: value.provider,
    model: value.model,
    status: value.status,
    error_code: value.error_code,
    error_category: value.error_category,
    latency_ms: value.latency_ms,
    submitted_at: value.submitted_at,
    finished_at: value.finished_at,
    isContinuityRevision: Boolean(
      value.input_snapshot_json?.context?.continuity_revision,
    ),
    batchSize:
      typeof batchSize === "number" &&
      Number.isInteger(batchSize) &&
      batchSize >= 1 &&
      batchSize <= 64
        ? batchSize
        : null,
    outputHandles: Array.isArray(handles)
      ? handles.filter(
          (handle): handle is string =>
            typeof handle === "string" &&
            /^[A-Za-z0-9_.:/-]{1,200}$/.test(handle) &&
            !handle.includes("://"),
        )
      : [],
  };
}

function replayStorageKey(projectId: string, jobId: string) {
  return `arclight:media-replay:${projectId}:${jobId}`;
}
function readReplay(projectId: string, jobId: string): PendingReplay | null {
  const stored = sessionStorage.getItem(replayStorageKey(projectId, jobId));
  if (!stored) return null;
  const value: PendingReplay = JSON.parse(stored);
  if (
    value.projectId !== projectId ||
    value.sourceJobId !== jobId ||
    typeof value.key !== "string" ||
    !/^[!-~]{1,160}$/.test(value.key) ||
    typeof value.reason !== "string" ||
    !value.reason.trim() ||
    value.reason.length > 1000 ||
    !Number.isSafeInteger(value.expected_revision) ||
    value.expected_revision < 0
  )
    throw new Error(
      "未确认的重新发起记录无法读取，请保留此会话并核实任务记录。",
    );
  return value;
}
function clearReplay(value: PendingReplay) {
  const current = readReplay(value.projectId, value.sourceJobId);
  if (current?.key === value.key)
    sessionStorage.removeItem(
      replayStorageKey(value.projectId, value.sourceJobId),
    );
}

export default function useMediaTasks({
  projectId,
  scopeKey,
  writable,
  owner,
}: {
  projectId: string;
  scopeKey: string;
  writable: boolean;
  owner: boolean;
}) {
  const scope = `${projectId}:${scopeKey}`;
  const [jobs, setJobs] = useState<MediaJob[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [offset, updateOffset] = useState(0);
  const [hasMore, setHasMore] = useState(false);
  const [queue, setQueue] = useState<MediaQueue | null>(null);
  const [queueError, setQueueError] = useState("");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [detail, setDetail] = useState<MediaDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [detailFresh, setDetailFresh] = useState(false);
  const [attemptOffset, updateAttemptOffset] = useState(0);
  const [callOffset, updateCallOffset] = useState(0);
  const [costOffset, updateCostOffset] = useState(0);
  const [mutating, setMutating] = useState(false);
  const [notice, setNotice] = useState("");
  const [pendingReplay, setPendingReplay] = useState<PendingReplay | null>(
    null,
  );
  const [listVersion, setListVersion] = useState(0);
  const [detailVersion, setDetailVersion] = useState(0);
  const live = useRef(false);
  const currentScope = useRef(scope);
  const currentSelection = useRef(selectedId);
  const permissions = useRef({ writable, owner });
  const mutatingNow = useRef(false);
  const listRead = useRef<AbortController | null>(null);
  const detailRead = useRef<AbortController | null>(null);
  const command = useRef<AbortController | null>(null);
  const epoch = useRef(0);
  const revisions = useRef(new Map<string, MediaJob>());
  const currentDetail = useRef(detail);
  const fresh = useRef(detailFresh);
  currentScope.current = scope;
  currentSelection.current = selectedId;
  permissions.current = { writable, owner };
  currentDetail.current = detail;
  fresh.current = detailFresh;

  const remember = useCallback((job: MediaJob) => {
    const known = revisions.current.get(job.id);
    if (known && known.revision > job.revision) return known;
    revisions.current.set(job.id, job);
    return job;
  }, []);

  useEffect(() => {
    live.current = true;
    revisions.current.clear();
    setJobs([]);
    setQueue(null);
    setSelectedId(null);
    setDetail(null);
    setDetailFresh(false);
    updateOffset(0);
    updateAttemptOffset(0);
    updateCallOffset(0);
    updateCostOffset(0);
    setNotice("");
    setPendingReplay(null);
    setMutating(false);
    mutatingNow.current = false;
    return () => {
      live.current = false;
      epoch.current++;
      listRead.current?.abort();
      detailRead.current?.abort();
      command.current?.abort();
    };
  }, [scope]);

  const loadList = useCallback(async () => {
    if (!projectId || mutatingNow.current) return false;
    listRead.current?.abort();
    const controller = new AbortController();
    listRead.current = controller;
    const started = epoch.current;
    const valid = () =>
      live.current &&
      currentScope.current === scope &&
      !controller.signal.aborted &&
      started === epoch.current;
    setLoading(true);
    setError("");
    const base = `/projects/${projectId}/generation-jobs`;
    try {
      const [result, queueResult] = await Promise.allSettled([
        api<MediaJob[]>(`${base}?limit=${MEDIA_PAGE_SIZE}&offset=${offset}`, {
          signal: controller.signal,
        }),
        api<MediaQueue>(`${base}/queue`, { signal: controller.signal }),
      ]);
      if (!valid()) return false;
      if (result.status === "rejected") throw result.reason;
      if (
        queueResult.status === "rejected" &&
        queueResult.reason instanceof ApiError &&
        [401, 403, 404].includes(queueResult.reason.status)
      )
        throw queueResult.reason;
      const rows = result.value.map(remember);
      setJobs(rows);
      setHasMore(result.value.length === MEDIA_PAGE_SIZE);
      const opened = currentDetail.current;
      if (
        opened &&
        rows.some(
          (row) =>
            row.id === opened.job.id && row.revision > opened.job.revision,
        )
      ) {
        setDetailFresh(false);
        fresh.current = false;
        setDetailVersion((version) => version + 1);
      }
      if (queueResult.status === "fulfilled") {
        setQueue(queueResult.value);
        setQueueError("");
      } else {
        setQueue(null);
        setQueueError(message(queueResult.reason));
      }
      return (
        rows.some(active) ||
        (queueResult.status === "fulfilled" &&
          queueResult.value.lanes.some(
            (lane) =>
              lane.ready +
                lane.backoff +
                lane.waiting_permit +
                lane.executing +
                lane.unknown >
              0,
          ))
      );
    } catch (cause) {
      if (!valid()) return false;
      setJobs([]);
      setHasMore(false);
      setQueue(null);
      setError(message(cause));
      setDetailFresh(false);
      fresh.current = false;
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        detailRead.current?.abort();
        setDetail(null);
        currentDetail.current = null;
        setSelectedId(null);
        currentSelection.current = null;
        setPendingReplay(null);
      }
      return false;
    } finally {
      if (listRead.current === controller) {
        listRead.current = null;
        if (live.current && currentScope.current === scope) setLoading(false);
      }
    }
  }, [projectId, scope, offset, remember]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const again = await loadList();
      if (!stopped && again) timer = setTimeout(() => void tick(), 3000);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      listRead.current?.abort();
    };
  }, [loadList, listVersion]);

  const loadDetail = useCallback(async () => {
    if (!selectedId || mutatingNow.current) return false;
    detailRead.current?.abort();
    const controller = new AbortController();
    detailRead.current = controller;
    const started = epoch.current;
    const valid = () =>
      live.current &&
      currentScope.current === scope &&
      currentSelection.current === selectedId &&
      !controller.signal.aborted &&
      started === epoch.current;
    const base = `/projects/${projectId}/generation-jobs/${selectedId}`;
    const read = <T>(path: string) =>
      api<T>(path, { signal: controller.signal });
    setDetailLoading(true);
    setDetailFresh(false);
    fresh.current = false;
    setDetailError("");
    try {
      const job = await read<MediaJob>(base);
      if (!valid()) return false;
      if (job.id !== selectedId || job.project_id !== projectId)
        throw new Error("任务不属于当前项目，请刷新后重试。");
      const errors: Record<string, string> = {};
      const part = async <T>(
        name: string,
        path: string,
        fallback: T,
        absent = false,
      ) => {
        try {
          return await read<T>(path);
        } catch (cause) {
          if (!(absent && cause instanceof ApiError && cause.status === 404))
            errors[name] = message(cause);
          return fallback;
        }
      };
      const [
        attemptRows,
        latestRows,
        scheduleRaw,
        deadRaw,
        lane,
        calls,
        costReport,
        costEntries,
      ] = await Promise.all([
        part<RawAttempt[]>(
          "attempts",
          `${base}/attempts?limit=20&offset=${attemptOffset}`,
          [],
        ),
        job.attempt_count > 0
          ? part<RawAttempt[]>(
              "latestAttempt",
              `${base}/attempts?limit=1&offset=${Math.max(0, job.attempt_count - 1)}`,
              [],
            )
          : Promise.resolve([] as RawAttempt[]),
        part<
          | (Omit<MediaSchedule, "policy"> & {
              policy_json: Record<string, unknown>;
            })
          | null
        >("schedule", `${base}/schedule`, null),
        part<MediaDeadLetter | null>("deadLetter", `${base}/dead-letter`, null),
        part<MediaLane | null>("lane", `${base}/queue`, null),
        part<MediaCall[]>(
          "calls",
          `${base}/calls?limit=20&offset=${callOffset}`,
          [],
        ),
        part<{ summary: CostTotals } | null>(
          "costs",
          `/projects/${projectId}/costs?group_by=task&task_id=${selectedId}`,
          null,
          true,
        ),
        part<MediaCostEntries | null>(
          "costEntries",
          `/projects/${projectId}/costs/entries?task_id=${selectedId}&limit=10&offset=${costOffset}`,
          null,
          true,
        ),
      ]);
      const after = await read<MediaJob>(base);
      if (!valid()) return false;
      const known = remember(after);
      if (
        after.revision !== job.revision ||
        known.revision !== after.revision
      ) {
        setDetailError("任务状态已变化，正在重新读取详情。");
        return true;
      }
      const latestAttempt = latestRows[0] ? publicAttempt(latestRows[0]) : null;
      if (job.attempt_count > 0 && !latestAttempt && !errors.latestAttempt)
        errors.latestAttempt = "尚未取得最新执行记录，请刷新后操作。";
      const policy: MediaSchedule["policy"] = {};
      for (const field of [
        "max_attempts",
        "max_elapsed_seconds",
        "retry_base_seconds",
        "retry_max_seconds",
      ] as const) {
        const value = scheduleRaw?.policy_json?.[field];
        if (typeof value === "number" && Number.isFinite(value))
          policy[field] = value;
      }
      const schedule: MediaSchedule | null = scheduleRaw
        ? {
            deadline_at: scheduleRaw.deadline_at,
            next_attempt_at: scheduleRaw.next_attempt_at,
            blocked_code: scheduleRaw.blocked_code,
            policy,
          }
        : null;
      const deadLetter: MediaDeadLetter | null = deadRaw
        ? {
            id: deadRaw.id,
            attempt_id: deadRaw.attempt_id,
            reason: deadRaw.reason,
            error_code: deadRaw.error_code,
            category: deadRaw.category,
          }
        : null;
      const next: MediaDetail = {
        job: known,
        attempts: attemptRows.map(publicAttempt),
        latestAttempt,
        batchSize: latestAttempt?.batchSize ?? null,
        schedule,
        deadLetter,
        lane: lane
          ? {
              job_id: lane.job_id,
              lane: lane.lane,
              origin: lane.origin,
              policy_version: lane.policy_version,
            }
          : null,
        calls,
        costs: costReport?.summary ?? null,
        costEntries,
        errors,
      };
      setDetail(next);
      currentDetail.current = next;
      const ready = !errors.latestAttempt && !errors.attempts;
      setDetailFresh(ready);
      fresh.current = ready;
      setJobs((rows) => rows.map((row) => (row.id === known.id ? known : row)));
      return active(known);
    } catch (cause) {
      if (!valid()) return false;
      setDetailError(message(cause));
      setDetailFresh(false);
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        setDetail(null);
        currentDetail.current = null;
        setJobs([]);
        setQueue(null);
        setHasMore(false);
      }
      return false;
    } finally {
      if (detailRead.current === controller) {
        detailRead.current = null;
        if (live.current && currentScope.current === scope)
          setDetailLoading(false);
      }
    }
  }, [
    projectId,
    scope,
    selectedId,
    attemptOffset,
    callOffset,
    costOffset,
    remember,
  ]);

  useEffect(() => {
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const again = await loadDetail();
      if (!stopped && again) timer = setTimeout(() => void tick(), 3000);
    };
    void tick();
    return () => {
      stopped = true;
      clearTimeout(timer);
      detailRead.current?.abort();
    };
  }, [loadDetail, detailVersion]);

  const refresh = useCallback(() => {
    listRead.current?.abort();
    detailRead.current?.abort();
    setDetailFresh(false);
    fresh.current = false;
    setListVersion((value) => value + 1);
    setDetailVersion((value) => value + 1);
  }, []);

  const selectJob = useCallback(
    (id: string | null) => {
      detailRead.current?.abort();
      setSelectedId(id);
      currentSelection.current = id;
      setDetail(null);
      currentDetail.current = null;
      setDetailFresh(false);
      fresh.current = false;
      setDetailError("");
      setNotice("");
      updateAttemptOffset(0);
      updateCallOffset(0);
      updateCostOffset(0);
      try {
        setPendingReplay(id ? readReplay(projectId, id) : null);
      } catch {
        setPendingReplay(null);
        setNotice(
          "无法读取浏览器中的未确认命令。重新发起已暂停，请检查会话存储后重试。",
        );
      }
    },
    [projectId],
  );

  const mutate = useCallback(
    async (action: MediaAction, payload?: MediaActionPayload) => {
      const value = currentDetail.current;
      if (
        mutatingNow.current ||
        !value ||
        !fresh.current ||
        !permissions.current.writable
      )
        return false;
      const { job, latestAttempt } = value;
      if (currentSelection.current !== job.id || currentScope.current !== scope)
        return false;
      const unknown = MEDIA_UNKNOWN_STATUSES.includes(job.status);
      const allowed =
        action === "retry"
          ? job.status === "FAILED" &&
            !job.cancel_requested_at &&
            latestAttempt?.status === "FAILED" &&
            !!latestAttempt.finished_at
          : action === "cancel"
            ? !["SUCCEEDED", "CANCELLED", "CANCEL_REQUESTED"].includes(
                job.status,
              ) && !job.cancel_requested_at
            : action === "reconcile"
              ? unknown
              : action === "resolve"
                ? permissions.current.owner &&
                  unknown &&
                  !!latestAttempt &&
                  !latestAttempt.finished_at
                : permissions.current.owner &&
                  !latestAttempt?.isContinuityRevision &&
                  !!value.deadLetter &&
                  ["FAILED", "CANCELLED"].includes(job.status);
      if (!allowed) {
        setNotice("当前权限或任务状态不允许此操作，请刷新任务。");
        return false;
      }
      let replay: PendingReplay | null = null;
      let replayWasPending = false;
      let body: Record<string, unknown> = { expected_revision: job.revision };
      try {
        if (action === "resolve") {
          const resolution = payload as ResolvePayload | undefined;
          if (
            !resolution?.confirmed ||
            !resolution.evidence_reference ||
            !resolution.receipt
          )
            throw new Error("请填写证据和明确的供应商结果并确认。");
          body = { ...resolution, expected_revision: job.revision };
        }
        if (action === "replay") {
          const reason = payload && "reason" in payload ? payload.reason : "";
          if (!reason.trim() || reason.length > 1000)
            throw new Error("请填写 1–1000 字的重新发起原因。");
          replay = readReplay(projectId, job.id);
          replayWasPending = replay !== null;
          if (replay && replay.reason !== reason)
            throw new Error(
              "上次重新发起结果尚未确认，请沿用原原因重试，以核实同一个新任务。",
            );
          replay ??= {
            projectId,
            sourceJobId: job.id,
            key: crypto.randomUUID(),
            expected_revision: job.revision,
            reason,
          };
          sessionStorage.setItem(
            replayStorageKey(projectId, job.id),
            JSON.stringify(replay),
          );
          setPendingReplay(replay);
          body = {
            expected_revision: replay.expected_revision,
            reason: replay.reason,
          };
        }
      } catch (cause) {
        setNotice(
          action === "replay" &&
            !(cause instanceof Error && cause.message.includes("原因"))
            ? "浏览器无法保存或读取未确认的重新发起命令，请检查会话存储后重试。"
            : message(cause),
        );
        return false;
      }
      mutatingNow.current = true;
      setMutating(true);
      setNotice("");
      setDetailFresh(false);
      fresh.current = false;
      epoch.current++;
      listRead.current?.abort();
      detailRead.current?.abort();
      const controller = new AbortController();
      command.current = controller;
      const valid = () =>
        live.current &&
        currentScope.current === scope &&
        currentSelection.current === job.id;
      try {
        const result = await api<MediaJob>(
          `/projects/${projectId}/generation-jobs/${job.id}/${action}`,
          {
            ...json("POST", body),
            signal: controller.signal,
            ...(replay ? { headers: { "Idempotency-Key": replay.key } } : {}),
          },
        );
        if (replay) {
          clearReplay(replay);
          if (valid()) setPendingReplay(null);
        }
        if (!valid() || controller.signal.aborted) return false;
        remember(result);
        const status = result.status_label || result.status;
        setNotice(
          action === "replay"
            ? `已创建新任务 ${result.id}，状态：${status}。原任务与费用历史保留，新任务独立预留预算。`
            : action === "cancel"
              ? result.status === "CANCELLED"
                ? "任务已取消。"
                : `已记录取消请求，当前状态：${status}。外部调用可能仍需核验。`
              : action === "retry"
                ? result.status === "FAILED"
                  ? "任务仍为失败，未确认重新排队。请查看调度阻塞与死信原因。"
                  : `重试请求已处理，当前状态：${status}。`
                : `供应商结果已核验，当前状态：${status}。`,
        );
        if (action === "replay") {
          setSelectedId(result.id);
          currentSelection.current = result.id;
          setDetail(null);
          currentDetail.current = null;
          updateOffset(0);
          updateAttemptOffset(0);
          updateCallOffset(0);
          updateCostOffset(0);
        } else {
          setJobs((rows) =>
            rows.map((row) => (row.id === result.id ? result : row)),
          );
        }
        return true;
      } catch (cause) {
        // Only a definitive rejection of the very first request proves no replay
        // was created. A later denial cannot disprove an earlier lost response.
        const replayRejected =
          replay !== null &&
          !replayWasPending &&
          cause instanceof ApiError &&
          cause.status >= 400 &&
          cause.status < 500;
        if (replayRejected && replay) {
          try {
            clearReplay(replay);
            if (valid()) setPendingReplay(null);
          } catch {
            // Retain the durable command if storage is temporarily unavailable.
          }
        }
        if (!valid() || controller.signal.aborted) return false;
        if (
          cause instanceof ApiError &&
          [401, 403, 404].includes(cause.status)
        ) {
          setDetail(null);
          currentDetail.current = null;
          setJobs([]);
          setQueue(null);
          setHasMore(false);
        }
        setNotice(
          cause instanceof ApiError && cause.status === 409
            ? `${message(cause)}。正在刷新权威状态，请核对后再操作。`
            : replayRejected
              ? `${message(cause)}。本次重新发起已被拒绝，请核对原因后修改并重试。`
              : replay
                ? `${message(cause)}。重新发起结果尚未确认；再次提交同一原因会沿用原命令，避免创建重复任务。`
                : `${message(cause)}。正在刷新任务，请核对服务端结果后再操作。`,
        );
        return false;
      } finally {
        const ownsCommand = command.current === controller;
        if (ownsCommand) {
          command.current = null;
          mutatingNow.current = false;
        }
        if (ownsCommand && live.current && currentScope.current === scope) {
          setMutating(false);
          setListVersion((version) => version + 1);
          setDetailVersion((version) => version + 1);
        }
      }
    },
    [projectId, scope, remember],
  );

  return {
    jobs,
    loading,
    error,
    offset,
    hasMore,
    queue,
    queueError,
    selectedId,
    selectJob,
    detail,
    detailLoading,
    detailError,
    detailFresh,
    refresh,
    mutate,
    mutating,
    notice,
    pendingReplay,
    setOffset: (value: number) => {
      selectJob(null);
      updateOffset(boundedOffset(value, MEDIA_PAGE_SIZE));
    },
    attemptOffset,
    setAttemptOffset: (value: number) => {
      setDetailFresh(false);
      fresh.current = false;
      updateAttemptOffset(boundedOffset(value, MEDIA_PAGE_SIZE));
    },
    callOffset,
    setCallOffset: (value: number) => {
      setDetailFresh(false);
      fresh.current = false;
      updateCallOffset(boundedOffset(value, MEDIA_PAGE_SIZE));
    },
    costOffset,
    setCostOffset: (value: number) => {
      setDetailFresh(false);
      fresh.current = false;
      updateCostOffset(boundedOffset(value, MEDIA_COST_PAGE_SIZE));
    },
  };
}
