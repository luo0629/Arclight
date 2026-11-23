import { readFile, writeFile } from "node:fs/promises";
import { expect, test, type Page } from "@playwright/test";
import {
  checksum,
  mockedVideoFixture,
  realVideoFixture,
} from "./video-candidates-helpers";
import { deferred } from "./render-helpers";
import { closeDirectory } from "./workspace-helpers";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("video01-result.json"),
    JSON.stringify(
      {
        title: info.title,
        status: info.status,
        expectedStatus: info.expectedStatus,
        retry: info.retry,
        errors: info.errors,
      },
      null,
      2,
    ),
  );
});

async function observeURLs(page: Page) {
  await page.addInitScript(() => {
    const revoke = URL.revokeObjectURL;
    (window as unknown as { revokedVideoURLs: string[] }).revokedVideoURLs = [];
    URL.revokeObjectURL = (url) => {
      (
        window as unknown as { revokedVideoURLs: string[] }
      ).revokedVideoURLs.push(url);
      revoke(url);
    };
  });
}

test("VIDEO-01模拟回执：替换身份不匹配时保留原提交记录且不切换候选", async ({
  page,
}) => {
  const env = await mockedVideoFixture(page);
  await env.openCanvas(env.videoNodes[0]);
  const detail = page.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  await expect(detail.getByRole("heading", { name: /视频 v1/ })).toBeVisible();
  await detail.getByLabel("替换 MP4 文件", { exact: true }).setInputFiles({
    name: "mismatch.mp4",
    mimeType: "video/mp4",
    buffer: env.bytes,
  });
  await detail
    .getByLabel("视频来源说明", { exact: true })
    .fill("模拟错误候选回执");
  await detail
    .getByLabel("视频使用权说明", { exact: true })
    .fill("测试原创素材");
  await detail
    .getByRole("checkbox", { name: "我确认拥有此视频的使用权", exact: true })
    .check();
  await detail
    .getByRole("button", { name: "上传并另存替换候选", exact: true })
    .click();
  await expect(detail.getByRole("alert")).toContainText(
    "替换视频回执与原候选或上传文件不一致",
  );
  await expect(detail.getByRole("heading", { name: /视频 v1/ })).toBeVisible();
  await expect(detail.getByRole("heading", { name: /视频 v2/ })).toHaveCount(0);
  await expect(
    detail.getByRole("button", { name: "核验原视频替换提交", exact: true }),
  ).toBeDisabled();
  const saved = await page.evaluate(() => {
    const key = Object.keys(sessionStorage).find(
      (value) =>
        value.startsWith("arclight:video-replacement:v1:") &&
        !value.endsWith(":restore"),
    );
    return key ? JSON.parse(sessionStorage.getItem(key)!) : null;
  });
  expect(env.videoUploads.map((upload) => upload.path)).toEqual([
    `/api/v1/projects/${env.project.id}/uploads`,
    `/api/v1/projects/${env.project.id}/uploads/local`,
  ]);
  expect(env.videoUploads[1].body).toEqual({
    ...env.videoUploads[0].body,
    purpose: "VIDEO",
    original_name: "mismatch.mp4",
    media_type: "video/mp4",
    byte_size: env.bytes.length,
    checksum: checksum(env.bytes),
    content_base64: env.bytes.toString("base64"),
    source_note: "模拟错误候选回执",
    rights_note: "测试原创素材",
    rights_confirmed: true,
  });
  expect(saved.file_id).toBe(env.uploadedVideoFileId);
  expect(saved.file_id).toBe(env.videoMutations[0].body.file_id);
  expect(saved.creation_key).toBe(env.videoMutations[0].body.creation_key);
});

