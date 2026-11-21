import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { resolve } from "node:path";
import { deflateSync } from "node:zlib";
import { expect, type Page } from "@playwright/test";
import type { GraphNode } from "../src/types/api";
import type { VideoAsset, VideoCandidate } from "../src/features/video/types";
import { shotProductionFixture } from "./shot-production-helpers";
import { canvasMediaFixture } from "./canvas-media-helpers";

export const checksum = (bytes: Buffer) =>
  createHash("sha256").update(bytes).digest("hex");

// Original flat RGB first frame, synthesized offline at the native input dimensions.
function firstFramePng() {
  const chunk = (kind: string, data: Buffer) => {
    const body = Buffer.concat([Buffer.from(kind), data]);
    let crc = 0xffffffff;
    for (const value of body) {
      crc ^= value;
      for (let i = 0; i < 8; i++)
        crc = (crc >>> 1) ^ (crc & 1 ? 0xedb88320 : 0);
    }
    const result = Buffer.alloc(data.length + 12);
    result.writeUInt32BE(data.length);
    body.copy(result, 4);
    result.writeUInt32BE((crc ^ 0xffffffff) >>> 0, result.length - 4);
    return result;
  };
  const header = Buffer.alloc(13);
  header.writeUInt32BE(320);
  header.writeUInt32BE(480, 4);
  header[8] = 8;
  header[9] = 2;
  const raw = Buffer.alloc(480 * (1 + 320 * 3));
  for (let y = 0; y < 480; y++)
    for (let x = 0; x < 320; x++) {
      const offset = y * (1 + 320 * 3) + 1 + x * 3;
      raw.set([27, 70, 113], offset);
    }
  return Buffer.concat([
    Buffer.from("89504e470d0a1a0a", "hex"),
    chunk("IHDR", header),
    chunk("IDAT", deflateSync(raw)),
    chunk("IEND", Buffer.alloc(0)),
  ]);
}

