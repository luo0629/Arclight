import "client-only";

import { ApiError, api, apiBlob, json } from "@/lib/api/client";
import type { Graph } from "@/types/api";
import type { ContinuityComparison } from "@/features/continuity/types";
import type {
  HumanReview,
  LineAudioDecision,
  LineAudioPreparation,
  MachineReport,
  RenderAdmission,
  RenderAttempt,
  RenderDelivery,
  RenderDeliveryDetail,
  RenderDeliveryReport,
  RenderJob,
  RenderManifest,
  RenderProfile,
  ReviewDecision,
  ReviewResume,
  Timeline,
  TimelineCreate,
  TimelinePreparation,
} from "./types";

export const PAGE_SIZE = 30;
const id = encodeURIComponent;
const read = <T>(path: string, signal?: AbortSignal) =>
  api<T>(path, { signal });
const write = <T>(path: string, body?: unknown, signal?: AbortSignal) =>
  api<T>(path, { ...json("POST", body), signal });
const page = (cursor?: string) =>
  `?limit=${PAGE_SIZE}${cursor ? `&before_id=${id(cursor)}` : ""}`;
const deliveryPath = (episodeId: string, artifactId: string) =>
  `/episodes/${id(episodeId)}/deliveries/${id(artifactId)}`;

export const renderingApi = {
  listTimelines: (
    episodeId: string,
    beforeVersion?: number,
    signal?: AbortSignal,
  ) =>
    read<Timeline[]>(
      `/episodes/${id(episodeId)}/timelines?limit=${PAGE_SIZE}${beforeVersion === undefined ? "" : `&before_version=${beforeVersion}`}`,
      signal,
    ),
  getTimeline: (timelineId: string, signal?: AbortSignal) =>
    read<Timeline>(`/timelines/${id(timelineId)}`, signal),
  createTimeline: (
    episodeId: string,
    body: TimelineCreate,
    signal?: AbortSignal,
  ) => write<Timeline>(`/episodes/${id(episodeId)}/timelines`, body, signal),
  listManifests: (
    timelineId: string,
    beforeId?: string,
    signal?: AbortSignal,
  ) =>
    read<RenderManifest[]>(
      `/timelines/${id(timelineId)}/render-manifests${page(beforeId)}`,
      signal,
    ),
  getManifest: (manifestId: string, signal?: AbortSignal) =>
    read<RenderManifest>(`/render-manifests/${id(manifestId)}`, signal),
  createManifest: (
    timelineId: string,
    body: {
      creation_key: string;
      profile: RenderProfile;
      line_audio_decisions?: LineAudioDecision[];
      subtitle_style?: Record<string, unknown>;
      font_file_id?: string;
    },
    signal?: AbortSignal,
  ) =>
    write<RenderManifest>(
      `/timelines/${id(timelineId)}/render-manifests`,
      body,
      signal,
    ),
  exportReview: (manifestId: string, signal?: AbortSignal) =>
    write<HumanReview>(
      `/render-manifests/${id(manifestId)}/export-review`,
      undefined,
      signal,
    ),
  getAdmission: (manifestId: string, signal?: AbortSignal) =>
    write<RenderAdmission>(
      `/render-manifests/${id(manifestId)}/admission`,
      undefined,
      signal,
    ),
  listJobs: (manifestId: string, beforeId?: string, signal?: AbortSignal) =>
    read<RenderJob[]>(
      `/render-manifests/${id(manifestId)}/jobs${page(beforeId)}`,
      signal,
    ),
  getJob: (jobId: string, signal?: AbortSignal) =>
    read<RenderJob>(`/render-jobs/${id(jobId)}`, signal),
  listAttempts: (jobId: string, signal?: AbortSignal) =>
    read<RenderAttempt[]>(`/render-jobs/${id(jobId)}/attempts`, signal),
  createJob: (
    manifestId: string,
    body: { creation_key: string; retry_of?: string },
    signal?: AbortSignal,
  ) =>
    write<RenderJob>(`/render-manifests/${id(manifestId)}/jobs`, body, signal),
  cancelJob: (jobId: string, expectedRevision: number, signal?: AbortSignal) =>
    write<RenderJob>(
      `/render-jobs/${id(jobId)}/cancel`,
      { expected_revision: expectedRevision },
      signal,
    ),
  listDeliveries: (
    episodeId: string,
    beforeId?: string,
    signal?: AbortSignal,
  ) =>
    read<RenderDelivery[]>(
      `/episodes/${id(episodeId)}/deliveries${page(beforeId)}`,
      signal,
    ),
  getDelivery: (episodeId: string, artifactId: string, signal?: AbortSignal) =>
    read<RenderDeliveryDetail>(deliveryPath(episodeId, artifactId), signal),
  getReport: (episodeId: string, artifactId: string, signal?: AbortSignal) =>
    read<RenderDeliveryReport>(
      `${deliveryPath(episodeId, artifactId)}/report`,
      signal,
    ),
  requestWarningReview: (
    episodeId: string,
    artifactId: string,
    signal?: AbortSignal,
  ) =>
    write<HumanReview>(
      `${deliveryPath(episodeId, artifactId)}/warning-review`,
      undefined,
      signal,
    ),
  getHumanReview: (reviewId: string, signal?: AbortSignal) =>
    read<HumanReview>(`/human-reviews/${id(reviewId)}`, signal),
  decideReview: (
    review: HumanReview,
    decision: "APPROVE" | "REJECT",
    comment: string,
    signal?: AbortSignal,
  ) =>
    write<ReviewDecision>(
      `/human-reviews/${id(review.id)}/decisions`,
      {
        version_id: review.snapshot.version_id,
        request_sha256: review.request_sha256,
        decision,
        comment: comment.trim(),
      },
      signal,
    ),
  resumeReview: (review: HumanReview, signal?: AbortSignal) => {
    if (!review.decision)
      return Promise.reject(
        new ApiError(409, "DECISION_REQUIRED", "请先保存本次审核决定"),
      );
    return write<ReviewResume>(
      `/human-reviews/${id(review.id)}/resume`,
      {
        decision_id: review.decision.id,
        request_sha256: review.request_sha256,
      },
      signal,
    );
  },
};