test("VIDEO-01：真实候选API、完整H264规格、私有播放下载、替换和历史另存", async ({
  page,
  playwright,
}, info) => {
  test.setTimeout(90000);
  await page.setViewportSize({ width: 1440, height: 1200 });
  const env = await realVideoFixture(page);
  const original = structuredClone(env.candidate);
  expect(original.media_check?.video).toMatchObject({
    codec: "h264",
    width: 1280,
    height: 720,
    duration_ms: 6000,
    frame_rate: 24,
    decoded_frames: 144,
    full_decode_verified: true,
  });
  expect(original.video_source?.video_images[0].asset_version_id).toBe(
    env.frame.id,
  );
  const dialog = await env.open();
  const shot = dialog.getByRole("region", {
    name: "镜头 1 视频候选",
    exact: true,
  });
  await shot
    .getByRole("button", { name: "视频 v1 · DRAFT", exact: true })
    .click();
  const detail = shot.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  await expect(detail).toContainText(
    `已固定 ${original.video_source!.video_images.length} 张首帧／参考图`,
  );
  await expect(detail).not.toContainText(env.frame.id);
  await expect(detail).toContainText("doubao-seedance-1-0-pro-250528");
  await expect(detail).toContainText("女孩提灯走过小桥并回头。");
  await expect(
    detail.getByRole("region", { name: "视频技术检查", exact: true }),
  ).toContainText("完整解码 144 帧");
  for (const name of [
    "提交视频版本审核",
    "批准此视频版本",
    "正式选用此视频",
    "锁定当前视频选用",
  ])
    await expect(
      detail.getByRole("button", { name, exact: true }),
    ).toBeDisabled();
  await detail
    .getByRole("button", { name: "读取视频候选预览", exact: true })
    .click();
  const video = detail.getByLabel("视频候选 v1 预览", { exact: true });
  await expect
    .poll(() =>
      video.evaluate((element: HTMLVideoElement) => element.readyState),
    )
    .toBeGreaterThanOrEqual(1);
  expect(
    await video.evaluate((element: HTMLVideoElement) => ({
      width: element.videoWidth,
      height: element.videoHeight,
      duration: element.duration,
    })),
  ).toEqual({ width: 1280, height: 720, duration: 6 });
  await video.evaluate((element: HTMLVideoElement) => {
    element.muted = true;
    return element.play();
  });
  await expect
    .poll(() =>
      video.evaluate((element: HTMLVideoElement) => element.currentTime),
    )
    .toBeGreaterThan(0);
  await video.evaluate((element: HTMLVideoElement) => element.pause());
  const downloaded = page.waitForEvent("download");
  await detail
    .getByRole("button", { name: "下载候选 MP4", exact: true })
    .click();
  const download = await downloaded;
  const downloadPath = info.outputPath("original-shot.mp4");
  await download.saveAs(downloadPath);
  expect(checksum(await readFile(downloadPath))).toBe(
    original.version!.checksum,
  );
  await video.scrollIntoViewIfNeeded();
  const desktop = info.outputPath("video-candidate-desktop.png");
  await page.screenshot({ path: desktop });
  await info.attach("真实视频候选桌面", {
    path: desktop,
    contentType: "image/png",
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await video.scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const mobile = info.outputPath("video-candidate-mobile.png");
  await page.screenshot({ path: mobile });
  await info.attach("真实视频候选手机", {
    path: mobile,
    contentType: "image/png",
  });
  await page.setViewportSize({ width: 1440, height: 1200 });
  await detail.getByLabel("替换 MP4 文件", { exact: true }).setInputFiles({
    name: "replacement.mp4",
    mimeType: "video/mp4",
    buffer: env.bytes,
  });
  await detail
    .getByLabel("视频来源说明", { exact: true })
    .fill("离线合成视频人工替换");
  await detail
    .getByLabel("视频使用权说明", { exact: true })
    .fill("测试原创合成视频");
  await detail
    .getByRole("checkbox", { name: "我确认拥有此视频的使用权", exact: true })
    .check();
  const uploadedFileResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === env.root + "/uploads/local" &&
      response.request().method() === "POST",
  );
  await detail
    .getByRole("button", { name: "上传并另存替换候选", exact: true })
    .click();
  const uploadResponse = await uploadedFileResponse;
  expect(uploadResponse.status(), await uploadResponse.text()).toBe(201);
  const uploadedFile = await uploadResponse.json();
  const expectedUpload = {
    project_id: env.project.id,
    original_name: "replacement.mp4",
    media_type: "video/mp4",
    byte_size: env.bytes.length,
    checksum: checksum(env.bytes),
    source_note: "离线合成视频人工替换",
    rights_note: "测试原创合成视频",
    rights_confirmed: true,
  };
  expect(uploadedFile).toMatchObject(expectedUpload);
  const savedFile = await page.request.get(
    env.root + `/files/${uploadedFile.id}`,
    { headers: env.headers },
  );
  expect(savedFile.status(), await savedFile.text()).toBe(200);
  expect(await savedFile.json()).toMatchObject({
    ...expectedUpload,
    id: uploadedFile.id,
  });
  await expect(detail).toContainText("视频 v2");
  await expect(detail).toContainText("人工上传替换");
  await expect(detail).toContainText("replacement.mp4");
  await expect(detail).toContainText("原生成任务配置");
  await expect(
    detail.getByRole("button", { name: "正式选用此视频", exact: true }),
  ).toBeDisabled();
  await detail
    .getByLabel("视频版本说明", { exact: true })
    .fill("核对原精确输入后另存历史视频");
  await detail
    .getByRole("button", { name: "从历史视频另存新候选", exact: true })
    .click();
  await expect(
    shot.getByRole("button", { name: "视频 v3 · DRAFT", exact: true }),
  ).toBeVisible();
  await shot
    .getByRole("button", { name: "视频 v3 · DRAFT", exact: true })
    .click();
  await expect(detail).toContainText("历史视频另存");
  await expect(
    detail.getByRole("button", { name: "正式选用此视频", exact: true }),
  ).toBeDisabled();
  const historicalResponse = await page.request.get(
    env.root + `/generation-candidates/${original.id}`,
    { headers: env.headers },
  );
  expect(historicalResponse.status()).toBe(200);
  const historical = await historicalResponse.json();
  expect(historical.version).toEqual(original.version);
  expect(historical.video_source).toEqual(original.video_source);
  const listed = await page.request.get(
    env.generationPath + `/${env.job.id}/candidates`,
    { headers: env.headers },
  );
  const candidates = await listed.json();
  expect(
    candidates.map(
      (row: { version: { status: string } }) => row.version.status,
    ),
  ).toEqual(["DRAFT", "DRAFT", "DRAFT"]);
  expect(candidates).toContainEqual(
    expect.objectContaining({
      source_file_id: uploadedFile.id,
      replaces_candidate_id: original.id,
    }),
  );
  const outsider = await playwright.request.newContext({
    baseURL: new URL(page.url()).origin,
  });
  try {
    const registered = await outsider.post("/api/v1/auth/register", {
      data: {
        email: `foreign-video-${crypto.randomUUID()}@example.com`,
        password: "foreign-video-test-password",
        display_name: "无权读取视频",
      },
    });
    expect(registered.status()).toBe(201);
    const token = (await registered.json()).access_token;
    const denied = await outsider.get("/api/v1" + original.content_path, {
      headers: { Authorization: "Bearer " + token },
    });
    expect(denied.status()).toBe(404);
    expect(await denied.json()).toMatchObject({ error: { code: "NOT_FOUND" } });
  } finally {
    await outsider.dispose();
  }
});

test("VIDEO-01：真实生成与替换响应丢失后沿用原请求和同一幂等提交", async ({
  page,
}) => {
  test.setTimeout(90000);
  const env = await realVideoFixture(page, true);
  const submitted: Record<string, unknown>[] = [];
  const accepted: string[] = [];
  await page.route(
    `**/api/v1/projects/${env.project.id}/generation-candidates/${env.candidate.id}/replacements`,
    async (route) => {
      submitted.push(route.request().postDataJSON());
      const response = await route.fetch();
      expect(response.status(), await response.text()).toBe(201);
      accepted.push((await response.json()).id);
      if (submitted.length === 1) return route.abort("failed");
      await route.fulfill({ response });
    },
  );
  const dialog = await env.open();
  const shot = dialog.getByRole("region", {
    name: "镜头 1 视频候选",
    exact: true,
  });
  await shot
    .getByRole("button", { name: "视频 v1 · DRAFT", exact: true })
    .click();
  const detail = shot.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  await detail.getByLabel("替换 MP4 文件", { exact: true }).setInputFiles({
    name: "recover.mp4",
    mimeType: "video/mp4",
    buffer: env.bytes,
  });
  await detail
    .getByLabel("视频来源说明", { exact: true })
    .fill("受控响应丢失验证");
  await detail.getByLabel("视频使用权说明", { exact: true }).fill("测试原创");
  await detail
    .getByRole("checkbox", { name: "我确认拥有此视频的使用权", exact: true })
    .check();
  const uploadedFileResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === env.root + "/uploads/local" &&
      response.request().method() === "POST",
  );
  await detail
    .getByRole("button", { name: "上传并另存替换候选", exact: true })
    .click();
  const uploadResponse = await uploadedFileResponse;
  expect(uploadResponse.status(), await uploadResponse.text()).toBe(201);
  const uploadedFile = await uploadResponse.json();
  expect(uploadedFile).toMatchObject({
    project_id: env.project.id,
    original_name: "recover.mp4",
    media_type: "video/mp4",
    byte_size: env.bytes.length,
    checksum: checksum(env.bytes),
    source_note: "受控响应丢失验证",
    rights_note: "测试原创",
    rights_confirmed: true,
  });
  await expect(detail.getByRole("alert")).toContainText("网络连接失败");
  await expect(
    detail.getByRole("button", { name: "核验原视频替换提交", exact: true }),
  ).toBeDisabled();
  await detail
    .getByRole("button", { name: "刷新视频候选", exact: true })
    .click();
  await detail
    .getByRole("button", { name: "核验原视频替换提交", exact: true })
    .click();
  await expect(detail).toContainText("视频 v2");
  expect(submitted).toHaveLength(2);
  expect(submitted[0].file_id).toBe(uploadedFile.id);
  expect(submitted[1]).toEqual(submitted[0]);
  expect(accepted[1]).toBe(accepted[0]);
  const response = await page.request.get(
    env.generationPath + `/${env.job.id}/candidates`,
    { headers: env.headers },
  );
  expect(await response.json()).toHaveLength(2);
});

