"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useAuth } from "@/features/auth/auth-context";
import { api, ApiError, json, message } from "@/lib/api/client";
import { isResourceId } from "@/lib/routes";
import type { AssetVersion, ReferenceImage } from "@/types/api";
import type { VideoCandidate } from "../types";

export type VideoConfiguration = {
  enabled: boolean;
  provider: string;
  model: string;
  configuration_version: string;
  message?: string;
  capabilities: {
    task_types: string[];
    max_reference_images: number;
    min_width: number;
    min_height: number;
    max_width: number;
    max_height: number;
    max_pixels: number;
    dimension_multiple: number;
    sizes: { width: number; height: number }[];
    aspect_ratios: { width: number; height: number }[];
    max_video_seconds: number | null;
  };
};
type Request = {
  episode_id: string;
  target_type: "VIDEO";
  task_type: "VIDEO";
  review_target: { storyboard_id: string; shot_id: string };
  inputs: {
    source_asset_version_id: string;
    dependency_type: string;
    selectors?: string[];
  }[];
  spec: {
    provider: string;
    model: string;
    model_config_version: string;
    prompt: string;
    prompt_version: string;
    parameters: Record<string, never>;
    output_spec: {
      width: number;
      height: number;
      format: "mp4";
      duration_seconds: number;
    };
  };
};
type Pending = {
  schema: 1;
  user_id: string;
  project_id: string;
  key: string;
  request: Request;
  maximum_cost: string | null;
  phase: "PREPARED" | "UNKNOWN" | "ACCEPTED";
  job_id: string | null;
};
type Job = {
  id: string;
  project_id: string;
  episode_id: string;
  task_type: string;
  target_type: string;
  status: string;
};

