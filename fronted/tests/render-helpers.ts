import { createHash } from "node:crypto";
import { expect, type Page, type Route } from "@playwright/test";
import type {
  DeliveryAcceptance,
  HumanReview,
  MachineReport,
  RenderAdmission,
  RenderDeliveryDetail,
  RenderDeliveryReport,
  RenderJob,
  RenderManifest,
  RenderOutput,
  RenderProfile,
  RenderSource,
  RenderWarning,
  ReviewDecision,
  Timeline,
  TimelineCreate,
  TimelineSnapshot,
} from "../src/features/rendering/types";
import type { ContentVersion, Episode, Graph, Project } from "../src/types/api";
import type { ContinuityComparison } from "../src/features/continuity/types";
import { closeDirectory } from "./workspace-helpers";

const now = "2026-11-11T07:00:00Z";
const id = () => crypto.randomUUID();
const hash = (input: string | Buffer) =>
  createHash("sha256").update(input).digest("hex");
type Profile = RenderProfile;
type Phase = DeliveryAcceptance["phase"];
type Mutation = { path: string; body: Record<string, unknown> };

function acceptance(phase: Phase): DeliveryAcceptance {
  return {
    phase,
    is_delivery: ["COMPLETED", "COMPLETED_WITH_WARNINGS"].includes(phase),
    warnings_confirmed: phase === "COMPLETED_WITH_WARNINGS",
    review_id: null as string | null,
    reviewed_by_user_id: null as string | null,
    blocking_issues: [] as string[],
  };
}