test("VIDEO-01模拟媒体拒绝：任务成功但候选检查失败时保留原任务并明确阻塞", async ({
  page,
}) => {
  test.setTimeout(90000);
  let rejectedReceipt = false;
  await page.route(
    "**/api/v1/projects/*/generation-jobs/*/candidates",
    async (route) => {
      const response = await route.fetch();
      const rows = await response.json();
      if (
        !Array.isArray(rows) ||
        !rows.some((row) => row.media_kind === "VIDEO")
      )
        return route.fulfill({ response });
      rejectedReceipt = true;
      return route.fulfill({
        response,
        json: rows.map((row) => ({
          ...row,
          asset_version_id: null,
          version: null,
          error_code: "INVALID_VIDEO",
          media_check: {
            ...row.media_check,
            status: "FAILED",
            error_code: "INVALID_VIDEO",
            video: null,
          },
          admission: null,
          actions: {},
        })),
      });
    },
  );
  const env = await realVideoFixture(page);
  const form = page.getByRole("region", {
    name: "单镜头视频生成",
    exact: true,
  });
  await expect(form.getByRole("alert")).toContainText(
    "视频结果未通过媒体检查，未登记为可用候选",
  );
  await expect(form.getByRole("status")).toContainText(env.job.id);
  await expect(
    form.getByRole("button", { name: "准备另一个视频任务", exact: true }),
  ).toBeEnabled();
  expect(rejectedReceipt).toBe(true);
  const jobs = await page.request.get(env.generationPath, {
    headers: env.headers,
  });
  const listed = await jobs.json();
  expect(
    listed.filter((row: { task_type: string }) => row.task_type === "VIDEO"),
  ).toHaveLength(1);
});

