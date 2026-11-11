import { expect, type Page, type Route } from "@playwright/test";
import type { Episode, Project } from "../src/types/api";
import type { MediaJob, MediaStatus } from "../src/features/tasks/media-types";
import { closeDirectory } from "./workspace-helpers";

const now = "2026-11-11T07:00:00Z";
const id = () => crypto.randomUUID();
export const secret = "UI05_PRIVATE_SNAPSHOT_DO_NOT_DISPLAY";
type Mutation = {
  command: string;
  jobId: string;
  body: Record<string, unknown>;
  key: string | undefined;
};
type RawAttempt = {
  id: string;
  job_id: string;
  attempt_no: number;
  input_manifest_id: string;
  provider: string;
  model: string;
  status: MediaStatus;
  error_code: string | null;
  error_category: string | null;
  latency_ms: number | null;
  submitted_at: string;
  finished_at: string | null;
  input_snapshot_json: Record<string, unknown>;
  result_json: Record<string, unknown> | null;
};
type ReadHandler = (route: Route, job: MediaJob) => Promise<boolean>;

export function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}

// Authentication, project and episode are real. Only generation/queue/cost
// receipts are controlled here; these tests prove browser contracts, not a
// paid provider invocation, backend concurrency or a completed media asset.
export async function mediaFixture(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `ui05-${id()}@example.com`,
      password: "media-task-browser-contract",
      display_name: "媒体任务验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "媒体任务与安全恢复" },
  });
  expect(created.status()).toBe(201);
  const project: Project = await created.json();
  const episodes: Episode[] = [];
  for (let episode_no = 1; episode_no <= 2; episode_no++) {
    const response = await page.request.post(
      `/api/v1/projects/${project.id}/episodes`,
      { headers, data: { episode_no, title: `媒体任务第${episode_no}集` } },
    );
    expect(response.status()).toBe(201);
    episodes.push(await response.json());
  }
  const createJob = (
    status: MediaStatus = "RUNNING",
    episodeId: string | null = episodes[0].id,
  ): MediaJob => ({
    id: id(),
    project_id: project.id,
    episode_id: episodeId,
    target_type: "SHOT_IMAGE",
    target_asset_id: id(),
    input_manifest_id: id(),
    task_type: "IMAGE",
    provider: "historical-provider",
    model: "frozen-image-model-v7",
    status,
    status_label: `服务端状态 ${status}`,
    revision: 7,
    attempt_count: 1,
    cancel_requested_at: null,
    created_at: now,
    updated_at: now,
  });
  const createAttempt = (job: MediaJob, attemptNo = 1): RawAttempt => ({
    id: id(),
    job_id: job.id,
    attempt_no: attemptNo,
    input_manifest_id: job.input_manifest_id,
    provider: `attempt-provider-${attemptNo}`,
    model: `attempt-model-${attemptNo}`,
    status: job.status,
    error_code: job.status === "FAILED" ? "PROVIDER_UNAVAILABLE" : null,
    error_category: job.status === "FAILED" ? "SERVICE" : null,
    latency_ms: job.status === "FAILED" ? 2345 : null,
    submitted_at: now,
    finished_at: ["FAILED", "CANCELLED", "SUCCEEDED"].includes(job.status)
      ? "2026-11-11T07:00:03Z"
      : null,
    input_snapshot_json: {
      context: { generation: { batch_size: 1, prompt: secret } },
      api_key: secret,
    },
    result_json: {
      output_handles:
        job.status === "SUCCEEDED" ? ["artifact:safe-output"] : [],
      metadata: { provider_debug: secret },
    },
  });
  const jobs: MediaJob[] = [createJob()];
  const attempts = new Map<string, RawAttempt[]>([
    [jobs[0].id, [createAttempt(jobs[0])]],
  ]);
  const calls = new Map<string, Record<string, unknown>[]>();
  const costEntries = new Map<string, Record<string, unknown>[]>();
  const deadLetters = new Map<string, Record<string, unknown>>();
  const schedules = new Map<string, Record<string, unknown>>();
  const mutations: Mutation[] = [];
  const reads: string[] = [];
  const replayed = new Map<string, MediaJob>();
  const control: {
    role: "owner" | "editor" | "viewer";
    archived: boolean;
    listStatus?: number;
    detailStatus?: number;
    costStatus?: number;
    attemptStatus?: number;
    queueStatus?: number;
    retryUnchanged?: boolean;
    mutationStatus?: number;
    mutationGate?: Promise<void>;
    replayDropOnce?: boolean;
    jobRead?: ReadHandler;
  } = { role: "owner", archived: false };
  const totals = {
    estimated_cost: "0.250000" as string | null,
    actual_cost: null as string | null,
    pending_estimate: "0.250000",
    projected_cost: null as string | null,
    entry_count: 1,
    confirmed_count: 0,
    estimated_count: 1,
    unpriced_count: 1,
    cost_is_estimate: true,
  };
  const queue = {
    observed_at: now,
    scheduler_heartbeat_at: now,
    scheduler_stale: false,
    count_unit: "business_jobs",
    broker_message_count: null,
    policy_version: 1,
    lanes: ["INTERACTIVE", "BATCH"].map((lane, index) => ({
      lane,
      ready: 2 + index,
      backoff: 1,
      waiting_permit: 1,
      executing: 1,
      unknown: 1,
      terminal: 0,
      dead_letter: 1,
      oldest_waiting_at: now,
      oldest_wait_seconds: 83,
      wait_seconds: { p50: 12, p95: 75 },
      blocked_reasons: { BACKOFF: 1 },
    })),
  };
  const base = `/api/v1/projects/${project.id}/generation-jobs`;
  const costsBase = `/api/v1/projects/${project.id}/costs`;
  const fail = (route: Route, status: number, code: string, message: string) =>
    route.fulfill({
      status,
      json: { error: { code, message, request_id: "ui05-browser-request" } },
    });
  const slice = <T>(items: T[], url: URL, defaultLimit = 20) => {
    const offset = Number(url.searchParams.get("offset") ?? 0);
    return items.slice(
      offset,
      offset + Number(url.searchParams.get("limit") ?? defaultLimit),
    );
  };

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (path === `/api/v1/projects/${project.id}/access`)
      return route.fulfill({ json: { role: control.role } });
    if (
      control.archived &&
      method === "GET" &&
      (path === `/api/v1/projects/${project.id}` ||
        path === `/api/v1/projects/${project.id}/episodes` ||
        episodes.some((episode) => path === `/api/v1/episodes/${episode.id}`))
    ) {
      const response = await route.fetch();
      const original = await response.json();
      const archive = (row: Record<string, unknown>) => ({
        ...row,
        status: "ARCHIVED",
        archived: true,
      });
      return route.fulfill({
        response,
        json: Array.isArray(original)
          ? original.map(archive)
          : archive(original),
      });
    }
    if (path === costsBase || path === costsBase + "/entries") {
      reads.push(request.url());
      if (control.costStatus)
        return fail(
          route,
          control.costStatus,
          "COSTS_UNAVAILABLE",
          "费用记录暂不可读",
        );
      const jobId = url.searchParams.get("task_id")!;
      if (path === costsBase)
        return route.fulfill({
          json: {
            summary: totals,
            groups: [],
            total_groups: 0,
            generated_at: now,
          },
        });
      const items = costEntries.get(jobId) ?? [
        {
          id: id(),
          source: "MEDIA",
          task_id: jobId,
          task_type: "IMAGE",
          provider: "billed-provider",
          model: "billed-model",
          attempt_no: 1,
          attempt_id: attempts.get(jobId)?.[0]?.id ?? null,
          call_id: null,
          input_manifest_id: jobs.find((row) => row.id === jobId)
            ?.input_manifest_id,
          trace_id: "ui05-cost-trace",
          actual_cost: null,
          estimated_cost: "0.250000",
          pending_estimate: "0.250000",
          cost_is_estimate: true,
        },
      ];
      return route.fulfill({
        json: { items: slice(items, url, 10), total: items.length },
      });
    }
    if (!path.startsWith(base)) return route.fallback();
    if (method === "GET") reads.push(request.url());
    if (path === base) {
      if (control.listStatus)
        return fail(
          route,
          control.listStatus,
          "MEDIA_LIST_UNAVAILABLE",
          "媒体任务暂不可读",
        );
      return route.fulfill({ json: slice(jobs, url) });
    }
    if (path === base + "/queue") {
      if (control.queueStatus)
        return fail(
          route,
          control.queueStatus,
          "QUEUE_UNAVAILABLE",
          "队列暂不可读",
        );
      return route.fulfill({ json: queue });
    }
    const [jobId, command] = path.slice(base.length + 1).split("/");
    const job = jobs.find((row) => row.id === jobId);
    if (!job) return fail(route, 404, "NOT_FOUND", "媒体任务不存在");
    if (method === "GET") {
      if (!command) {
        if (control.detailStatus)
          return fail(
            route,
            control.detailStatus,
            "FORBIDDEN",
            "此任务权限已变化",
          );
        if (control.jobRead && (await control.jobRead(route, job))) return;
        return route.fulfill({ json: job });
      }
      if (command === "attempts") {
        if (control.attemptStatus)
          return fail(
            route,
            control.attemptStatus,
            "ATTEMPTS_UNAVAILABLE",
            "尝试记录暂不可读",
          );
        return route.fulfill({ json: slice(attempts.get(jobId) ?? [], url) });
      }
      if (command === "calls")
        return route.fulfill({ json: slice(calls.get(jobId) ?? [], url) });
      if (command === "queue")
        return route.fulfill({
          json: {
            id: id(),
            job_id: jobId,
            lane: "INTERACTIVE",
            origin: "USER",
            policy_version: 1,
          },
        });
      if (command === "schedule")
        return route.fulfill({
          json: schedules.get(jobId) ?? {
            id: id(),
            job_id: jobId,
            policy_json: { max_attempts: 3, max_elapsed_seconds: 600 },
            deadline_at: "2026-11-11T07:10:00Z",
            next_attempt_at: now,
            blocked_code: null,
          },
        });
      if (command === "dead-letter")
        return route.fulfill({ json: deadLetters.get(jobId) ?? null });
      return fail(route, 404, "NOT_FOUND", "没有此媒体查询");
    }
    const body = request.postDataJSON() ?? {};
    const key = request.headers()["idempotency-key"];
    mutations.push({ command, jobId, body, key });
    if (control.mutationGate) await control.mutationGate;
    if (control.role === "viewer" || control.archived)
      return fail(route, 403, "FORBIDDEN", "项目只读");
    if (["resolve", "replay"].includes(command) && control.role !== "owner")
      return fail(route, 403, "FORBIDDEN", "只有拥有者可以确认");
    if (control.mutationStatus)
      return fail(
        route,
        control.mutationStatus,
        control.mutationStatus === 403
          ? "FORBIDDEN"
          : control.mutationStatus === 422
            ? "INVALID_INPUT"
            : "REVISION_CONFLICT",
        control.mutationStatus === 403
          ? "项目写权限已撤销"
          : control.mutationStatus === 422
            ? "重新发起原因不符合要求"
            : "任务版本已更新，请刷新",
      );
    if (command === "replay" && key && replayed.has(key))
      return route.fulfill({ status: 201, json: replayed.get(key) });
    if (body.expected_revision !== job.revision)
      return fail(route, 409, "REVISION_CONFLICT", "任务版本已更新，请刷新");
    if (command === "cancel") {
      job.cancel_requested_at = "2026-11-11T07:01:00Z";
      if (job.status === "FAILED") {
        job.status = "CANCELLED";
        job.status_label = "服务端状态 CANCELLED";
      } else if (
        !["RESULT_UNKNOWN", "RECONCILING", "MANUAL_REVIEW"].includes(job.status)
      ) {
        job.status = "CANCEL_REQUESTED";
        job.status_label = "服务端状态 CANCEL_REQUESTED";
      }
    } else if (command === "retry") {
      if (control.retryUnchanged) return route.fulfill({ json: job });
      job.status = "RETRY_WAIT";
      job.status_label = "服务端状态 RETRY_WAIT";
    } else if (command === "reconcile") {
      job.status = "MANUAL_REVIEW";
      job.status_label = "服务端状态 MANUAL_REVIEW";
    } else if (command === "resolve") {
      const receipt = body.receipt as {
        status: MediaStatus | "NOT_ACCEPTED";
        cost?: string;
      };
      job.status =
        receipt.status === "NOT_ACCEPTED" ? "RETRY_WAIT" : receipt.status;
      job.status_label = `服务端状态 ${job.status}`;
      if (receipt.cost) {
        totals.actual_cost = receipt.cost;
        totals.projected_cost = receipt.cost;
        totals.pending_estimate = "0.000000";
        totals.cost_is_estimate = false;
        totals.confirmed_count = 1;
        totals.estimated_count = 0;
      }
    } else if (command === "replay") {
      if (!key) return fail(route, 422, "MISSING_KEY", "缺少幂等键");
      const createdJob = createJob("CREATED", job.episode_id);
      createdJob.revision = 0;
      createdJob.attempt_count = 0;
      jobs.unshift(createdJob);
      replayed.set(key, createdJob);
      if (control.replayDropOnce) {
        control.replayDropOnce = false;
        return route.abort("failed");
      }
      return route.fulfill({ status: 201, json: createdJob });
    }
    job.revision++;
    return route.fulfill({ json: job });
  });
  const href = (episodeIndex = 0) =>
    `/projects/${project.id}/episodes/${episodes[episodeIndex].id}?view=tasks`;
  const open = async (episodeIndex = 0) => {
    await page.goto(href(episodeIndex));
    await closeDirectory(page);
    const panel = page.getByRole("region", {
      name: "项目媒体任务",
      exact: true,
    });
    await expect(panel).toBeVisible();
    return panel;
  };
  const select = async (job = jobs[0]) => {
    const panel = page.getByRole("region", {
      name: "项目媒体任务",
      exact: true,
    });
    await panel
      .getByRole("button", { name: `查看媒体任务 ${job.id}`, exact: true })
      .click();
    const detail = panel.getByRole("region", {
      name: `媒体任务详情 ${job.id}`,
      exact: true,
    });
    await expect(detail).toBeVisible();
    return detail;
  };
  const terminal = (status: MediaStatus = "FAILED", job = jobs[0]) => {
    job.status = status;
    job.status_label = `服务端状态 ${status}`;
    attempts.set(job.id, [createAttempt(job)]);
    return job;
  };
  const deadLetter = (job = jobs[0]) => {
    deadLetters.set(job.id, {
      id: id(),
      job_id: job.id,
      attempt_id: attempts.get(job.id)?.[0]?.id ?? null,
      reason: "ATTEMPT_LIMIT",
      error_code: "PROVIDER_UNAVAILABLE",
      category: "SERVICE",
      diagnostic_json: { provider_debug: secret },
    });
  };
  return {
    project,
    episodes,
    jobs,
    attempts,
    calls,
    costEntries,
    deadLetters,
    schedules,
    control,
    totals,
    queue,
    mutations,
    reads,
    replayed,
    createJob,
    createAttempt,
    href,
    open,
    select,
    terminal,
    deadLetter,
  };
}