// All HTTP, artifact registration and media storage are real. Only the native
// supplier network is intercepted by the isolated e2e_server video worker.
export async function realVideoFixture(
  page: Page,
  loseGenerationResponse = false,
  forTimeline = false,
) {
  const env = await shotProductionFixture(page, {
    singleShot: forTimeline,
    outputProfile: forTimeline
      ? { width: 320, height: 180, fps: 30 }
      : undefined,
  });
  const root = `/api/v1/projects/${env.project.id}`;
  const call = { headers: env.headers };
  const configured = await page.request.put("/api/v1/me/model-services/video", {
    ...call,
    data: {
      mode: "custom",
      supplier: "custom",
      model: "doubao-seedance-1-0-pro-250528",
      protocol: "seedance",
      api_key: "video01-fixture-key",
      base_url: "https://native.example/v1",
      expected_revision: 0,
    },
  });
  expect(configured.status(), await configured.text()).toBe(200);
  const budget = await (await page.request.get(root + "/budget", call)).json();
  const limited = await page.request.patch(root + "/budget", {
    ...call,
    data: { expected_revision: budget.policy_revision, project_limit: "20" },
  });
  expect(limited.status(), await limited.text()).toBe(200);
  const created = await page.request.post(root + "/assets", {
    ...call,
    data: { name: "合成视频首帧", asset_type: "SCENE" },
  });
  expect(created.status()).toBe(201);
  const asset = await created.json();
  const versions = root + `/assets/${asset.id}/versions`;
  const saved = await page.request.post(versions, {
    ...call,
    data: {
      expected_revision: asset.revision,
      creation_key: crypto.randomUUID(),
      original_name: "first-frame.png",
      media_type: "image/png",
      content_base64: firstFramePng().toString("base64"),
      source_note: "离线原创合成首帧",
      rights_note: "测试原创图像",
      rights_confirmed: true,
    },
  });
  expect(saved.status(), await saved.text()).toBe(201);
  let frame = await saved.json();
  for (const status of ["READY_FOR_REVIEW", "APPROVED"]) {
    const reviewed = await page.request.patch(versions + `/${frame.id}/state`, {
      ...call,
      data: {
        expected_revision: frame.revision,
        status,
        comment: "独立测试首帧",
      },
    });
    expect(reviewed.status(), await reviewed.text()).toBe(200);
    frame = await reviewed.json();
  }
  const state = await env.state();
  const shot = state.shots[0];
  const generationKeys: string[] = [];
  const generationJobs: string[] = [];
  await page.route("**" + env.generationPath, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const data = route.request().postDataJSON();
    if (data.task_type !== "VIDEO") return route.continue();
    data.spec.context = {
      ...data.spec.context,
      e2e_synthetic_video: forTimeline ? "video03" : "video01",
    };
    generationKeys.push(route.request().headers()["idempotency-key"]);
    if (loseGenerationResponse) {
      const response = await route.fetch({ postData: JSON.stringify(data) });
      expect(response.status(), await response.text()).toBe(201);
      generationJobs.push((await response.json()).id);
      if (generationKeys.length === 1) return route.abort("failed");
      return route.fulfill({ response });
    }
    await route.continue({ postData: JSON.stringify(data) });
  });
  const dialog = await env.open();
  const videoShot = dialog.getByRole("region", {
    name: "镜头 1 视频候选",
    exact: true,
  });
  await videoShot
    .getByRole("button", { name: "为此镜头生成视频", exact: true })
    .click();
  const form = videoShot.getByRole("region", {
    name: "单镜头视频生成",
    exact: true,
  });
  await expect(form).toContainText("doubao-seedance-1-0-pro-250528");
  await form
    .getByLabel("已批准首帧精确版本", { exact: true })
    .selectOption(frame.id);
  await form
    .getByLabel("镜头视频提示词", { exact: true })
    .fill("女孩提灯走过小桥并回头。");
  await form.getByLabel("视频宽度", { exact: true }).fill("1280");
  await form.getByLabel("视频高度", { exact: true }).fill("720");
  await form.getByLabel("视频时长（秒）", { exact: true }).fill("6");
  await form
    .getByRole("button", { name: "估算此镜头视频费用", exact: true })
    .click();
  const confirmation = form.getByRole("region", {
    name: "视频费用确认与提交结果",
    exact: true,
  });
  await expect(confirmation).toContainText("最高费用");
  const submit = confirmation.getByRole("button", {
    name: "确认费用并生成镜头视频",
    exact: true,
  });
  await expect(submit).toBeDisabled();
  await confirmation
    .getByRole("checkbox", { name: /我确认本次视频最高费用/ })
    .check();
  let responsePromise = loseGenerationResponse
    ? null
    : page.waitForResponse(
        (response) =>
          new URL(response.url()).pathname === env.generationPath &&
          response.request().method() === "POST" &&
          response.status() === 201,
      );
  await submit.click();
  if (loseGenerationResponse) {
    await expect(form.getByRole("alert")).toContainText("网络连接失败");
    responsePromise = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === env.generationPath &&
        response.request().method() === "POST" &&
        response.status() === 201,
    );
    await confirmation
      .getByRole("button", { name: "核验原视频生成提交", exact: true })
      .click();
  }
  const captured = await responsePromise!;
  const job = await captured.json();
  if (loseGenerationResponse) {
    expect(generationKeys).toHaveLength(2);
    expect(generationKeys[0]).toBe(generationKeys[1]);
    expect(generationJobs[0]).toBe(generationJobs[1]);
  }
  await expect(confirmation.getByRole("status")).toContainText(job.id);
  let candidate!: VideoCandidate;
  await expect(async () => {
    const response = await page.request.get(
      env.generationPath + `/${job.id}/candidates`,
      call,
    );
    expect(response.status(), await response.text()).toBe(200);
    const rows: VideoCandidate[] = await response.json();
    expect(rows).toHaveLength(1);
    candidate = rows[0];
    expect(candidate.media_check?.status).toBe("PASSED");
    expect(candidate.asset_version_id).toBeTruthy();
  }).toPass({ timeout: 20000 });
  const content = await page.request.get(
    "/api/v1" + candidate.content_path,
    call,
  );
  expect(content.status()).toBe(200);
  const bytes = await content.body();
  expect(checksum(bytes)).toBe(candidate.version!.checksum);
  return {
    ...env,
    root,
    candidate,
    frame,
    bytes,
    job,
    shot,
    open: async () => dialog,
  };
}