test("VIDEO-01模拟故障：哈希损坏拒绝播放下载，撤权后清除旧视频和私有详情", async ({
  page,
}) => {
  await observeURLs(page);
  const env = await mockedVideoFixture(page);
  await env.openCanvas(env.videoNodes[0]);
  const detail = page.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  await expect(detail.getByRole("heading", { name: /视频 v1/ })).toBeVisible();
  env.controlVideo.corrupt = true;
  await detail
    .getByRole("button", { name: "读取视频候选预览", exact: true })
    .click();
  await expect(detail.getByRole("alert")).toContainText("视频完整性检查未通过");
  await expect(detail.locator("video")).toHaveCount(0);
  env.controlVideo.corrupt = false;
  await detail
    .getByRole("button", { name: "读取视频候选预览", exact: true })
    .click();
  await expect(detail.locator("video")).toHaveAttribute("src", /^blob:/);
  const oldURL = await detail.locator("video").getAttribute("src");
  env.controlVideo.contentStatus = 403;
  await detail
    .getByRole("button", { name: "下载候选 MP4", exact: true })
    .click();
  await expect(page.locator("video")).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "精确视频候选详情", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () =>
        (window as unknown as { revokedVideoURLs: string[] }).revokedVideoURLs,
    ),
  ).toContain(oldURL);
});

