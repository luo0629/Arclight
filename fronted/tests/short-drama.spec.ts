import { expect, test, type Page } from "@playwright/test";
import type { ContentVersion, Episode, Project } from "../src/types/api";

async function createEpisode(page: Page) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `short-drama-${crypto.randomUUID()}@example.test`,
      password: "arclight-short-drama-browser-test",
      display_name: "短剧创作者",
    },
  });
  expect(registration.status()).toBe(201);
  const session = await registration.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const createdProject = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "雨夜短剧" },
  });
  expect(createdProject.status()).toBe(201);
  const project: Project = await createdProject.json();
  const projectApi = `/api/v1/projects/${project.id}`;
  const createdEpisode = await page.request.post(`${projectApi}/episodes`, {
    headers,
    data: { episode_no: 1, title: "纸船的归途" },
  });
  expect(createdEpisode.status()).toBe(201);
  const episode: Episode = await createdEpisode.json();
  return {
    headers,
    project,
    projectApi,
    episodeApi: `/api/v1/episodes/${episode.id}`,
    episodePath: `/projects/${project.id}/episodes/${episode.id}`,
  };
}

test("从空剧集起稿、修改和准备配音后，只确认一次定稿制作", async ({ page }) => {
  const { headers, projectApi, episodeApi, episodePath } =
    await createEpisode(page);
  const speaker = "主角";
  const characterId = crypto.randomUUID();
  const runId = crypto.randomUUID();
  let savedScriptId: string | undefined;
  let savedVoice = false;
  let checkBlocked = true;
  const preflightScripts: (string | null)[] = [];
  const approvals: string[] = [];
  const submissions: { body: Record<string, unknown>; key: string }[] = [];

  page.on("request", (request) => {
    const path = new URL(request.url()).pathname;
    if (
      request.method() === "POST" &&
      /\/api\/v1\/(?:runs\/[^/]+\/resume|episodes\/.*\/review)$/.test(path)
    )
      approvals.push(path);
  });

  // Text drafts and edits use the isolated real API. The
  // media endpoints return synthetic receipts without a provider or user data.
  await page.route(
    `**${episodeApi}/short-drama/preflight?**`,
    async (route) => {
      const script = new URL(route.request().url()).searchParams.get(
        "script_version_id",
      );
      preflightScripts.push(script);
      await route.fulfill({
        json: {
          issues: checkBlocked
            ? [
                {
                  phase: "PLANNING",
                  code: "SHORT_DRAMA_TEXT_SERVICE_REQUIRED",
                  service_kind: "TEXT",
                  supplier: null,
                  model: null,
                },
              ]
            : [],
          needed_voices:
            script === savedScriptId && !savedVoice
              ? [{ speaker, character_asset_id: characterId }]
              : [],
          blocked_voices: [],
        },
      });
    },
  );
  await page.route(`**${projectApi}/audio-voice-catalog`, (route) =>
    route.fulfill({
      json: {
        status: "READY",
        supplier: "测试供应商",
        model: "test-tts",
        configuration_version: "test-config:1",
        options: [{ id: "test-voice", label: "测试音色", description: null }],
        supported_parameters: ["voice"],
        parameter_ranges: {},
        error_code: null,
      },
    }),
  );
  await page.route(`**${projectApi}/audio-voices**`, async (route) => {
    if (route.request().method() === "GET") {
      await route.fulfill({ json: [] });
      return;
    }
    const body = route.request().postDataJSON();
    expect(body).toMatchObject({
      speaker,
      character_asset_id: characterId,
      expected_version_no: 0,
      model_config_version: "test-config:1",
      parameters: { voice: "test-voice" },
    });
    savedVoice = true;
    await route.fulfill({
      status: 201,
      json: { ...body, id: crypto.randomUUID(), version_no: 1 },
    });
  });
  await page.route(`**${episodeApi}/short-drama`, async (route) => {
    const request = route.request();
    if (request.method() === "GET") {
      await route.fulfill({
        json: submissions.length
          ? {
              run_id: runId,
              status: "RUNNING",
              phase: "PLANNING",
              error_code: null,
              issue: null,
              revision: 1,
              can_retry_balance: false,
            }
          : null,
      });
      return;
    }
    submissions.push({
      body: request.postDataJSON(),
      key: request.headers()["idempotency-key"],
    });
    await route.fulfill({
      status: 202,
      json: { run_id: runId, status: "QUEUED" },
    });
  });

  await page.goto(episodePath);
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  const brief = "雨夜在桥边发现纸船，主角想把它送回失主。";
  await page.getByLabel("故事灵感", { exact: true }).fill(brief);
  const draftResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${episodeApi}/script-runs` &&
      response.request().method() === "POST",
  );
  await page
    .getByRole("button", { name: "请编剧开始创作", exact: true })
    .click();
  const accepted = await draftResponse;
  expect(accepted.status()).toBe(202);
  expect(accepted.request().postDataJSON()).toMatchObject({
    story: brief,
    purpose: "EDITABLE_DRAFT",
  });
  await expect(page.locator(".task .status")).toHaveText("已完成", {
    timeout: 20000,
  });
  await expect(
    page.getByRole("button", { name: "批准并继续", exact: true }),
  ).toHaveCount(0);
  const scripts: ContentVersion[] = await (
    await page.request.get(`${episodeApi}/scripts`, { headers })
  ).json();
  expect(scripts).toHaveLength(1);
  expect(scripts[0].status).toBe("READY_FOR_REVIEW");
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "演示剧本（Mock）" })
    .click();

  const production = page.getByRole("region", {
    name: "短剧制作",
    exact: true,
  });
  const confirm = production.getByRole("button", {
    name: "确认剧本并生成短剧",
    exact: true,
  });
  await expect(production.getByLabel(/费用上限|项目预算/)).toHaveCount(0);
  const rights = production.getByRole("checkbox", {
    name: "我确认本次生成的画面和配音具备用于此短剧的授权。",
    exact: true,
  });
  await expect(confirm).toBeDisabled();
  await rights.check();
  const blocker = production.getByRole("alert").filter({
    hasText: "请配置用于制作规划和分镜的文字模型。",
  });
  await expect(blocker).toHaveText(/制作规划 · 文字模型/);
  await expect(blocker).toContainText("模型服务配置");
  await expect(confirm).toBeDisabled();
  expect(submissions).toHaveLength(0);
  checkBlocked = false;
  await production
    .getByRole("button", { name: "重新检查制作准备", exact: true })
    .click();
  await expect(blocker).toHaveCount(0);
  await expect(confirm).toBeEnabled();

  await page
    .getByRole("button", { name: "编辑并另存新版本", exact: true })
    .click();
  await page.getByLabel("剧本标题", { exact: true }).fill("纸船归家 · 定稿");
  await expect(
    production.getByText("请先保存剧本修改，再确认制作。", { exact: true }),
  ).toBeVisible();
  await expect(confirm).toBeDisabled();
  await page.getByRole("button", { name: "添加对白", exact: true }).click();
  await page.getByLabel("说话角色", { exact: true }).selectOption(speaker);
  await page.getByLabel("对白", { exact: true }).fill("我会送你回家。");
  const saveResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === `${episodeApi}/scripts` &&
      response.request().method() === "POST",
  );
  await page.route(`**${episodeApi}/scripts`, async (route) => {
    if (route.request().method() !== "POST") return route.fallback();
    const result = await route.fetch();
    const saved: ContentVersion = await result.json();
    savedScriptId = saved.id;
    await route.fulfill({ response: result, json: saved });
  });
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  const saved = await saveResponse;
  expect(saved.status()).toBe(201);
  expect(savedScriptId).toBeTruthy();
  expect(savedScriptId).not.toBe(scripts[0].id);
  await expect(production.getByText(/使用剧本 v2/)).toBeVisible();
  await rights.check();
  await expect(production.getByLabel("字幕字体", { exact: true })).toHaveCount(
    0,
  );
  await expect(production.getByText(/自动分配/)).toBeVisible();
  await expect(confirm).toBeEnabled();

  await production.getByText("手动选择音色", { exact: true }).click();
  const voice = production.getByRole("region", {
    name: `${speaker}的音色`,
    exact: true,
  });
  await voice.getByText(`为${speaker}保存新音色`, { exact: true }).click();
  await voice.getByLabel("供应商音色", { exact: true }).click();
  await page.getByRole("option", { name: "测试音色", exact: true }).click();
  await voice
    .getByRole("button", { name: "保存新音色版本", exact: true })
    .click();
  await expect(voice).toHaveCount(0);
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect(
    page.getByRole("region", { name: "短剧制作进度", exact: true }),
  ).toContainText("正在规划制作");
  expect(preflightScripts).toContain(scripts[0].id);
  expect(
    preflightScripts.filter((id) => id === savedScriptId).length,
  ).toBeGreaterThanOrEqual(2);
  expect(submissions).toEqual([
    {
      body: {
        script_version_id: savedScriptId,
        generated_media_rights_confirmed: true,
        impact_digest: expect.stringMatching(/^[a-f0-9]{64}$/),
      },
      key: expect.stringMatching(/^[a-f0-9-]{36}$/),
    },
  ]);
  expect(approvals).toEqual([]);
});

test("短剧进度显示不兼容模型，并引导核对原单或余额恢复", async ({ page }) => {
  const { episodeApi, episodePath } = await createEpisode(page);
  const runId = crypto.randomUUID();
  const generationId = crypto.randomUUID();
  const retryPath = `/api/v1/runs/${runId}/short-drama/retry`;
  const retryRequests: unknown[] = [];
  let issue = {
    phase: "SHOTS",
    code: "SHORT_DRAMA_FIRST_FRAME_UNSUPPORTED",
    service_kind: "VIDEO",
    supplier: "示例供应商",
    model: "test-video",
    task: { id: generationId, family: "GENERATION" },
  };
  let queued = false;
  await page.route(`**${episodeApi}/short-drama`, (route) => {
    const reconciling = issue.code === "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN";
    return route.fulfill({
      json: {
        run_id: runId,
        status: queued ? "QUEUED" : reconciling ? "RUNNING" : "FAILED",
        phase: queued ? "PLANNING" : reconciling ? "RECONCILING" : "FAILED",
        error_code: queued || reconciling ? null : issue.code,
        issue: queued ? null : issue,
        revision: 7,
        can_retry_balance:
          !queued && issue.code === "ACCOUNT_BALANCE_INSUFFICIENT",
      },
    });
  });
  await page.route(`**${retryPath}`, async (route) => {
    expect(route.request().headers()["idempotency-key"]).toMatch(
      /^[a-f0-9-]{36}$/,
    );
    retryRequests.push(route.request().postDataJSON());
    queued = true;
    await route.fulfill({
      status: 202,
      json: { run_id: runId, status: "QUEUED" },
    });
  });

  await page.goto(episodePath);
  const progress = page.getByRole("region", {
    name: "短剧制作进度",
    exact: true,
  });
  const diagnosis = progress.getByRole("alert");
  await expect(diagnosis).toContainText(
    "镜头制作 · 视频模型 · 示例供应商 · test-video",
  );
  await expect(diagnosis).toContainText("不支持使用镜头照片作为首帧");
  await expect(diagnosis).toContainText("模型服务配置");

  issue = { ...issue, code: "SHORT_DRAMA_VISUAL_RESULT_UNKNOWN" };
  await page.reload();
  await expect(diagnosis).toContainText("在供应商后台核对原请求");
  await expect(
    progress.getByRole("link", { name: "查看原任务" }),
  ).toHaveAttribute(
    "href",
    `${episodePath}?view=tasks&generation_job=${generationId}`,
  );
  const resume = progress.getByRole("button", { name: "充值后继续制作" });
  await expect(resume).toHaveCount(0);

  issue = { ...issue, code: "ACCOUNT_BALANCE_INSUFFICIENT" };
  await page.reload();
  await expect(
    diagnosis.getByText("账户余额不足", { exact: true }),
  ).toBeVisible();
  await resume.click();
  await expect(progress).toContainText("正在规划制作");
  expect(retryRequests).toEqual([{ expected_revision: 7 }]);
});