// Simulated response faults are intentionally separate from the real API path.
export async function mockedVideoFixture(page: Page) {
  const env = await canvasMediaFixture(page);
  const bytes = await readFile(resolve("tests/fixtures/timeline-preview.mp4"));
  const asset: VideoAsset = {
    id: crypto.randomUUID(),
    project_id: env.project.id,
    revision: 7,
    lock_version: 2,
    status: "ACTIVE",
    current_version_id: null,
  };
  const make = (versionNo: number): VideoCandidate => {
    const id = crypto.randomUUID(),
      versionId = crypto.randomUUID(),
      jobId = crypto.randomUUID(),
      manifestId = crypto.randomUUID(),
      attemptId = crypto.randomUUID();
    return {
      id,
      project_id: env.project.id,
      job_id: jobId,
      attempt_id: attemptId,
      input_manifest_id: manifestId,
      source: "GENERATED",
      source_file_id: null,
      replaces_candidate_id: null,
      asset_version_id: versionId,
      media_kind: "VIDEO",
      required_review: "VIDEO_TEMPORAL",
      asset_revision: asset.revision,
      version: {
        id: versionId,
        asset_id: asset.id,
        version_no: versionNo,
        checksum: checksum(bytes),
        created_by: crypto.randomUUID(),
        created_at: "2026-11-18T00:00:00Z",
        status: "DRAFT",
        stale: false,
        revision: 1,
        archived_from_status: null,
        media: null,
        asset_snapshot: null,
        restored_from_version_id: null,
        restoration_note: null,
        provenance: null,
      },
      content_path: `/projects/${env.project.id}/generation-candidates/${id}/content`,
      video_source: {
        schema_version: "shot-video-source/1",
        artifact_id: crypto.randomUUID(),
        source_file_id: crypto.randomUUID(),
        generation_manifest_id: manifestId,
        generation_manifest_sha256: "a".repeat(64),
        storyboard_id: env.sourceBoards[0].id,
        shot_id: crypto.randomUUID(),
        video_images: [
          {
            asset_version_id: env.image.resource_id,
            checksum: env.image.checksum,
          },
        ],
        job_id: jobId,
        attempt_id: attemptId,
      },
      production: {
        provider: "custom",
        model: "synthetic-video-model",
        model_config_version: "video01-mock/1",
        prompt_version: "video01-mock/1",
        prompt: "模拟协议：女孩在桥边回头。",
      },
      media_check: {
        schema_version: "candidate-video-check/1",
        status: "PASSED",
        candidate_id: id,
        input_manifest_id: manifestId,
        manifest_sha256: "a".repeat(64),
        references_valid: true,
        error_code: null,
        video: {
          checksum: checksum(bytes),
          media_type: "video/mp4",
          byte_size: bytes.length,
          width: 64,
          height: 64,
          duration_ms: 2000,
          frame_rate: 24,
          codec: "h264",
          decoded_frames: 48,
          has_audio: true,
          full_decode_verified: true,
        },
      },
      admission: {
        schema_version: "video-review-summary/1",
        status: "BLOCKED",
        blocked_code: "VIDEO_REVIEW_REQUIRED",
        report_id: null,
        conclusion: null,
        report_sha256: null,
        sampled_frame_count: null,
        coverage_start_ms: null,
        coverage_end_ms: null,
        limitations: [],
      },
      actions: {
        can_submit: false,
        can_approve: false,
        can_reject: true,
        can_select: false,
        can_lock: false,
        can_unlock: false,
        can_replace: true,
        can_restore: true,
        can_start_review: false,
        can_decide_review: false,
        can_revise: false,
        can_lip_sync: false,
      },
    };
  };
  const first = make(1),
    second = make(2),
    candidates = [first, second];
  const nodes: GraphNode[] = candidates.map((row) => ({
    id: "asset-version:" + row.asset_version_id,
    kind: "asset_version",
    resource_id: row.asset_version_id!,
    asset_id: asset.id,
    asset_type: "VIDEO",
    title: "合成视频 " + row.version!.version_no,
    status: "DRAFT",
    version_no: row.version!.version_no,
    checksum: row.version!.checksum,
    preview_kind: "VIDEO",
    candidate_id: row.id,
    preview_path: row.content_path!,
  }));
  env.graph.nodes.push(...nodes);
  const control: {
    contentStatus?: number;
    corrupt?: boolean;
    detailGate?: Promise<void>;
    mediaGate?: Promise<void>;
    failRefresh?: boolean;
    wrongProject?: boolean;
    failReplace?: boolean;
  } = {};
  const reads: string[] = [],
    mutations: { path: string; body: Record<string, unknown> }[] = [],
    uploads: { path: string; body: Record<string, unknown> }[] = [];
  const uploadedFileId = crypto.randomUUID();
  const uploadPath = `/api/v1/projects/${env.project.id}/uploads`;
  await page.route("**/api/v1/**", async (route) => {
    const path = new URL(route.request().url()).pathname;
    if (
      route.request().method() === "POST" &&
      [uploadPath, uploadPath + "/local"].includes(path)
    ) {
      const body = route.request().postDataJSON();
      uploads.push({ path, body });
      if (path === uploadPath)
        return route.fulfill({
          status: 503,
          json: {
            error: {
              code: "PRESIGNED_UNAVAILABLE",
              message: "文件直传需要启用私有 S3 存储",
            },
          },
        });
      const content = Buffer.from(body.content_base64, "base64");
      return route.fulfill({
        status: 201,
        json: {
          id: uploadedFileId,
          project_id: env.project.id,
          asset_version_id: null,
          checksum: checksum(content),
          byte_size: content.length,
          media_type: body.media_type,
          original_name: body.original_name,
          source_note: body.source_note,
          rights_note: body.rights_note,
          rights_confirmed: body.rights_confirmed,
          created_by: crypto.randomUUID(),
          created_at: "2026-11-18T00:00:00Z",
          revision: 1,
          stage: "CANDIDATE",
        },
      });
    }
    if (path === `/api/v1/projects/${env.project.id}/assets/${asset.id}`)
      return route.fulfill({ json: asset });
    const row = candidates.find((item) =>
      path.startsWith(
        `/api/v1/projects/${env.project.id}/generation-candidates/${item.id}`,
      ),
    );
    if (!row) return route.fallback();
    reads.push(path);
    if (path.endsWith("/content")) {
      if (control.mediaGate) await control.mediaGate;
      if (control.contentStatus)
        return route.fulfill({
          status: control.contentStatus,
          json: {
            error: { code: "FORBIDDEN", message: "视频读取权限已撤回。" },
          },
        });
      return route.fulfill({
        contentType: "video/mp4",
        body: control.corrupt ? Buffer.from("corrupted-video") : bytes,
      });
    }
    if (path.endsWith("/replacements")) {
      mutations.push({ path, body: route.request().postDataJSON() });
      if (control.failReplace) return route.abort("failed");
      return route.fulfill({ status: 201, json: second });
    }
    if (control.detailGate && row.id === first.id) await control.detailGate;
    if (control.failRefresh)
      return route.fulfill({
        status: 503,
        json: { error: { code: "UNAVAILABLE", message: "候选刷新暂不可用。" } },
      });
    return route.fulfill({
      json: control.wrongProject
        ? { ...row, project_id: crypto.randomUUID() }
        : row,
    });
  });
  return {
    ...env,
    bytes,
    asset,
    first,
    second,
    videoNodes: nodes,
    controlVideo: control,
    videoReads: reads,
    videoMutations: mutations,
    videoUploads: uploads,
    uploadedVideoFileId: uploadedFileId,
  };
}