test("VIDEO-01模拟迟到：切换精确候选丢弃旧详情和媒体，跨项目回执停止读取", async ({
  page,
}) => {
  const env = await mockedVideoFixture(page);
  const lateDetail = deferred();
  env.controlVideo.detailGate = lateDetail.promise;
  await env.openCanvas(env.videoNodes[0]);
  await expect(
    page.getByText("正在读取精确视频候选…", { exact: true }),
  ).toBeVisible();
  await env.selectNode(env.videoNodes[1]);
  const detail = page.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  await expect(detail.getByRole("heading", { name: /视频 v2/ })).toBeVisible();
  lateDetail.release();
  await expect(detail.getByRole("heading", { name: /视频 v1/ })).toHaveCount(0);
  env.controlVideo.detailGate = undefined;
  await env.selectNode(env.videoNodes[0]);
  await expect(detail.getByRole("heading", { name: /视频 v1/ })).toBeVisible();
  const lateMedia = deferred();
  env.controlVideo.mediaGate = lateMedia.promise;
  await detail
    .getByRole("button", { name: "读取视频候选预览", exact: true })
    .click();
  await expect
    .poll(
      () => env.videoReads.filter((path) => path.endsWith("/content")).length,
    )
    .toBe(1);
  await env.selectNode(env.videoNodes[1]);
  await expect(detail.getByRole("heading", { name: /视频 v2/ })).toBeVisible();
  lateMedia.release();
  await expect(detail.locator("video")).toHaveCount(0);
  env.controlVideo.mediaGate = undefined;
  env.controlVideo.wrongProject = true;
  await detail
    .getByRole("button", { name: "刷新视频候选", exact: true })
    .click();
  await expect(detail.getByRole("alert")).toContainText(
    "视频候选与当前项目或精确版本不一致",
  );
  await expect(detail.locator("video")).toHaveCount(0);
  await expect(
    detail.getByRole("button", { name: "正式选用此视频", exact: true }),
  ).toBeDisabled();
});

async function admitVideoInWorkspace(
  page: Page,
  env: Awaited<ReturnType<typeof realVideoFixture>>,
) {
  const shot = (await env.open()).getByRole("region", {
    name: "镜头 1 视频候选",
    exact: true,
  });
  await shot
    .getByRole("button", { name: "视频 v1 · DRAFT", exact: true })
    .click();
  const detail = shot.getByRole("region", {
    name: "精确视频候选详情",
    exact: true,
  });
  const panel = detail.getByRole("region", {
    name: "视频时序审查准入",
    exact: true,
  });
  const endpoint = `/api/v1${env.candidate.content_path?.replace(/\/content$/, "")}`;
  await panel
    .getByRole("button", { name: "提交视频时序审查", exact: true })
    .click();
  await expect(async () => {
    const response = await page.request.get(endpoint, { headers: env.headers });
    expect(response.status()).toBe(200);
    expect((await response.json()).admission.blocked_code).toBe(
      "HUMAN_DECISION_REQUIRED",
    );
  }).toPass({ timeout: 20000 });
  await detail
    .getByRole("button", { name: "刷新视频候选", exact: true })
    .click();
  await expect(panel).toContainText("采样 8 帧");
  await expect(panel).toContainText("HUMAN_DECISION_REQUIRED");
  await panel.getByText("时间码与取证记录", { exact: true }).click();
  await expect(
    panel.getByText("5958 ms · 媒体沙箱取得的可解码帧", { exact: true }),
  ).toBeVisible();
  await detail
    .getByRole("button", { name: "读取视频候选预览", exact: true })
    .click();
  await expect(detail.locator("video")).toBeVisible();
  const queue = panel.getByRole("region", {
    name: "精确版本人工处理",
    exact: true,
  });
  await expect(queue).toContainText(env.candidate.asset_version_id!);
  await queue
    .getByLabel("人工处理意见（必填）")
    .fill("已观看完整离线合成候选，接受抽样覆盖限制。");
  await queue
    .getByRole("button", { name: "接受本次例外", exact: true })
    .click();
  await expect(
    queue.getByRole("button", { name: "恢复原执行", exact: true }),
  ).toBeEnabled();
  await queue.getByRole("button", { name: "恢复原执行", exact: true }).click();
  await expect(panel).toContainText("审查已通过准入 · HUMAN_REVIEW");
  await expect(
    shot.getByRole("button", { name: "视频 v1 · DRAFT", exact: true }),
  ).toBeVisible();
  return detail;
}