function record(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// Legacy report envelopes are readable history, never machine acceptance proof.
export function asMachineReport(value: unknown): MachineReport | null {
  if (
    !record(value) ||
    value.schema_version !== "render-report/1" ||
    value.checker !== "arclight-delivery-v1" ||
    value.render_status !== "passed" ||
    value.profile !== "FORMAL" ||
    !record(value.checks) ||
    !record(value.checks.technical) ||
    !record(value.output) ||
    !record(value.software) ||
    !Array.isArray(value.sources) ||
    !Array.isArray(value.warnings)
  )
    return null;
  const checks = value.checks;
  const technical = value.checks.technical;
  const hash = (item: unknown) =>
    typeof item === "string" && /^[0-9a-f]{64}$/.test(item);
  if (
    ![
      "job_id",
      "attempt_id",
      "artifact_id",
      "asset_version_id",
      "manifest_id",
      "timeline_id",
      "inspection_id",
      "output_file_id",
    ].every((key) => typeof value[key] === "string" && value[key] !== "") ||
    ![
      "manifest_sha256",
      "timeline_sha256",
      "input_sha256",
      "output_sha256",
    ].every((key) => hash(value[key])) ||
    !["byte_size", "duration_ms", "timeline_version"].every(
      (key) => Number.isSafeInteger(value[key]) && Number(value[key]) > 0,
    ) ||
    !["video_decodable", "audio_stream_present", "subtitle_range_valid"].every(
      (key) => checks[key] === true,
    ) ||
    ![
      "missing_required_shots",
      "missing_audio_lines",
      "stale_dependencies",
    ].every((key) => checks[key] === 0) ||
    technical.passed !== true ||
    technical.output_sha256 !== value.output_sha256 ||
    technical.byte_size !== value.byte_size ||
    value.output.name !== "FORMAL" ||
    !value.sources.every(
      (source) =>
        record(source) &&
        typeof source.asset_id === "string" &&
        typeof source.asset_version_id === "string" &&
        typeof source.asset_type === "string" &&
        hash(source.sha256),
    ) ||
    !value.warnings.every(
      (warning) =>
        record(warning) &&
        hash(warning.id) &&
        typeof warning.reason === "string" &&
        ["EXPLICIT_SILENCE", "SUBTITLE_ONLY", "MEDIA_EXCEPTION"].includes(
          String(warning.code),
        ),
    )
  )
    return null;
  return value as MachineReport;
}

type Shot = {
  id: string;
  shot_no: number;
  description: string;
  storyboard_id: string;
  content_json?: {
    quality_review?: { review_id: string } | null;
    selected_visual_asset?: {
      asset_id: string;
      asset_version_id: string;
    } | null;
  };
};

/** Resolve dialogue from the timeline's exact storyboard, never the latest script. */
export async function prepareLineAudioDecisions(
  episodeId: string,
  timeline: Timeline,
  signal?: AbortSignal,
): Promise<LineAudioPreparation> {
  if (timeline.snapshot.episode_id !== episodeId)
    throw new ApiError(409, "RENDER_SOURCE_MISMATCH", "时间轴不属于当前剧集");
  type SourceBoard = {
    id: string;
    episode_id: string;
    script_version_id: string;
  };
  let source: SourceBoard | undefined;
  const boardIds = new Set<string>();
  for (let offset = 0; !source; offset += 100) {
    signal?.throwIfAborted();
    const boards = await read<SourceBoard[]>(
      `/episodes/${id(episodeId)}/storyboards?limit=100&offset=${offset}`,
      signal,
    );
    source = boards.find(
      (board) => board.id === timeline.snapshot.storyboard_id,
    );
    const repeated =
      boards.length > 0 && boards.every((board) => boardIds.has(board.id));
    boards.forEach((board) => boardIds.add(board.id));
    if (!source && (boards.length < 100 || repeated))
      throw new ApiError(
        409,
        "RENDER_SOURCE_MISSING",
        "未找到时间轴的精确来源分镜，请刷新并核对来源",
      );
  }
  if (source.episode_id !== episodeId || !source.script_version_id)
    throw new ApiError(
      409,
      "RENDER_SOURCE_MISMATCH",
      "分镜来源与当前剧集不一致",
    );
  const scriptId = source.script_version_id;
  const voiceLines = new Set(
    timeline.snapshot.tracks
      .filter((track) => track.kind === "VOICE")
      .flatMap((track) => track.clips)
      .filter((clip) => clip.script_version_id === scriptId && clip.line_id)
      .map((clip) => clip.line_id),
  );
  const subtitleLines = new Set(
    timeline.snapshot.tracks
      .filter((track) => track.kind === "SUBTITLE")
      .flatMap((track) => track.clips)
      .filter((clip) => clip.script_version_id === scriptId && clip.line_id)
      .map((clip) => clip.line_id),
  );
  const prepared: LineAudioPreparation = {
    script_version_id: scriptId,
    voiced_count: 0,
    lines: [],
  };
  const lineIds = new Set<string>();
  type SourceLine = {
    script_version_id: string;
    line_id: string;
    text: string;
  };
  for (let offset = 0; ; offset += 100) {
    signal?.throwIfAborted();
    const lines = await read<SourceLine[]>(
      `/episodes/${id(episodeId)}/dialogue-lines?script_version_id=${id(scriptId)}&limit=100&offset=${offset}`,
      signal,
    );
    for (const line of lines) {
      if (
        line.script_version_id !== scriptId ||
        !line.line_id ||
        lineIds.has(line.line_id) ||
        lineIds.size >= 5000
      )
        throw new ApiError(
          409,
          "RENDER_LINES_INVALID",
          "来源台词清单不完整或不属于精确剧本，请刷新后重试",
        );
      lineIds.add(line.line_id);
      if (voiceLines.has(line.line_id)) prepared.voiced_count += 1;
      else
        prepared.lines.push({
          line_id: line.line_id,
          text: line.text,
          subtitle_available: subtitleLines.has(line.line_id),
        });
    }
    if (lines.length < 100) break;
  }
  return prepared;
}

export async function prepareTimelineSelection(
  projectId: string,
  episodeId: string,
  storyboardId: string,
  signal?: AbortSignal,
): Promise<TimelinePreparation> {
  const [shots, graph] = await Promise.all([
    read<Shot[]>(
      `/episodes/${id(episodeId)}/shots?storyboard_id=${id(storyboardId)}&limit=100&offset=0`,
      signal,
    ),
    read<Graph>(`/episodes/${id(episodeId)}/canvas`, signal),
  ]);
  if (
    !shots.length ||
    shots.length > 63 ||
    shots.some((shot) => shot.storyboard_id !== storyboardId)
  )
    throw new ApiError(
      409,
      "TIMELINE_SHOTS_INVALID",
      "默认时间轴需要该分镜的完整镜头清单（1–63个镜头）",
    );
  const result: TimelinePreparation = {
    storyboard_id: storyboardId,
    selected_shots: [],
    shots: [...shots]
      .sort((a, b) => a.shot_no - b.shot_no)
      .map((shot) => ({
        shot_id: shot.id,
        shot_no: shot.shot_no,
        description: shot.description,
        options: [],
      })),
    blocking_issues: [],
  };
  const versions = graph.nodes.filter(
    (node) =>
      node.kind === "asset_version" &&
      node.current === true &&
      !node.stale &&
      ["IMAGE", "SHOT_IMAGE"].includes(node.asset_type ?? "") &&
      ["APPROVED", "LOCKED"].includes(node.status) &&
      node.asset_id,
  );
  // Read existing exact selections only; bounded batches avoid a request storm.
  for (let start = 0; start < versions.length; start += 4) {
    signal?.throwIfAborted();
    const comparisons = await Promise.all(
      versions.slice(start, start + 4).map(async (node) => {
        try {
          return await read<ContinuityComparison>(
            `/projects/${id(projectId)}/assets/${id(node.asset_id!)}/versions/${id(node.resource_id ?? node.id)}/continuity?limit=1`,
            signal,
          );
        } catch (error) {
          if (
            error instanceof ApiError &&
            [400, 404, 409, 422].includes(error.status)
          )
            return null;
          throw error;
        }
      }),
    );
    for (const comparison of comparisons) {
      if (
        !comparison ||
        comparison.actions.blocker !== null ||
        comparison.version.stale ||
        !["APPROVED", "LOCKED"].includes(comparison.version.status) ||
        ["ARCHIVED", "STALE", "REJECTED"].includes(comparison.asset.status) ||
        comparison.asset.current_version_id !== comparison.version.id
      )
        continue;
      const review = comparison.reviews[0]?.record.review;
      const target = review?.target;
      if (!target || target.asset_version_id !== comparison.version.id)
        continue;
      for (const currentShot of shots) {
        const direct =
          target.storyboard_id === storyboardId &&
          target.shot_id === currentShot.id;
        const inherited =
          currentShot.content_json?.quality_review?.review_id ===
          review?.result_id;
        const saved = currentShot.content_json?.selected_visual_asset;
        if (
          (!direct && !inherited) ||
          (saved &&
            (saved.asset_id !== comparison.asset.id ||
              saved.asset_version_id !== comparison.version.id))
        )
          continue;
        const shot = result.shots.find(
          (row) => row.shot_id === currentShot.id,
        )!;
        if (
          shot.options.some(
            (option) => option.asset_version_id === comparison.version.id,
          )
        )
          continue;
        // Inherited reviews preserve their original shot identity; creation rechecks intent.
        shot.options.push({
          asset_id: comparison.asset.id,
          asset_version_id: comparison.version.id,
          version_no: comparison.version.version_no,
          description: comparison.asset.name,
        });
      }
    }
  }
  for (const shot of result.shots) {
    if (shot.options.length === 1)
      result.selected_shots.push({
        shot_id: shot.shot_id,
        asset_version_id: shot.options[0].asset_version_id,
      });
    else if (shot.options.length === 0)
      result.blocking_issues.push(
        `镜头 ${shot.shot_no} 缺少当前已选用且审查通过的图片，请先完成素材审核与选用`,
      );
  }
  return result;
}

async function verifiedBlob(
  blob: Blob,
  delivery: RenderDelivery,
  signal: AbortSignal,
) {
  signal.throwIfAborted();
  if (blob.size !== delivery.byte_size || blob.size > 128 * 1024 ** 2)
    throw new ApiError(
      409,
      "DELIVERY_BYTES_MISMATCH",
      "成片大小与精确版本不一致，请刷新后重试",
    );
  if (!globalThis.crypto?.subtle)
    throw new ApiError(
      409,
      "DELIVERY_VERIFICATION_UNAVAILABLE",
      "当前浏览器环境无法校验成片，请通过 HTTPS 或本机地址访问后重试",
    );
  const digest = await crypto.subtle.digest(
    "SHA-256",
    await blob.arrayBuffer(),
  );
  signal.throwIfAborted();
  const sha256 = Array.from(new Uint8Array(digest), (value) =>
    value.toString(16).padStart(2, "0"),
  ).join("");
  if (sha256 !== delivery.sha256)
    throw new ApiError(
      409,
      "DELIVERY_HASH_MISMATCH",
      "成片校验未通过，请刷新后重试",
    );
  return blob.type === "video/mp4"
    ? blob
    : new Blob([blob], { type: "video/mp4" });
}

// Both preview and download obtain fresh authorization. Tickets never enter URL state.
export async function loadDeliveryBlob(
  projectId: string,
  episodeId: string,
  delivery: RenderDelivery,
  signal: AbortSignal,
): Promise<Blob> {
  if (
    delivery.project_id !== projectId ||
    delivery.episode_id !== episodeId ||
    !Number.isSafeInteger(delivery.byte_size) ||
    delivery.byte_size <= 0 ||
    delivery.byte_size > 128 * 1024 ** 2
  )
    throw new ApiError(
      409,
      "DELIVERY_SCOPE_INVALID",
      "成片版本不属于当前剧集或大小无效，请刷新后重试",
    );
  if (delivery.storage_backend === "LOCAL")
    return verifiedBlob(
      await apiBlob(`${deliveryPath(episodeId, delivery.id)}/content`, signal),
      delivery,
      signal,
    );
  const grant = await write<{
    file_id: string;
    method: "GET";
    url: string;
    expires_at: string;
    checksum: string;
    byte_size: number;
  }>(
    `/projects/${id(projectId)}/files/${id(delivery.file_id)}/download-url`,
    { expires_in: 120 },
    signal,
  );
  if (
    grant.file_id !== delivery.file_id ||
    grant.method !== "GET" ||
    grant.checksum !== delivery.sha256 ||
    grant.byte_size !== delivery.byte_size ||
    !Number.isFinite(Date.parse(grant.expires_at)) ||
    Date.parse(grant.expires_at) <= Date.now()
  )
    throw new ApiError(
      409,
      "DELIVERY_GRANT_MISMATCH",
      "下载授权与当前成片版本不一致，请重试",
    );
  const url = new URL(grant.url, window.location.origin);
  if (
    !["http:", "https:"].includes(url.protocol) ||
    url.username ||
    url.password ||
    url.hash ||
    !/^\/file-transfers\/[^/]+$/.test(url.pathname)
  )
    throw new ApiError(
      409,
      "DELIVERY_GRANT_INVALID",
      "下载地址无效，请重新申请授权",
    );
  const response = await fetch(url, {
    signal,
    credentials: "omit",
    cache: "no-store",
    redirect: "error",
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    throw new ApiError(
      response.status,
      body.error?.code ?? "DELIVERY_UNAVAILABLE",
      body.error?.message ?? "成片暂时无法读取，请重新申请授权",
      body.error?.request_id ?? "",
    );
  }
  if (!response.body)
    throw new ApiError(409, "DELIVERY_EMPTY", "成片响应为空，请重试");
  const reader = response.body.getReader();
  const chunks: ArrayBuffer[] = [];
  let bytes = 0;
  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      bytes += value.byteLength;
      if (bytes > delivery.byte_size)
        throw new ApiError(
          409,
          "DELIVERY_BYTES_MISMATCH",
          "成片大小与精确版本不一致，请刷新后重试",
        );
      chunks.push(new Uint8Array(value).buffer);
    }
  } finally {
    await reader.cancel().catch(() => {});
    reader.releaseLock();
  }
  return verifiedBlob(
    new Blob(chunks, { type: "video/mp4" }),
    delivery,
    signal,
  );
}
