"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/features/auth/auth-context";
import { api, ApiError, json, message } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import {
  batchActions,
  type BatchAction,
  type BatchItem,
  type ImageConfiguration,
  type ProductionPage,
  type ShotBatch,
} from "../shot-production-types";

export default function useShotProduction(
  projectId: string,
  episodeId: string,
  storyboardId: string,
  offset: number,
) {
  const { user } = useAuth();
  const endpoint = `/episodes/${episodeId}/storyboards/${storyboardId}/production?limit=50&offset=${offset}`;
  const projectPath = `/projects/${projectId}`;
  const storageKey = `arclight:shot-batch:v1:${user.id}:${projectId}:${episodeId}:${storyboardId}`;
  const [data, setData] = useState<ProductionPage | null>(null);
  const [batch, setBatch] = useState<ShotBatch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [storageBlocked, setStorageBlocked] = useState(false);
  const live = useRef(true);
  const operating = useRef(false);
  const currentBatch = useRef<ShotBatch | null>(null);
  const scope = useRef(0);
  const readSequence = useRef(0);

  const unavailable = useCallback(
    (cause: unknown) => {
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        readSequence.current += 1;
        currentBatch.current = null;
        try {
          sessionStorage.removeItem(storageKey);
          const revisionPrefix = `arclight:shot-revision:v1:${user.id}:${projectId}:${episodeId}:${storyboardId}:`;
          for (const key of Object.keys(sessionStorage)) {
            if (key.startsWith(revisionPrefix)) sessionStorage.removeItem(key);
          }
        } catch {
          // Access is already blocked; no further command can be sent from this view.
        }
        setData(null);
        setBatch(null);
      }
    },
    [storageKey, user.id, projectId, episodeId, storyboardId],
  );
  const load = useCallback(
    async (signal?: AbortSignal) => {
      const sequence = ++readSequence.current;
      try {
        const page = await api<ProductionPage>(endpoint, { signal });
        if (
          page.project_id !== projectId ||
          page.episode_id !== episodeId ||
          page.storyboard_id !== storyboardId
        )
          throw new Error("生产列表与当前精确分镜不一致。");
        if (
          live.current &&
          !signal?.aborted &&
          sequence === readSequence.current
        )
          setData(page);
        return page;
      } catch (cause) {
        if (
          live.current &&
          !signal?.aborted &&
          sequence === readSequence.current
        ) {
          setError(message(cause));
          unavailable(cause);
        }
        throw cause;
      }
    },
    [endpoint, projectId, episodeId, storyboardId, unavailable],
  );

  useEffect(() => {
    live.current = true;
    return () => {
      live.current = false;
      scope.current += 1;
    };
  }, []);
  useEffect(() => {
    scope.current += 1;
    const controller = new AbortController();
    setData(null);
    void load(controller.signal).catch(() => {});
    return () => controller.abort();
  }, [load]);
  useEffect(() => {
    try {
      const saved: unknown = JSON.parse(
        sessionStorage.getItem(storageKey) ?? "null",
      );
      if (saved === null) return;
      const value = saved as ShotBatch;
      if (
        value.schema !== 1 ||
        value.user_id !== user.id ||
        value.project_id !== projectId ||
        value.episode_id !== episodeId ||
        value.storyboard_id !== storyboardId ||
        !Object.hasOwn(batchActions, value.action) ||
        typeof value.comment !== "string" ||
        !Array.isArray(value.items) ||
        !value.items.length ||
        value.items.length > 50 ||
        !value.items.every(
          (item) =>
            isResourceId(item.shot_id) &&
            isResourceId(item.key) &&
            Number.isInteger(item.shot_no) &&
            typeof item.attempted === "boolean" &&
            ["ready", "blocked", "unknown", "accepted", "failed"].includes(
              item.phase,
            ) &&
            (!item.request ||
              (item.request.episode_id === episodeId &&
                item.request.review_target.storyboard_id === storyboardId &&
                item.request.review_target.shot_id === item.shot_id)) &&
            (!item.candidate ||
              (isResourceId(item.candidate.asset_id) &&
                isResourceId(item.candidate.version_id))) &&
            (!item.job || isResourceId(item.job.id)),
        )
      )
        throw new Error(
          "本标签页的批量提交记录损坏。请先核对服务器任务，避免重复提交。",
        );
      currentBatch.current = value;
      setBatch(value);
    } catch (cause) {
      setStorageBlocked(true);
      setError(message(cause));
    }
  }, [storageKey, user.id, projectId, episodeId, storyboardId]);
  const active = data?.shots.some(
    (shot) =>
      shot.job &&
      !["SUCCEEDED", "FAILED", "CANCELLED", "MANUAL_REVIEW"].includes(
        shot.job.status,
      ),
  );
  useEffect(() => {
    if (!active || busy) return;
    const controller = new AbortController();
    const timer = setInterval(() => {
      void load(controller.signal).catch(() => {});
    }, 2500);
    return () => {
      controller.abort();
      clearInterval(timer);
    };
  }, [active, busy, load]);

  function persist(value: ShotBatch) {
    // A durable logical key is required before a request can leave the browser.
    sessionStorage.setItem(storageKey, JSON.stringify(value));
    currentBatch.current = value;
    if (live.current) setBatch(value);
  }

  async function prepare(
    action: BatchAction,
    selected: Set<string>,
    candidates: Record<string, string>,
    config: ImageConfiguration,
    comment: string,
  ) {
    if (operating.current || storageBlocked || !selected.size) return;
    if (currentBatch.current?.items.some((item) => item.phase === "unknown")) {
      setError("仍有结果待确认的提交，请先继续原批次并核对结果。");
      return;
    }
    operating.current = true;
    setBusy(true);
    setError("");
    const stamp = scope.current;
    try {
      if (["approve", "reject"].includes(action) && !comment.trim())
        throw new Error("批准或拒绝前请填写审核意见。");
      const fresh = await load();
      if (!fresh.writable)
        throw new Error("当前分镜只读，请核对权限和归档状态。");
      const items: BatchItem[] = [];
      for (const shotId of selected) {
        if (!live.current || scope.current !== stamp) return;
        const shot = fresh.shots.find((row) => row.id === shotId);
        if (!shot) throw new Error("所选镜头不在当前分镜页，请重新选择。");
        const item: BatchItem = {
          shot_id: shot.id,
          shot_no: shot.shot_no,
          key: crypto.randomUUID(),
          cost: null,
          phase: "ready",
          attempted: false,
          result: "等待确认提交",
        };
        try {
          if (shot.blocker) throw new Error(shot.blocker.message);
          if (action === "generate") {
            if (shot.job)
              throw new Error(
                shot.job.status === "SUCCEEDED"
                  ? "已生成成功，保留原结果"
                  : "已有任务，请查看任务状态或只重试失败项",
              );
            if (!shot.request) throw new Error("镜头暂不具备生成条件。");
            item.request = {
              ...shot.request,
              spec: { ...shot.request.spec, ...config },
            };
            const estimate = await api<{
              estimate: { maximum_cost: string; currency: string } | null;
            }>(
              projectPath + "/generation-costs/estimate",
              json("POST", item.request),
            );
            item.cost = estimate.estimate?.maximum_cost ?? null;
          } else if (action === "retry") {
            if (!shot.job || shot.job.status !== "FAILED")
              throw new Error(
                shot.job?.status === "SUCCEEDED"
                  ? "已生成成功，保留原结果"
                  : "只允许重试明确失败的任务；未知结果须先在任务中心对账",
              );
            item.job = shot.job;
            item.cost = shot.job.maximum_cost;
          } else {
            const candidate = shot.candidates.find(
              (row) => row.id === candidates[shot.id],
            );
            if (!candidate)
              throw new Error("请先为此镜头选择并查看要审核的精确候选。");
            if (
              !(action === "submit"
                ? candidate.can_submit
                : action === "approve"
                  ? candidate.can_approve
                  : candidate.can_reject)
            )
              throw new Error(
                candidate.blocker?.message ??
                  "候选当前状态、权限或锁定不允许此操作。",
              );
            item.candidate = candidate;
            item.cost = "0";
          }
        } catch (cause) {
          if (
            cause instanceof ApiError &&
            [401, 403, 404].includes(cause.status)
          ) {
            unavailable(cause);
            throw cause;
          }
          item.phase = "blocked";
          item.result = message(cause);
        }
        items.push(item);
      }
      if (live.current && scope.current === stamp)
        persist({
          schema: 1,
          user_id: user.id,
          project_id: projectId,
          episode_id: episodeId,
          storyboard_id: storyboardId,
          action,
          comment: comment.trim(),
          items,
        });
    } catch (cause) {
      if (live.current) setError(message(cause));
    } finally {
      operating.current = false;
      if (live.current) setBusy(false);
    }
  }

  async function execute() {
    const original = currentBatch.current;
    if (!original || operating.current || storageBlocked) return;
    operating.current = true;
    setBusy(true);
    setError("");
    let next = structuredClone(original);
    const stamp = scope.current;
    try {
      const fresh = await load();
      if (!fresh.writable)
        throw new Error("当前权限或归档状态不允许继续提交。");
      for (let index = 0; index < next.items.length; index++) {
        if (!live.current || scope.current !== stamp) return;
        let item = next.items[index];
        if (["accepted", "blocked"].includes(item.phase)) continue;
        const wasAttempted = item.attempted;
        item = {
          ...item,
          phase: "unknown",
          attempted: true,
          result: "提交中，等待服务器确认",
        };
        next.items[index] = item;
        persist(next);
        let sent = false;
        try {
          if (next.action === "generate") {
            if (!item.request)
              throw new Error("缺少冻结的生成请求，请重新核对。");
            sent = true;
            await api<{ id: string; status: string }>(
              projectPath + "/generation-jobs",
              {
                ...json("POST", item.request),
                headers: { "Idempotency-Key": item.key },
              },
            );
            item.result = "生成任务已受理";
          } else if (next.action === "retry") {
            if (!item.job) throw new Error("缺少原任务快照，请重新核对。");
            const path = projectPath + `/generation-jobs/${item.job.id}`;
            const state = await api<{
              id: string;
              status: string;
              revision: number;
            }>(path);
            if (state.status === "SUCCEEDED")
              item.result = "已生成成功，保留原任务和结果";
            else if (
              wasAttempted &&
              state.revision !== item.job.revision &&
              state.status !== "FAILED"
            )
              item.result = `原任务状态：${state.status}，保留已有执行；未知结果须对账`;
            else {
              if (
                state.status !== "FAILED" ||
                state.revision !== item.job.revision
              )
                throw new Error(
                  "原任务已变化，请刷新后重新预览失败任务；未知结果须先对账。",
                );
              sent = true;
              const retried = await api<{ status: string }>(
                path + "/retry",
                json("POST", { expected_revision: item.job.revision }),
              );
              if (retried.status !== "RETRY_WAIT")
                throw new ApiError(
                  409,
                  "GENERATION_NOT_RETRYABLE",
                  "原任务未进入重试，请在任务中心查看当前状态和失败原因。",
                );
              item.result = "原失败任务已进入重试";
            }
          } else {
            if (!item.candidate) throw new Error("缺少精确候选，请重新核对。");
            const path =
              projectPath +
              `/assets/${item.candidate.asset_id}/versions/${item.candidate.version_id}`;
            const status =
              next.action === "submit"
                ? "READY_FOR_REVIEW"
                : next.action === "approve"
                  ? "APPROVED"
                  : "REJECTED";
            const state = await api<{ status: string; revision: number }>(path);
            if (
              wasAttempted &&
              state.status === status &&
              state.revision === item.candidate.revision + 1
            )
              item.result = "已确认此精确候选的目标状态，保留原审核记录";
            else {
              sent = true;
              await api(
                path + "/state",
                json("PATCH", {
                  expected_revision: item.candidate.revision,
                  status,
                  comment: next.comment,
                }),
              );
              item.result = "精确候选状态已更新，审核记录已保存";
            }
          }
          item.phase = "accepted";
        } catch (cause) {
          item.phase =
            cause instanceof ApiError && cause.status < 500
              ? "failed"
              : sent || wasAttempted
                ? "unknown"
                : "failed";
          item.result = message(cause);
          if (
            cause instanceof ApiError &&
            [401, 403, 404].includes(cause.status)
          ) {
            unavailable(cause);
            next.items[index] = item;
            persist(next);
            throw cause;
          }
        }
        next.items[index] = item;
        next = structuredClone(next);
        persist(next);
      }
      if (live.current && scope.current === stamp) await load();
    } catch (cause) {
      if (live.current) setError(message(cause));
    } finally {
      operating.current = false;
      if (live.current) setBusy(false);
    }
  }
  async function refresh() {
    if (operating.current) return;
    setError("");
    await load().catch(() => {});
  }
  return {
    data,
    batch,
    busy,
    error,
    storageBlocked,
    prepare,
    execute,
    refresh,
    unavailable,
  };
}
