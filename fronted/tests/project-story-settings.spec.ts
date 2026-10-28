import { test, expect, type Page } from "@playwright/test";
import {
  openWorkspacePanels,
  chooseProject,
  openDirectory,
} from "./workspace-helpers";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "story-brief-" + crypto.randomUUID() + "@example.com",
      password: "story-brief-test-password",
      display_name: "故事设定验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "业务设定验收",
      style_guide: { description: "水彩" },
      output_profile: { width: 1080, height: 1920, fps: 30 },
    },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const path = "/api/v1/projects/" + project.id;
  const createdEpisode = await page.request.post(path + "/episodes", {
    headers,
    data: { episode_no: 1, title: "原有剧集" },
  });
  expect(createdEpisode.status()).toBe(201);
  return { path, headers, episode: await createdEpisode.json() };
}
async function openSettings(page: Page) {
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(
    page.getByRole("heading", { name: "项目业务设定", exact: true }),
  ).toBeVisible();
}
const save = (page: Page) =>
  page.getByRole("button", { name: "保存业务设定", exact: true });

test("STORY-01：业务字段持久化、新剧集继承默认时长、旧剧集不变", async ({
  page,
}, testInfo) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { path, headers, episode } = await setup(page);
  await openSettings(page);
  await expect(
    page.getByLabel("默认单集时长（秒）", { exact: true }),
  ).toHaveValue("30");
  await page.getByLabel("项目简介", { exact: true }).fill("旧书店的一封信");
  await page.getByLabel("题材", { exact: true }).fill("温暖悬疑");
  await page.getByLabel("目标受众", { exact: true }).fill("青少年及家长");
  await page.getByLabel("内容限制", { exact: true }).fill("避免血腥画面");
  await page
    .getByLabel("分集规划输入", { exact: true })
    .fill("第一集发现信，第二集寻找作者。");
  await page.getByLabel("默认单集时长（秒）", { exact: true }).fill("45.001");
  await page.getByLabel("目标集数", { exact: true }).fill("12");
  await save(page).click();
  await expect(
    page
      .getByRole("region", { name: "项目业务设定", exact: true })
      .getByRole("status"),
  ).toContainText("项目业务设定已保存");
  const persisted = await (await page.request.get(path, { headers })).json();
  expect(persisted.story_settings).toEqual({
    schema_version: 1,
    description: "旧书店的一封信",
    genre: "温暖悬疑",
    target_audience: "青少年及家长",
    content_restrictions: "避免血腥画面",
    episode_plan: "第一集发现信，第二集寻找作者。",
    default_episode_duration_ms: 45001,
    target_episode_count: 12,
  });
  expect(persisted.style_guide).toEqual({ description: "水彩" });
  expect(persisted.output_profile).toEqual({
    width: 1080,
    height: 1920,
    fps: 30,
  });
  expect(
    (
      await (
        await page.request.get("/api/v1/episodes/" + episode.id, { headers })
      ).json()
    ).target_duration_ms,
  ).toBe(30000);
  await openSettings(page);
  await expect(page.getByLabel("题材", { exact: true })).toHaveValue(
    "温暖悬疑",
  );
  await expect(
    page.getByLabel("默认单集时长（秒）", { exact: true }),
  ).toHaveValue("45.001");
  await page.screenshot({
    path: testInfo.outputPath("story-settings-desktop.png"),
    fullPage: true,
  });
  await openDirectory(page);
  await page.getByRole("button", { name: "＋ 添加一集", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByLabel("目标时长（秒）", { exact: true }),
  ).toHaveValue("45.001");
  await dialog.getByLabel("剧集标题", { exact: true }).fill("新的剧集");
  await dialog.getByRole("button", { name: "创建剧集", exact: true }).click();
  await expect(dialog).toBeHidden();
  const episodes = await (
    await page.request.get(path + "/episodes", { headers })
  ).json();
  expect(
    episodes.find((row: { title: string }) => row.title === "新的剧集")
      .target_duration_ms,
  ).toBe(45001);
  expect(episodes).toHaveLength(2);
  expect(crashes).toEqual([]);
});

test("STORY-01：失败保留、活动任务拒绝、取消重试、提交防重和归档只读", async ({
  page,
}) => {
  const { path, headers, episode } = await setup(page);
  await openSettings(page);
  const genre = page.getByLabel("题材", { exact: true });
  await genre.fill("失败仍保留");
  await page.route("**" + path, (route) =>
    route.request().method() === "PATCH"
      ? route.abort("failed")
      : route.continue(),
  );
  await save(page).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
  await expect(genre).toHaveValue("失败仍保留");
  await page.unroute("**" + path);
  const started = await page.request.post(
    "/api/v1/episodes/" + episode.id + "/script-runs",
    {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: { story: "一封测试信" },
    },
  );
  expect(started.status()).toBe(202);
  const run = await started.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/v1/runs/" + run.run_id, { headers })
          ).json()
        ).status,
    )
    .toBe("WAITING_REVIEW");
  await save(page).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("请先结束或取消正在进行的工作流");
  await expect(genre).toHaveValue("失败仍保留");
  expect(
    (
      await page.request.post("/api/v1/runs/" + run.run_id + "/cancel", {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let patches = 0;
  await page.route("**" + path, async (route) => {
    if (route.request().method() === "PATCH") {
      patches++;
      await gate;
    }
    await route.continue();
  });
  await save(page).click();
  await expect(
    page.getByRole("button", { name: "保存中…", exact: true }),
  ).toBeDisabled();
  await expect(genre).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "保存风格与画幅", exact: true }),
  ).toBeDisabled();
  release();
  await expect(
    page
      .getByRole("region", { name: "项目业务设定", exact: true })
      .getByRole("status"),
  ).toContainText("项目业务设定已保存");
  expect(patches).toBe(1);
  await page.unroute("**" + path);
  await genre.fill("未保存");
  await page.getByRole("button", { name: "恢复已保存的业务设定" }).click();
  await expect(genre).toHaveValue("失败仍保留");
  expect(
    (
      await page.request.patch(path, { headers, data: { archived: true } })
    ).ok(),
  ).toBeTruthy();
  await openSettings(page);
  await expect(save(page)).toBeDisabled();
  await expect(page.getByLabel("项目简介", { exact: true })).toBeDisabled();
});

test("STORY-01：边界校验、项目切换隔离及窄屏布局", async ({
  page,
}, testInfo) => {
  const { headers } = await setup(page);
  const other = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "独立业务项目",
      story_settings: { genre: "科幻", default_episode_duration_ms: 60000 },
    },
  });
  expect(other.status()).toBe(201);
  await openSettings(page);
  await chooseProject(page, { name: "业务设定验收" });
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await page.getByLabel("题材", { exact: true }).fill("不应串到别的项目");
  const count = page.getByLabel("目标集数", { exact: true });
  await count.fill("0");
  await save(page).click();
  expect(
    await count.evaluate((node: HTMLInputElement) => node.validity.valid),
  ).toBe(false);
  await count.fill("1001");
  await save(page).click();
  expect(
    await count.evaluate((node: HTMLInputElement) => node.validity.valid),
  ).toBe(false);
  await page.getByRole("button", { name: "恢复已保存的业务设定" }).click();
  await chooseProject(page, { name: "独立业务项目" });
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("题材", { exact: true })).toHaveValue("科幻");
  await expect(
    page.getByLabel("默认单集时长（秒）", { exact: true }),
  ).toHaveValue("60");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("story-settings-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
});