// These controlled renderer receipts prove browser/API contracts. Authentication,
// projects and episodes are real. Backend renderer tests prove media/provenance.
export async function renderFixture(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `render12-${id()}@example.com`,
      password: "render-delivery-browser-contract",
      display_name: "成片验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const createdProject = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "成片工作区 · 精确交付" },
  });
  expect(createdProject.status()).toBe(201);
  const project: Project = await createdProject.json();
  const episodes: Episode[] = [];
  for (let index = 0; index < 2; index++) {
    const created = await page.request.post(
      `/api/v1/projects/${project.id}/episodes`,
      {
        headers,
        data: {
          episode_no: index + 1,
          title: index ? "第二集的独立成片" : "第一集的固定交付",
        },
      },
    );
    expect(created.status()).toBe(201);
    episodes.push(await created.json());
  }

  const output = (name: Profile): RenderOutput => ({
    name,
    width: 320,
    height: 180,
    fps: 24,
    container: "mp4",
    video_codec: "h264",
    video_encoder: "libx264",
    video_bitrate: 800000,
    pixel_format: "yuv420p",
    audio_codec: "aac",
    audio_bitrate: 128000,
    audio_sample_rate: 48000,
    audio_channels: 2,
  });
  const software = {
    renderer_version: "arclight-renderer/browser-fixture",
    pyav_version: "fixture",
    ffmpeg_libraries: {
      libavcodec: "fixture",
      libavformat: "fixture",
      libavutil: "fixture",
    },
  };
  const source: RenderSource = {
    asset_id: id(),
    asset_version_id: id(),
    asset_type: "SHOT_IMAGE",
    sha256: hash("fixed render input"),
    file: null,
  };
  const trackKinds: TimelineSnapshot["tracks"][number]["kind"][] = [
    "VIDEO",
    "VOICE",
    "MUSIC",
    "SFX",
    "SUBTITLE",
  ];
  const timelines: Timeline[] = episodes.map((episode): Timeline => ({
    id: id(),
    asset_id: id(),
    version_no: 1,
    status: "APPROVED",
    stale: false,
    content_sha256: hash(episode.id + ":timeline"),
    dependency_manifest_id: id(),
    created_at: now,
    based_on_timeline_id: null,
    snapshot: {
      schema_version: 1,
      episode_id: episode.id,
      storyboard_id: id(),
      default_duration_ms: 3000,
      duration_ms: 3000,
      output_profile: { width: 320, height: 180, fps: 24 },
      tracks: trackKinds.map((kind, ordinal) => ({
        id: id(),
        kind,
        ordinal,
        clips:
          kind === "VIDEO"
            ? [
                {
                  id: id(),
                  ordinal: 0,
                  source_asset_version_id: source.asset_version_id,
                  source_sha256: source.sha256,
                  source_start_ms: 0,
                  shot_id: id(),
                  script_version_id: null,
                  start_ms: 0,
                  duration_ms: 3000,
                  line_id: null,
                  audio_version_id: null,
                  review_id: id(),
                  review_storyboard_id: null,
                  review_shot_id: null,
                  visual: null,
                },
              ]
            : [],
      })),
    },
  }));
  const timelineTemplates = structuredClone(timelines);
  const sourceBoards: ContentVersion[] = timelines.map((timeline) => ({
    id: timeline.snapshot.storyboard_id,
    episode_id: timeline.snapshot.episode_id,
    version_no: 1,
    content_json: {},
    status: "APPROVED",
    run_id: null,
    script_version_id: id(),
  }));
  const dialogueLines: {
    script_version_id: string;
    line_id: string;
    text: string;
  }[] = [];
  const manifest = (
    episodeIndex: number,
    profile: Profile,
  ): RenderManifest => ({
    id: id(),
    project_id: project.id,
    created_by: session.user.id,
    content_sha256: hash(episodes[episodeIndex].id + profile),
    snapshot: {
      schema_version: 1,
      timeline_id: timelines[episodeIndex].id,
      timeline_sha256: timelines[episodeIndex].content_sha256,
      dependency_manifest_id: timelines[episodeIndex].dependency_manifest_id,
      dependency_manifest_sha256: hash("frozen dependencies"),
      timeline: timelines[episodeIndex].snapshot,
      visuals: [],
      sources: [source],
      output: output(profile),
      subtitle_style: {
        mode: "BURN_IN",
        font_size: 48,
        color: "#FFFFFF",
        outline_color: "#000000",
        outline_width: 2,
        margin_bottom: 64,
        alignment: "BOTTOM_CENTER",
        bold: false,
      },
      font: null,
      software,
      created_at: now,
    },
  });
  const previewManifests = episodes.map((_, index) =>
    manifest(index, "PREVIEW"),
  );
  const formalManifests = episodes.map((_, index) => manifest(index, "FORMAL"));
  const manifests = [...previewManifests, ...formalManifests];
  const job = (frozen: RenderManifest): RenderJob => ({
    id: id(),
    manifest_id: frozen.id,
    manifest_sha256: frozen.content_sha256,
    retry_of_id: null as string | null,
    profile: frozen.snapshot.output.name,
    status: "QUEUED",
    revision: 1,
    progress: 0,
    phase: "VALIDATING",
    attempt_count: 0,
    cancel_requested_at: null as string | null,
    output_file_id: null as string | null,
    output_asset_version_id: null as string | null,
    artifact_id: null as string | null,
    error_code: null as string | null,
    cleanup_status: "PENDING",
    created_at: now,
    started_at: null as string | null,
    finished_at: null as string | null,
    is_delivery: false,
    report_id: null as string | null,
    delivery_phase: null as Phase | null,
  });
  const jobs: RenderJob[] = [];
  // Fixed offline 64×64 H264/AAC media from the existing backend inspection helper.
  // Embedded so CI does not depend on local ignored verification artifacts.
  let media = Buffer.from(
    "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",
    "base64",
  );
  const delivery = (
    episodeIndex: number,
    version: number,
    phase: Phase,
  ): RenderDeliveryDetail => {
    const frozen =
      phase === "PREVIEW"
        ? previewManifests[episodeIndex]
        : formalManifests[episodeIndex];
    return {
      id: id(),
      project_id: project.id,
      episode_id: episodes[episodeIndex].id,
      job_id: id(),
      asset_id: id(),
      asset_version_id: id(),
      version_no: version,
      file_id: id(),
      sha256: hash(media),
      byte_size: media.length,
      duration_ms: 3000,
      profile: (phase === "PREVIEW" ? "PREVIEW" : "FORMAL") as Profile,
      manifest_id: frozen.id,
      manifest_sha256: frozen.content_sha256,
      timeline_id: timelines[episodeIndex].id,
      dependency_manifest_id: id(),
      inspection_id: id(),
      report_id: phase === "PREVIEW" || phase === "LEGACY" ? null : id(),
      report_file_id: phase === "PREVIEW" || phase === "LEGACY" ? null : id(),
      storage_backend: "LOCAL" as "LOCAL" | "S3",
      stale: false,
      is_delivery: acceptance(phase).is_delivery,
      acceptance: acceptance(phase),
      created_at: now,
      timeline_sha256: timelines[episodeIndex].content_sha256,
      timeline_dependency_manifest_id:
        timelines[episodeIndex].dependency_manifest_id,
      timeline_dependency_manifest_sha256: hash("timeline dependencies"),
      sources: [source],
      font: null,
      software,
      output: output(phase === "PREVIEW" ? "PREVIEW" : "FORMAL"),
    };
  };
  const deliveries = [
    delivery(0, 3, "COMPLETED"),
    delivery(0, 2, "PREVIEW"),
    delivery(0, 1, "LEGACY"),
    delivery(1, 1, "COMPLETED"),
  ];
  type Delivery = RenderDeliveryDetail;
  const warnings: RenderWarning[] = [
    {
      id: hash("fixed subtitle-only warning"),
      code: "SUBTITLE_ONLY",
      source_version_id: source.asset_version_id,
      line_id: "line-1",
      reason: "此句按已确认分镜只显示字幕。",
      human_review_request_id: null,
      human_review_decision_id: null,
      human_review_request_sha256: null,
    },
    {
      id: hash("fixed silence warning"),
      code: "EXPLICIT_SILENCE",
      source_version_id: source.asset_version_id,
      line_id: "line-2",
      reason: "角色停顿按创作决定保留静默。",
      human_review_request_id: null,
      human_review_decision_id: null,
      human_review_request_sha256: null,
    },
  ];
  const review = (
    kind: "EXPORT" | "DELIVERY",
    versionId: string,
  ): HumanReview => ({
    id: id(),
    project_id: project.id,
    requested_by: session.user.id,
    request_sha256: hash(versionId + kind),
    status: "WAITING_REVIEW",
    blocking_code: null as string | null,
    created_at: now,
    updated_at: now,
    snapshot: {
      schema_version: "human-review/1",
      project_id: project.id,
      episode_id: episodes[0].id,
      run_id: null,
      loop_id: null,
      thread_id: `render:${versionId}`,
      kind,
      purpose: "PRODUCTION",
      object_id: versionId,
      version_id: versionId,
      content_sha256: hash(versionId),
      input_sha256: hash("frozen review input"),
      source_version_ids: [source.asset_version_id],
      expected_revision: 0,
    },
    decision: null,
    resume: null,
  });
  const reviews: ReturnType<typeof review>[] = [];
  const control: {
    role: "owner" | "editor" | "viewer";
    archived: boolean;
    readStatus?: number;
    jobNetworkFailure?: boolean;
    jobGate?: Promise<void>;
    warnings: boolean;
    detailGate?: (route: Route, row: Delivery) => Promise<void>;
    reportGate?: (route: Route, row: Delivery) => Promise<void>;
    contentGate?: (route: Route, row: Delivery) => Promise<void>;
  } = { role: "owner", archived: false, warnings: false };
  const mutations: Mutation[] = [];
  const reads: string[] = [];
  const contentReads: { path: string; authorization: string }[] = [];
  const createdJobs = new Map<string, RenderJob>();
  const createdManifests = new Map<string, RenderManifest>();
  const createdTimelines = new Map<string, Timeline>();
  const paginate = <T extends { id: string }>(rows: T[], url: URL) => {
    const before = url.searchParams.get("before_id");
    const start = before ? rows.findIndex((row) => row.id === before) + 1 : 0;
    return rows.slice(
      start,
      start + Number(url.searchParams.get("limit") ?? 30),
    );
  };
  const fail = (route: Route, status: number, code: string, text: string) =>
    route.fulfill({
      status,
      json: {
        error: { code, message: text, request_id: "render-browser-request" },
      },
    });
  const report = (row: Delivery): RenderDeliveryReport => ({
    report_id: row.report_id!,
    report_file_id: row.report_file_id!,
    report_sha256: hash(row.id + ":report"),
    report: {
      schema_version: 1,
      manifest_id: row.manifest_id,
      manifest_sha256: row.manifest_sha256,
      timeline_id: row.timeline_id,
      output_file_id: row.file_id,
      output_sha256: row.sha256,
      details: {
        schema_version: "render-report/1",
        checker: "arclight-delivery-v1",
        render_status: "passed",
        job_id: row.job_id,
        attempt_id: id(),
        artifact_id: row.id,
        asset_version_id: row.asset_version_id,
        manifest_id: row.manifest_id,
        manifest_sha256: row.manifest_sha256,
        timeline_id: row.timeline_id,
        timeline_sha256: row.timeline_sha256,
        timeline_version: 1,
        inspection_id: row.inspection_id,
        input_sha256: hash("input"),
        output_file_id: row.file_id,
        output_sha256: row.sha256,
        byte_size: row.byte_size,
        duration_ms: row.duration_ms,
        profile: "FORMAL",
        sources: row.sources,
        font: null,
        software,
        output: row.output,
        line_audio_decisions: [],
        checks: {
          video_decodable: true,
          audio_stream_present: true,
          subtitle_range_valid: true,
          missing_required_shots: 0,
          missing_audio_lines: 0,
          stale_dependencies: 0,
          technical: {
            passed: true,
            error_code: null,
            output_sha256: row.sha256,
            byte_size: row.byte_size,
            checks: [
              "decoding",
              "tracks",
              "resolution",
              "frame_rate",
              "video_codec",
              "pixel_format",
              "audio_codec",
              "audio_format",
              "duration",
            ].map((name) => ({ name, passed: true })),
            measurement: null,
          },
        },
        warnings: control.warnings ? warnings : [],
      } satisfies MachineReport,
    },
    acceptance: row.acceptance,
  });

  await page.route("**/api/v1/**", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    if (method === "GET") {
      const episodeIndex = episodes.findIndex(
        (episode) =>
          path === `/api/v1/episodes/${episode.id}/storyboards` ||
          path === `/api/v1/episodes/${episode.id}/dialogue-lines`,
      );
      if (episodeIndex >= 0) {
        reads.push(request.url());
        if (path.endsWith("/storyboards"))
          return route.fulfill({ json: [sourceBoards[episodeIndex]] });
        return route.fulfill({
          json: dialogueLines.filter(
            (line) =>
              line.script_version_id ===
              url.searchParams.get("script_version_id"),
          ),
        });
      }
    }
    if (path === `/api/v1/projects/${project.id}/access`)
      return route.fulfill({ json: { role: control.role } });
    if (
      control.archived &&
      method === "GET" &&
      (path === `/api/v1/projects/${project.id}/episodes` ||
        episodes.some((episode) => path === `/api/v1/episodes/${episode.id}`))
    ) {
      const original = await route.fetch();
      const data = await original.json();
      return route.fulfill({
        response: original,
        json: Array.isArray(data)
          ? data.map((row: Episode) => ({ ...row, status: "ARCHIVED" }))
          : { ...data, status: "ARCHIVED" },
      });
    }
    const rendererPath =
      /\/(timelines|render-manifests|render-jobs|deliveries|human-reviews)(\/|$)/.test(
        path,
      );
    if (!rendererPath) return route.fallback();
    if (method === "GET") {
      reads.push(request.url());
      if (control.readStatus)
        return fail(
          route,
          control.readStatus,
          control.readStatus === 403 ? "FORBIDDEN" : "NOT_FOUND",
          "当前项目权限已变化，请重新核对。",
        );
    } else {
      const body = request.postDataJSON() ?? {};
      mutations.push({ path, body });
      if (control.role === "viewer" || control.archived)
        return fail(route, 403, "FORBIDDEN", "当前成员无写入权限。");
    }
    for (let index = 0; index < episodes.length; index++) {
      const prefix = `/api/v1/episodes/${episodes[index].id}`;
      if (path === prefix + "/timelines") {
        const rows = timelines.filter(
          (row) => row.snapshot.episode_id === episodes[index].id,
        );
        if (method === "POST") {
          const body: TimelineCreate = request.postDataJSON();
          let timeline = createdTimelines.get(body.creation_key);
          if (!timeline) {
            const template = structuredClone(timelineTemplates[index]);
            timeline = {
              ...template,
              id: id(),
              version_no: Math.max(0, ...rows.map((row) => row.version_no)) + 1,
              content_sha256: hash(JSON.stringify(body)),
              snapshot: {
                ...template.snapshot,
                storyboard_id: body.storyboard_id,
                default_duration_ms: body.default_duration_ms,
                duration_ms:
                  body.selected_shots.length * body.default_duration_ms,
                tracks: template.snapshot.tracks.map((track) => ({
                  ...track,
                  clips:
                    track.kind === "VIDEO"
                      ? body.selected_shots.map((selection, ordinal) => ({
                          ...template.snapshot.tracks[0].clips[0],
                          id: id(),
                          ordinal,
                          source_asset_version_id: selection.asset_version_id,
                          source_sha256: hash(selection.asset_version_id),
                          shot_id: selection.shot_id,
                          start_ms: ordinal * body.default_duration_ms,
                          duration_ms: body.default_duration_ms,
                          review_storyboard_id: body.storyboard_id,
                          review_shot_id: selection.shot_id,
                        }))
                      : [],
                })),
              },
            };
            timelines.unshift(timeline);
            createdTimelines.set(body.creation_key, timeline);
          }
          return route.fulfill({ status: 201, json: timeline });
        }
        const before = Number(
          url.searchParams.get("before_version") ?? Infinity,
        );
        return route.fulfill({
          json: rows
            .filter((row) => row.version_no < before)
            .slice(0, Number(url.searchParams.get("limit") ?? 30)),
        });
      }
      if (path === prefix + "/deliveries")
        return route.fulfill({
          json: paginate(
            deliveries.filter((row) => row.episode_id === episodes[index].id),
            url,
          ),
        });
      const row = deliveries.find((item) =>
        path.startsWith(prefix + "/deliveries/" + item.id),
      );
      if (!row) continue;
      if (path.endsWith("/warning-review")) {
        let record = reviews.find(
          (entry) =>
            entry.snapshot.kind === "DELIVERY" &&
            entry.snapshot.version_id === row.asset_version_id,
        );
        if (!record) {
          record = review("DELIVERY", row.asset_version_id);
          reviews.push(record);
        }
        return route.fulfill({ status: 201, json: record });
      }
      if (path.endsWith("/report")) {
        if (
          !row.report_id ||
          !row.report_file_id ||
          row.acceptance.phase === "LEGACY"
        )
          return fail(route, 404, "NOT_FOUND", "本版本没有正式机器验收报告。");
        if (control.reportGate) return control.reportGate(route, row);
        return route.fulfill({ json: report(row) });
      }
      if (path.endsWith("/content")) {
        contentReads.push({
          path,
          authorization: request.headers().authorization ?? "",
        });
        if (control.contentGate) return control.contentGate(route, row);
        return route.fulfill({ contentType: "video/mp4", body: media });
      }
      if (control.detailGate) return control.detailGate(route, row);
      return route.fulfill({ json: row });
    }
    const timeline = timelines.find((row) =>
      path.includes(`/timelines/${row.id}`),
    );
    if (timeline) {
      if (path.endsWith("/render-manifests")) {
        if (method === "GET")
          return route.fulfill({
            json: paginate(
              manifests.filter(
                (row) => row.snapshot.timeline_id === timeline.id,
              ),
              url,
            ),
          });
        const body = request.postDataJSON();
        let frozen = createdManifests.get(body.creation_key);
        if (!frozen) {
          const index = episodes.findIndex(
            (episode) => episode.id === timeline.snapshot.episode_id,
          );
          frozen = manifest(index, body.profile);
          frozen.snapshot.line_audio_decisions =
            body.line_audio_decisions ?? [];
          manifests.unshift(frozen);
          createdManifests.set(body.creation_key, frozen);
        }
        return route.fulfill({ status: 201, json: frozen });
      }
      return route.fulfill({ json: timeline });
    }
    const frozen = manifests.find((row) =>
      path.includes(`/render-manifests/${row.id}`),
    );
    if (frozen) {
      if (path.endsWith("/export-review")) {
        let record = reviews.find(
          (entry) =>
            entry.snapshot.kind === "EXPORT" &&
            entry.snapshot.version_id === frozen.id,
        );
        if (!record) {
          record = review("EXPORT", frozen.id);
          reviews.push(record);
        }
        return route.fulfill({ status: 201, json: record });
      }
      if (path.endsWith("/admission")) {
        const approved = reviews.find(
          (row) =>
            row.snapshot.kind === "EXPORT" &&
            row.snapshot.version_id === frozen.id &&
            row.status === "RESUMED" &&
            row.decision?.decision_json.decision === "APPROVE",
        );
        if (frozen.snapshot.output.name === "FORMAL" && !approved)
          return fail(
            route,
            409,
            "EXPORT_REVIEW_REQUIRED",
            "本次正式清单尚未获拥有者批准并独立恢复。",
          );
        return route.fulfill({
          json: {
            manifest_id: frozen.id,
            manifest_sha256: frozen.content_sha256,
            profile: frozen.snapshot.output.name,
            review_request_id: approved?.id ?? null,
            decision_id: approved?.decision?.id ?? null,
            checked_at: now,
            admitted: true,
            requires_recheck: true,
            is_delivery: false,
          } satisfies RenderAdmission,
        });
      }
      if (path.endsWith("/jobs")) {
        if (method === "GET")
          return route.fulfill({
            json: paginate(
              jobs.filter((row) => row.manifest_id === frozen.id),
              url,
            ),
          });
        if (control.jobGate) await control.jobGate;
        const body = request.postDataJSON();
        let queued = createdJobs.get(body.creation_key);
        if (!queued) {
          queued = job(frozen);
          queued.retry_of_id = body.retry_of ?? null;
          jobs.unshift(queued);
          createdJobs.set(body.creation_key, queued);
        }
        if (control.jobNetworkFailure) {
          control.jobNetworkFailure = false;
          return route.abort("failed");
        }
        return route.fulfill({ status: 202, json: queued });
      }
      return route.fulfill({ json: frozen });
    }
    const task = jobs.find((row) => path.includes(`/render-jobs/${row.id}`));
    if (task) {
      if (path.endsWith("/attempts"))
        return route.fulfill({
          json: [
            {
              attempt_no: 1,
              status: task.status,
              progress: task.progress,
              phase: task.phase,
              error_code: task.error_code,
              cleanup_status: task.cleanup_status,
              cleanup_error_code: null,
              created_at: now,
              finished_at: task.finished_at,
              inspection: null,
            },
          ],
        });
      if (path.endsWith("/cancel")) {
        const body = request.postDataJSON();
        if (body.expected_revision !== task.revision)
          return fail(
            route,
            409,
            "RENDER_JOB_CONFLICT",
            "任务状态已变化，请刷新。",
          );
        task.status = "CANCELLED";
        task.cancel_requested_at = now;
        task.finished_at = now;
        task.revision++;
        task.cleanup_status = "CLEANED";
      }
      return route.fulfill({ json: task });
    }
    if (path === `/api/v1/projects/${project.id}/human-reviews`)
      return route.fulfill({ json: reviews });
    const record = reviews.find((row) =>
      path.includes(`/human-reviews/${row.id}`),
    );
    if (record) {
      if (method === "GET") return route.fulfill({ json: record });
      const body = request.postDataJSON();
      if (path.endsWith("/decisions")) {
        if (
          body.version_id !== record.snapshot.version_id ||
          body.request_sha256 !== record.request_sha256
        )
          return fail(
            route,
            409,
            "HUMAN_REVIEW_CONFLICT",
            "精确审核输入已变化。",
          );
        record.decision = {
          id: id(),
          request_id: record.id,
          actor_id: session.user.id,
          created_at: now,
          updated_at: now,
          decision_json: {
            ...body,
            impact_digest: body.impact_digest ?? null,
          } as ReviewDecision["decision_json"],
        };
        record.status = "DECIDED";
        return route.fulfill({ json: record.decision });
      }
      if (
        !record.decision ||
        body.decision_id !== record.decision.id ||
        body.request_sha256 !== record.request_sha256
      )
        return fail(
          route,
          409,
          "HUMAN_REVIEW_CONFLICT",
          "没有匹配的人工决定。",
        );
      record.resume = {
        id: id(),
        request_id: record.id,
        decision_id: record.decision.id,
        actor_id: session.user.id,
        created_at: now,
        updated_at: now,
        result_json: { status: "APPROVED" },
      };
      record.status = "RESUMED";
      if (record.snapshot.kind === "DELIVERY") {
        const row = deliveries.find(
          (item) => item.asset_version_id === record.snapshot.version_id,
        )!;
        row.acceptance = acceptance("COMPLETED_WITH_WARNINGS");
        row.acceptance.review_id = record.id;
        row.acceptance.reviewed_by_user_id = session.user.id;
        row.is_delivery = true;
      }
      return route.fulfill({ json: record.resume });
    }
    return fail(route, 404, "NOT_FOUND", "没有匹配的精确渲染回执。");
  });

  const href = (index = 0) =>
    `/projects/${project.id}/episodes/${episodes[index].id}?view=render`;
  const open = async (index = 0) => {
    await page.goto(href(index));
    await closeDirectory(page);
    const panel = page.getByRole("region", {
      name: "渲染与成片",
      exact: true,
    });
    await expect(panel).toBeVisible();
    if (timelines.some((row) => row.snapshot.episode_id === episodes[index].id))
      await expect(
        panel.getByLabel("时间轴版本", { exact: true }),
      ).toBeEnabled();
    else
      await expect(
        panel.getByText(
          "还没有时间轴。请从已批准分镜的当前选用镜头创建最小时间轴。",
          { exact: true },
        ),
      ).toBeVisible();
    return panel;
  };
  return {
    project,
    episodes,
    session,
    headers,
    timelines,
    manifests,
    jobs,
    job,
    deliveries,
    delivery,
    warnings,
    reviews,
    source,
    sourceBoards,
    dialogueLines,
    review,
    report,
    mutations,
    reads,
    contentReads,
    control,
    href,
    open,
    mediaBytes: () => Buffer.from(media),
    setMedia(bytes: Buffer) {
      media = Buffer.from(bytes);
      for (const row of deliveries) {
        row.sha256 = hash(media);
        row.byte_size = media.length;
      }
    },
  };
}