test("VIDEO-02：视频时序取证、精确人工队列及独立版本门禁", async ({ page }) => {
  test.setTimeout(90000);
  await admitVideoInWorkspace(page, await realVideoFixture(page));
});

test("VIDEO-03：工作区选用视频、另存裁切和原声并播放真实预览及正式成片", async ({
  page,
}, info) => {
  test.setTimeout(120000);
  const env = await realVideoFixture(page, false, true);
  expect(env.candidate.media_check?.video?.has_audio).toBe(true);
  const detail = await admitVideoInWorkspace(page, env);
  for (const name of ["提交视频版本审核", "批准此视频版本", "正式选用此视频"]) {
    const action = detail.getByRole("button", { name, exact: true });
    await expect(action).toBeEnabled();
    await action.click();
  }
  await expect(detail).toContainText("已正式选用此精确视频版本");
  const call = { headers: env.headers };
  const prefix = `/api/v1/episodes/${env.episode.id}`;
  await page.goto(
    `/projects/${env.project.id}/episodes/${env.episode.id}?view=render`,
  );
  await closeDirectory(page);
  const panel = page.getByRole("region", { name: "渲染与成片", exact: true });
  await panel.getByText("从已批准分镜创建默认时间轴", { exact: true }).click();
  await panel
    .getByLabel("已批准分镜版本", { exact: true })
    .selectOption(env.board.id);
  await panel
    .getByRole("button", { name: "核对当前选用镜头", exact: true })
    .click();
  await expect(
    panel.getByRole("region", { name: "默认时间轴镜头核对" }),
  ).toContainText(env.candidate.asset_version_id!);
  await panel
    .getByRole("checkbox", {
      name: "我已核对全部镜头及精确素材版本",
      exact: true,
    })
    .check();
  const created = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === prefix + "/timelines" &&
      response.request().method() === "POST",
  );
  await panel
    .getByRole("button", { name: "创建默认时间轴", exact: true })
    .click();
  const original = await (await created).json();
  expect(original.snapshot.tracks[0].clips[0].video.keep_audio).toBe(false);

  await panel.getByText("时间轴编辑器", { exact: true }).click();
  const editor = panel.getByRole("region", {
    name: "时间轴编辑器",
    exact: true,
  });
  await editor
    .getByRole("button", { name: /画面片段 1，0 至 3000 毫秒/ })
    .click();
  await editor.getByLabel("源视频入点（毫秒）", { exact: true }).fill("1000");
  await editor.getByLabel("片段时长（毫秒）", { exact: true }).fill("1500");
  await editor
    .getByRole("checkbox", { name: "保留视频原声", exact: true })
    .check();
  await expect(editor).toContainText("出点 2500 毫秒");
  const saved = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `/api/v1/timelines/${original.id}` &&
      response.request().method() === "PATCH",
  );
  await editor
    .getByRole("button", { name: "保存时间轴新版本", exact: true })
    .click();
  const revised = await (await saved).json();
  expect(revised.based_on_timeline_id).toBe(original.id);
  expect(revised.snapshot.tracks[0].clips[0]).toMatchObject({
    source_asset_version_id: env.candidate.asset_version_id,
    duration_ms: 1500,
    source_start_ms: 1000,
    video: { source_end_ms: 2500, keep_audio: true, frame_rate: 24 },
  });

  async function startRender(manifestId: string) {
    const response = await page.request.post(
      `/api/v1/render-manifests/${manifestId}/jobs`,
      {
        ...call,
        data: { creation_key: crypto.randomUUID() },
      },
    );
    expect(response.status(), await response.text()).toBe(202);
    let job = await response.json();
    await expect(async () => {
      const read = await page.request.get(
        `/api/v1/render-jobs/${job.id}`,
        call,
      );
      expect(read.status()).toBe(200);
      job = await read.json();
      expect(job.status, JSON.stringify(job)).toBe("SUCCEEDED");
    }).toPass({ timeout: 30_000 });
    return job;
  }
  const manifestPath = `/api/v1/timelines/${revised.id}/render-manifests`;
  const previewCreated = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === manifestPath &&
      response.request().method() === "POST",
  );
  await panel
    .getByRole("button", { name: "创建预览清单", exact: true })
    .click();
  const preview = await (await previewCreated).json();
  const previewJob = await startRender(preview.id);
  const formal = await page.request.post(manifestPath, {
    ...call,
    data: { creation_key: crypto.randomUUID(), profile: "FORMAL" },
  });
  expect(formal.status(), await formal.text()).toBe(201);
  const frozen = await formal.json();
  const reviewResponse = await page.request.post(
    `/api/v1/render-manifests/${frozen.id}/export-review`,
    call,
  );
  expect(reviewResponse.status(), await reviewResponse.text()).toBe(201);
  const review = await reviewResponse.json();
  const accepted = await page.request.post(
    `/api/v1/human-reviews/${review.id}/decisions`,
    {
      ...call,
      data: {
        version_id: review.snapshot.version_id,
        request_sha256: review.request_sha256,
        decision: "APPROVE",
        comment: "验收合成视频的精确裁切与保留原声。",
      },
    },
  );
  expect(accepted.status(), await accepted.text()).toBe(200);
  const resumed = await page.request.post(
    `/api/v1/human-reviews/${review.id}/resume`,
    {
      ...call,
      data: {
        request_sha256: review.request_sha256,
        decision_id: (await accepted.json()).id,
      },
    },
  );
  expect(resumed.status(), await resumed.text()).toBe(200);
  const formalJob = await startRender(frozen.id);
  expect(formalJob.is_delivery).toBe(false);
  const report = await page.request.get(
    prefix + `/deliveries/${formalJob.artifact_id}/report`,
    call,
  );
  expect(report.status(), await report.text()).toBe(200);
  const result = (await report.json()).report.details;
  expect(result.manifest_sha256).toBe(frozen.content_sha256);
  expect(result.warnings).toEqual([
    expect.objectContaining({
      code: "MEDIA_EXCEPTION",
      source_version_id: env.candidate.asset_version_id,
      human_review_decision_id:
        revised.snapshot.tracks[0].clips[0].video.human_decision_id,
    }),
  ]);

  await expect(panel.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    revised.id,
  );
  await panel.getByText("时间轴编辑器", { exact: true }).click();
  await expect(
    editor.getByRole("heading", { name: "时间轴 v2" }),
  ).toBeVisible();
  await editor
    .getByRole("button", { name: "刷新时间轴预览", exact: true })
    .click();
  const savedPreview = editor.getByRole("region", {
    name: "时间轴保存版本预览",
  });
  const outputs = savedPreview.getByLabel("时间轴预览成片", { exact: true });
  await expect(outputs.locator("option")).toHaveCount(2);
  for (const job of [previewJob, formalJob]) {
    await outputs.selectOption(job.artifact_id);
    await expect(savedPreview).toHaveAttribute("aria-busy", "false");
    const player = savedPreview.getByRole("region", {
      name: "时间轴成片预览与下载",
      exact: true,
    });
    const receivedContent = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname ===
        `${prefix}/deliveries/${job.artifact_id}/content`,
    );
    await player
      .getByRole("button", { name: "读取时间轴成片预览", exact: true })
      .click();
    expect((await receivedContent).status()).toBe(200);
    const media = player.locator("video");
    await expect(media).toBeVisible();
    await expect
      .poll(() => media.evaluate((video: HTMLVideoElement) => video.readyState))
      .toBeGreaterThanOrEqual(1);
    const dimensions = await media.evaluate((video: HTMLVideoElement) => ({
      width: video.videoWidth,
      height: video.videoHeight,
      duration: video.duration,
    }));
    expect(dimensions).toMatchObject({ width: 320, height: 180 });
    expect(dimensions.duration).toBeCloseTo(1.5, 1);
    await media.evaluate(async (video: HTMLVideoElement) => {
      video.muted = true;
      await video.play();
    });
    await expect
      .poll(() =>
        media.evaluate((video: HTMLVideoElement) => video.currentTime),
      )
      .toBeGreaterThan(0);
  }
  await editor.screenshot({ path: info.outputPath("video03-timeline.png") });
});
