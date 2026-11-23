import { expect, test } from "@playwright/test";
import type { ContentVersion, Episode, Project } from "../src/types/api";

test("从空剧集起稿、修改和准备配音后，只确认一次定稿制作", async ({ page }) => {
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
  const episodeApi = `/api/v1/episodes/${episode.id}`;
  const episodePath = `/projects/${project.id}/episodes/${episode.id}`;
  const speaker = "主角";
  const characterId = crypto.randomUUID();
  const fontId = crypto.randomUUID();
  const runId = crypto.randomUUID();
  let savedScriptId: string | undefined;
  let savedVoice = false;
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

  // Text drafts, edits and the project budget use the isolated real API. The
  // media endpoints return synthetic receipts without a provider or user data.
  await page.route(
    `**${episodeApi}/short-drama/preflight?**`,
    async (route) => {
      const script = new URL(route.request().url()).searchParams.get(
        "script_version_id",
      );
      preflightScripts.push(script);
      const budgetResponse = await page.request.get(projectApi, { headers });
      expect(budgetResponse.status()).toBe(200);
      const budget: Project = await budgetResponse.json();
      await route.fulfill({
        json: {
          project_budget_limit: budget.budget_limit,
          needed_voices:
            script === savedScriptId && !savedVoice
              ? [{ speaker, character_asset_id: characterId }]
              : [],
          blocked_voices: [],
        },
      });
    },
  );
  await page.route(`**${projectApi}/files?**`, (route) =>
    route.fulfill({
      json: [
        {
          id: fontId,
          project_id: project.id,
          original_name: "字幕字体.ttf",
          media_type: "font/ttf",
          rights_confirmed: true,
        },
      ],
    }),
  );
  await page.route(
    `**${projectApi}/generation-jobs/services/speech/configuration`,
    (route) =>
      route.fulfill({
        json: {
          configured: true,
          message: "已配置语音服务",
          supported_parameters: ["voice"],
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
  const spendingLimit = production.getByLabel("本次制作费用上限（USD）", {
    exact: true,
  });
  const rights = production.getByRole("checkbox", {
    name: "我确认本次生成的画面和配音具备用于此短剧的授权。",
    exact: true,
  });
  await expect(
    production.getByText("请先设置大于零的项目预算。", { exact: true }),
  ).toBeVisible();
  await expect(confirm).toBeDisabled();
  const budgetLink = production.getByRole("link", {
    name: "设置项目预算",
    exact: true,
  });
  await expect(budgetLink).toHaveAttribute(
    "href",
    `/projects/${project.id}?section=settings`,
  );
  await budgetLink.click();
  await page.getByLabel("预算额度", { exact: true }).fill("5");
  await page.getByRole("button", { name: "保存设置", exact: true }).click();
  await expect(page.getByText("设置已保存", { exact: true })).toBeVisible();
  await page.goBack();
  await expect(production.getByText(/^项目预算上限（USD）5/)).toBeVisible();
  await spendingLimit.fill("6");
  await rights.check();
  await expect(
    production.getByText("本次制作费用上限不能超过项目预算。", { exact: true }),
  ).toBeVisible();
  await expect(confirm).toBeDisabled();
  await spendingLimit.fill("3");
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
  await spendingLimit.fill("3");
  await rights.check();
  await production.getByLabel("字幕字体", { exact: true }).selectOption(fontId);
  await expect(confirm).toBeDisabled();

  const voice = production.getByRole("region", {
    name: `${speaker}的音色`,
    exact: true,
  });
  await voice.getByText(`为${speaker}保存新音色`, { exact: true }).click();
  await voice
    .getByLabel("供应商音色名称或 ID", { exact: true })
    .fill("test-voice");
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
        maximum_cost: "3",
        font_file_id: fontId,
        generated_media_rights_confirmed: true,
        impact_digest: expect.stringMatching(/^[a-f0-9]{64}$/),
      },
      key: expect.stringMatching(/^[a-f0-9-]{36}$/),
    },
  ]);
  expect(approvals).toEqual([]);
});