export default function useVideoGeneration({
  projectId,
  episodeId,
  storyboardId,
  shotId,
  onChanged,
  unavailable,
}: {
  projectId: string;
  episodeId: string;
  storyboardId: string;
  shotId: string;
  onChanged: () => Promise<void>;
  unavailable: (cause: unknown) => void;
}) {
  const { user } = useAuth();
  const storageKey = `arclight:video-generation:v1:${user.id}:${projectId}:${episodeId}:${storyboardId}:${shotId}`;
  const root = `/projects/${projectId}`,
    jobs = root + "/generation-jobs";
  const [configuration, setConfiguration] = useState<VideoConfiguration | null>(
    null,
  );
  const [frames, setFrames] = useState<ReferenceImage[]>([]);
  const [hasMore, setHasMore] = useState(true);
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [blocked, setBlocked] = useState(false);
  const [jobStatus, setJobStatus] = useState("");
  const live = useRef(false),
    operating = useRef(false),
    offset = useRef(0);
  const active = useRef<AbortController | null>(null);
  const changed = useRef(onChanged);
  changed.current = onChanged;
  const fail = useCallback(
    (cause: unknown) => {
      setError(message(cause));
      if (cause instanceof ApiError && [401, 403, 404].includes(cause.status)) {
        setBlocked(true);
        setFrames([]);
        setConfiguration(null);
        setPending(null);
        unavailable(cause);
      }
    },
    [unavailable],
  );
  const persist = (row: Pending | null) => {
    if (row) sessionStorage.setItem(storageKey, JSON.stringify(row));
    else sessionStorage.removeItem(storageKey);
    if (live.current) setPending(row);
  };
  const load = useCallback(
    async (signal: AbortSignal, append = false) => {
      const start = append ? offset.current : 0;
      const [config, rows] = await Promise.all([
        api<VideoConfiguration>(jobs + "/services/video/configuration", {
          signal,
        }),
        api<ReferenceImage[]>(
          root + `/reference-images?limit=50&offset=${start}`,
          { signal },
        ),
      ]);
      if (!live.current || signal.aborted) return;
      setConfiguration(config);
      setFrames((previous) =>
        append
          ? [
              ...previous,
              ...rows.filter(
                (row) => !previous.some((item) => item.id === row.id),
              ),
            ]
          : rows,
      );
      offset.current = start + rows.length;
      setHasMore(rows.length === 50);
    },
    [jobs, root],
  );
  useEffect(() => {
    live.current = true;
    const controller = new AbortController();
    active.current = controller;
    setBusy(true);
    try {
      const saved = sessionStorage.getItem(storageKey);
      if (saved) {
        const row = JSON.parse(saved) as Pending;
        if (
          row.schema !== 1 ||
          row.user_id !== user.id ||
          row.project_id !== projectId ||
          !isResourceId(row.key) ||
          row.request?.episode_id !== episodeId ||
          row.request.target_type !== "VIDEO" ||
          row.request.task_type !== "VIDEO" ||
          row.request.review_target.storyboard_id !== storyboardId ||
          row.request.review_target.shot_id !== shotId ||
          !["PREPARED", "UNKNOWN", "ACCEPTED"].includes(row.phase) ||
          (row.maximum_cost !== null &&
            !/^\d+(\.\d{1,6})?$/.test(row.maximum_cost)) ||
          (row.phase === "ACCEPTED"
            ? !isResourceId(row.job_id ?? "")
            : row.job_id !== null)
        )
          throw new Error("视频生成提交记录无效，请先核对服务器任务。");
        setPending(row);
      }
    } catch (cause) {
      setBlocked(true);
      fail(cause);
    }
    void load(controller.signal)
      .catch((cause) => {
        if (!controller.signal.aborted && live.current) fail(cause);
      })
      .finally(() => {
        if (!controller.signal.aborted && live.current) setBusy(false);
      });
    return () => {
      live.current = false;
      controller.abort();
      active.current?.abort();
    };
  }, [
    storageKey,
    user.id,
    projectId,
    episodeId,
    storyboardId,
    shotId,
    load,
    fail,
  ]);
  async function run<T>(command: (signal: AbortSignal) => Promise<T>) {
    if (operating.current || blocked) return null;
    operating.current = true;
    setBusy(true);
    setError("");
    const controller = new AbortController();
    active.current = controller;
    try {
      return await command(controller.signal);
    } catch (cause) {
      if (live.current && !controller.signal.aborted) fail(cause);
      return null;
    } finally {
      operating.current = false;
      if (live.current && !controller.signal.aborted) setBusy(false);
    }
  }
  async function prepare({
    frameId,
    prompt,
    width,
    height,
    seconds,
  }: {
    frameId: string;
    prompt: string;
    width: number;
    height: number;
    seconds: number;
  }) {
    if (pending || !configuration?.enabled) return;
    await run(async (signal) => {
      const caps = configuration.capabilities;
      if (
        !caps?.task_types.includes("VIDEO") ||
        caps.max_reference_images < 1 ||
        ![width, height].every(Number.isSafeInteger) ||
        width < caps.min_width ||
        height < caps.min_height ||
        width > caps.max_width ||
        height > caps.max_height ||
        width * height > caps.max_pixels ||
        width % caps.dimension_multiple ||
        height % caps.dimension_multiple ||
        (caps.sizes.length > 0 &&
          !caps.sizes.some(
            (size) => size.width === width && size.height === height,
          )) ||
        (caps.aspect_ratios.length > 0 &&
          !caps.aspect_ratios.some(
            (ratio) => width * ratio.height === height * ratio.width,
          )) ||
        !Number.isFinite(seconds) ||
        seconds <= 0 ||
        !caps.max_video_seconds ||
        seconds > caps.max_video_seconds
      )
        throw new Error("尺寸、时长或首帧输入超出当前模型能力。");
      const frame = frames.find(
        (row) =>
          row.id === frameId &&
          !row.stale &&
          ["APPROVED", "LOCKED"].includes(row.status),
      );
      if (!frame || !prompt.trim())
        throw new Error("请选择已批准的精确首帧，并填写视频提示词。");
      const version = await api<AssetVersion>(
        root + `/assets/${frame.asset_id}/versions/${frame.id}`,
        { signal },
      );
      if (
        version.id !== frame.id ||
        version.asset_id !== frame.asset_id ||
        version.stale ||
        !["APPROVED", "LOCKED"].includes(version.status) ||
        !version.media ||
        !/^image\//.test(version.media.media_type)
      )
        throw new Error("首帧版本已变化或没有真实图片，请刷新后核对。");
      const request: Request = {
        episode_id: episodeId,
        target_type: "VIDEO",
        task_type: "VIDEO",
        review_target: { storyboard_id: storyboardId, shot_id: shotId },
        inputs: [
          {
            source_asset_version_id: storyboardId,
            dependency_type: "CONTENT_DEPENDENCY",
            selectors: ["shot:" + shotId],
          },
          {
            source_asset_version_id: frame.id,
            dependency_type: "REFERENCE_DEPENDENCY",
          },
        ],
        spec: {
          provider: configuration.provider,
          model: configuration.model,
          model_config_version: configuration.configuration_version,
          prompt: prompt.trim(),
          prompt_version: "shot-video-ui/1",
          parameters: {},
          output_spec: {
            width,
            height,
            format: "mp4",
            duration_seconds: seconds,
          },
        },
      };
      const estimate = await api<{
        estimate: { maximum_cost: string; currency: string } | null;
      }>(root + "/generation-costs/estimate", {
        ...json("POST", request),
        signal,
      });
      if (!live.current || signal.aborted) return;
      persist({
        schema: 1,
        user_id: user.id,
        project_id: projectId,
        key: crypto.randomUUID(),
        request,
        maximum_cost: estimate.estimate?.maximum_cost ?? null,
        phase: "PREPARED",
        job_id: null,
      });
    });
  }
  async function submit() {
    if (!pending || pending.phase === "ACCEPTED") return;
    await run(async (signal) => {
      const frozen = { ...pending, phase: "UNKNOWN" as const };
      persist(frozen);
      const job = await api<Job>(jobs, {
        ...json("POST", frozen.request),
        headers: { "Idempotency-Key": frozen.key },
        signal,
      });
      if (
        !isResourceId(job.id) ||
        job.project_id !== projectId ||
        job.episode_id !== episodeId ||
        job.task_type !== "VIDEO" ||
        job.target_type !== "VIDEO"
      )
        throw new Error("视频生成回执与当前精确镜头不一致，请核验原提交。");
      if (!live.current || signal.aborted) return;
      persist({ ...frozen, phase: "ACCEPTED", job_id: job.id });
      setJobStatus(job.status);
    });
  }
  const jobId = pending?.phase === "ACCEPTED" ? pending.job_id : null;
  useEffect(() => {
    if (!jobId || blocked) return;
    const controller = new AbortController();
    let timer: ReturnType<typeof setTimeout> | undefined;
    const check = async () => {
      try {
        const [job, candidates] = await Promise.all([
          api<Job>(jobs + "/" + jobId, { signal: controller.signal }),
          api<VideoCandidate[]>(jobs + "/" + jobId + "/candidates", {
            signal: controller.signal,
          }),
        ]);
        if (!live.current || controller.signal.aborted) return;
        if (
          job.id !== jobId ||
          job.project_id !== projectId ||
          job.episode_id !== episodeId ||
          candidates.some(
            (row) => row.project_id !== projectId || row.job_id !== jobId,
          )
        )
          throw new Error("视频生产结果不属于当前精确任务。");
        setJobStatus(job.status);
        if (candidates.some((row) => row.asset_version_id))
          await changed.current();
        else if (
          candidates.some(
            (row) => row.error_code || row.media_check?.status === "FAILED",
          )
        )
          setError(
            "视频结果未通过媒体检查，未登记为可用候选。请在媒体任务中心核对原任务和文件，再决定是否重新生成。",
          );
        else if (!["FAILED", "CANCELLED", "MANUAL_REVIEW"].includes(job.status))
          timer = setTimeout(() => void check(), 2500);
      } catch (cause) {
        if (live.current && !controller.signal.aborted) fail(cause);
      }
    };
    void check();
    return () => {
      controller.abort();
      if (timer) clearTimeout(timer);
    };
  }, [jobId, blocked, jobs, projectId, episodeId, fail]);
  return {
    configuration,
    frames,
    hasMore,
    pending,
    busy,
    blocked,
    error,
    jobStatus,
    prepare,
    submit,
    loadMore: () => run((signal) => load(signal, true)),
    refresh: () => run((signal) => load(signal)),
    reset: () => {
      if (
        !busy &&
        (pending?.phase === "PREPARED" ||
          ["SUCCEEDED", "FAILED", "CANCELLED"].includes(jobStatus))
      ) {
        persist(null);
        setJobStatus("");
      }
    },
  };
}