// Controlled reads for the approved-board/current-selection preparation path.
// No new media is selected: the browser must resolve already selected alternatives.
export async function defaultTimelineFixture(
  page: Page,
  env: Awaited<ReturnType<typeof renderFixture>>,
  missingSecondShot = false,
) {
  const episode = env.episodes[0];
  const board: ContentVersion = {
    id: id(),
    episode_id: episode.id,
    version_no: 3,
    content_json: {},
    status: "APPROVED",
    run_id: null,
    script_version_id: id(),
  };
  const shots = [1, 2].map((shot_no) => ({
    id: id(),
    storyboard_id: board.id,
    shot_no,
    description: `固定镜头 ${shot_no}`,
    content_json: {} as Record<string, unknown>,
  }));
  const choices = [0, 0, 1].map((shotIndex, index) => ({
    asset_id: id(),
    asset_version_id: id(),
    shot_id: shots[shotIndex].id,
    name: `已有选用图片 ${index + 1}`,
    selected: !(missingSecondShot && shotIndex === 1),
  }));
  const graph: Graph = {
    nodes: choices.map((choice) => ({
      id: `version:${choice.asset_version_id}`,
      resource_id: choice.asset_version_id,
      kind: "asset_version",
      title: choice.name,
      status: "APPROVED",
      version_no: 1,
      asset_id: choice.asset_id,
      asset_type: "SHOT_IMAGE",
      stale: false,
      current: choice.selected,
    })),
    edges: [],
    layout: { revision: 1, positions: {}, viewport: { x: 0, y: 0, zoom: 1 } },
  };
  const comparisons = choices.map((choice): ContinuityComparison => {
    const resultId = id();
    return {
      project_id: env.project.id,
      asset: {
        id: choice.asset_id,
        project_id: env.project.id,
        asset_type: "SHOT_IMAGE",
        name: choice.name,
        status: "APPROVED",
        current_version_id: choice.selected ? choice.asset_version_id : null,
        lock_version: 0,
        revision: 1,
        metadata: {
          description: choice.name,
          tags: [],
          usage_scope: { kind: "PROJECT", episode_ids: [] },
          appearance: {
            age_range: "",
            appearance: "",
            hairstyle: "",
            clothing: "",
            build: "",
            expressions: "",
            colors: "",
            materials: "",
          },
        },
        creation_source: null,
        created_by: env.session.user.id,
        character_profile: null,
        scene_profile: null,
        prop_profile: null,
        created_at: now,
        updated_at: now,
      },
      version: {
        id: choice.asset_version_id,
        asset_id: choice.asset_id,
        version_no: 1,
        checksum: hash(choice.asset_version_id),
        created_by: env.session.user.id,
        created_at: now,
        status: "APPROVED",
        stale: false,
        revision: 1,
        archived_from_status: null,
        media: null,
        asset_snapshot: null,
        restored_from_version_id: null,
        restoration_note: null,
        provenance: null,
      },
      candidate: null,
      image_path: "/unused-test-image",
      latest_review_id: resultId,
      reviews: [
        {
          record: {
            sequence: 1,
            recorded_by: env.session.user.id,
            recorded_at: now,
            review: {
              result_id: resultId,
              conclusion: "PASS",
              summary: "精确镜头审查通过",
              risk: "NONE",
              suggested_action: "PASS",
              target: {
                candidate_id: id(),
                asset_version_id: choice.asset_version_id,
                shot_id: choice.shot_id,
                storyboard_id: board.id,
              },
              confidence: { level: "HIGH", uncertainty: [] },
              issues: [],
              evidence: [],
              versions: [],
              rerun: null,
            },
          },
          references: [],
        },
      ],
      next_offset: null,
      images: [],
      images_truncated: false,
      revisions: [],
      human_reviews: [],
      next_human_offset: null,
      actions: {
        owner: true,
        writable: true,
        blocker: null,
        can_submit: false,
        can_approve: false,
        can_reject: false,
        can_select: false,
      },
    };
  });
  // Reused approved imagery keeps the historical review target; the storyboard
  // explicitly carries its review and selection into the new shot identity.
  shots[1].content_json = {
    quality_review: { review_id: comparisons[2].latest_review_id },
    selected_visual_asset: {
      asset_id: choices[2].asset_id,
      asset_version_id: choices[2].asset_version_id,
    },
  };
  comparisons[2].reviews[0].record.review.target.storyboard_id = id();
  comparisons[2].reviews[0].record.review.target.shot_id = id();
  env.timelines.splice(0, 1);
  const reads: string[] = [];
  await page.route(
    `**/api/v1/episodes/${episode.id}/storyboards?*`,
    async (route) => {
      await route.fulfill({
        json: [board, { ...board, id: id(), version_no: 4, status: "DRAFT" }],
      });
    },
  );
  await page.route(
    `**/api/v1/episodes/${episode.id}/shots?*`,
    async (route) => {
      reads.push(route.request().url());
      await route.fulfill({ json: shots });
    },
  );
  await page.route(`**/api/v1/episodes/${episode.id}/canvas`, async (route) => {
    reads.push(route.request().url());
    await route.fulfill({ json: graph });
  });
  await page.route(
    `**/api/v1/projects/${env.project.id}/assets/*/versions/*/continuity?*`,
    async (route) => {
      reads.push(route.request().url());
      const row = comparisons.find((item) =>
        route.request().url().includes(`/versions/${item.version.id}/`),
      );
      await route.fulfill({ json: row });
    },
  );
  return { board, shots, choices, reads };
}

export function deferred() {
  let release!: () => void;
  const promise = new Promise<void>((resolve) => {
    release = resolve;
  });
  return { promise, release };
}
