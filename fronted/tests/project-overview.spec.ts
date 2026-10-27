import { test, expect, type Page } from "@playwright/test";
import { chooseProject, closeDirectory } from "./workspace-helpers";

async function setup(page: Page, count = 1) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `overview-${crypto.randomUUID()}@example.com`,
      password: "overview-test-password",
      display_name: "总览验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "项目总览验收", budget_limit: "125.50" },
    })
  ).json();
  const episodes = [];
  for (let n = 1; n <= count; n++) {
    const response = await page.request.post(
      `/api/v1/projects/${project.id}/episodes`,
      { headers, data: { episode_no: n, title: `总览剧集${n}` } },
    );
    expect(response.status()).toBe(201);
    episodes.push(await response.json());
  }
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  return {
    headers,
    project,
    episodes,
    owner,
    url: `/api/v1/projects/${project.id}/overview`,
  };
}
async function open(page: Page) {
  await page.goto("/");
  await closeDirectory(page);
  await expect(
    page.getByRole("button", { name: "项目总览", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "项目总览", exact: true }).click();
}
const surface = (page: Page) =>
  page.getByLabel("项目总览数据", { exact: true });

test("aggregates actual versions and tasks, refreshes reviews, opens tasks, and fits mobile", async ({
  page,
}, info) => {
  const env = await setup(page, 2);
  const saved = await page.request.post(
    `/api/v1/episodes/${env.episodes[0].id}/scripts`,
    {
      headers: env.headers,
      data: {
        expected_version_no: 0,
        content: {
          title: "人工来源",
          summary: "雨夜",
          estimated_duration_ms: 30000,
          characters: ["女孩"],
          scenes: [
            { scene_no: 1, title: "相遇", action: "推开门", dialogue: [] },
          ],
        },
      },
    },
  );
  expect(saved.status()).toBe(201);
  const version = await saved.json();
  const started = await page.request.post(
    `/api/v1/episodes/${env.episodes[1].id}/script-runs`,
    {
      headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
      data: { story: "雨夜，女孩走进书店，寻找一封信。" },
    },
  );
  expect(started.status()).toBe(202);
  await expect(async () => {
    const result = await (
      await page.request.get(env.url, { headers: env.headers })
    ).json();
    expect(result.pending_reviews.total).toBe(2);
  }).toPass();
  await open(page);
  await expect(surface(page)).toHaveAttribute("aria-busy", "false");
  await expect(page.getByText("剧本待审核 · 2", { exact: true })).toBeVisible();
  await expect(page.getByText("125.50", { exact: true })).toBeVisible();
  await expect(
    page.getByText("累计实际成本：暂无成本账本", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("成片版本：尚未接入交付记录", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: info.outputPath("project-overview-desktop.png"),
    fullPage: true,
  });
  await page.getByText("成本与成片", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("project-overview-desktop-details.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByText("成本与成片", { exact: true }).scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("project-overview-mobile.png"),
    fullPage: true,
  });
  await page
    .getByText("文本任务进度", { exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("project-overview-mobile-tasks.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  const approved = await page.request.post(
    `/api/v1/episodes/${env.episodes[0].id}/scripts/${version.id}/review`,
    { headers: env.headers, data: { decision: "APPROVE" } },
  );
  expect(approved.status()).toBe(200);
  await page.getByRole("button", { name: "刷新总览", exact: true }).click();
  await expect(page.getByText("剧本已批准 · 1", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "查看该集任务", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 总览剧集2", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "任务与审核", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
});

test("paginates independently of the directory and opens an episode beyond its loaded page", async ({
  page,
}) => {
  const env = await setup(page, 21);
  await open(page);
  await expect(
    page.getByText("生产中 21 · 已归档 0", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: /^进入第 / })).toHaveCount(20);
  await page.getByRole("button", { name: "下一页剧集", exact: true }).click();
  await expect(page.getByRole("button", { name: /^进入第 / })).toHaveCount(1);
  await expect(
    page.getByRole("button", { name: "下一页剧集", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "进入第 21 集", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "第 21 集 · 总览剧集21", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "创作画布", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  expect(
    (await (await page.request.get(env.url, { headers: env.headers })).json())
      .episode_count,
  ).toBe(21);
});

test("shows loading and retry, preserves stale data on refresh failure, and recovers", async ({
  page,
}) => {
  const env = await setup(page);
  let fail = true;
  await page.route("**/projects/*/overview?*", async (route) => {
    if (fail)
      await route.fulfill({
        status: 503,
        json: { error: { code: "UNAVAILABLE", message: "总览暂不可用" } },
      });
    else await route.continue();
  });
  await open(page);
  await expect(page.getByRole("alert")).toContainText("总览暂不可用");
  fail = false;
  await page.getByRole("button", { name: "重试总览", exact: true }).click();
  await expect(
    page.getByText("生产中 1 · 已归档 0", { exact: true }),
  ).toBeVisible();
  fail = true;
  await page.getByRole("button", { name: "刷新总览", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText(
    "当前保留上次成功读取的数据",
  );
  await expect(
    page.getByRole("button", { name: "进入第 1 集", exact: true }),
  ).toBeVisible();
  fail = false;
  await page.getByRole("button", { name: "重试总览", exact: true }).click();
  await expect(page.getByRole("alert")).toHaveCount(0);
  expect(
    (await (await page.request.get(env.url, { headers: env.headers })).json())
      .episode_count,
  ).toBe(1);
});

test("aborts late overview responses when switching projects", async ({
  page,
}) => {
  const env = await setup(page);
  const second = await (
    await page.request.post("/api/v1/projects", {
      headers: env.headers,
      data: { name: "另一个空项目" },
    })
  ).json();
  let release!: () => void;
  let intercepted!: () => void;
  const held = new Promise<void>((resolve) => {
    release = resolve;
  });
  const ready = new Promise<void>((resolve) => {
    intercepted = resolve;
  });
  await page.route(
    `**/projects/${env.project.id}/overview?*`,
    async (route) => {
      const response = await route.fetch();
      intercepted();
      await held;
      await route.fulfill({ response });
    },
  );
  await open(page);
  await ready;
  await expect(
    page.getByText("正在汇总全项目记录…", { exact: true }),
  ).toBeVisible();
  await chooseProject(page, second);
  await page.getByRole("button", { name: "项目总览", exact: true }).click();
  await expect(
    page.getByText("生产中 0 · 已归档 0", { exact: true }),
  ).toBeVisible();
  release();
  await expect(
    page.getByRole("heading", { name: "暂无剧集记录", exact: true }),
  ).toBeVisible();
  await expect(surface(page)).not.toContainText("总览剧集1");
});

test("viewer can inspect an archived project without production actions", async ({
  page,
}) => {
  const env = await setup(page);
  const viewerResponse = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `viewer-${crypto.randomUUID()}@example.com`,
      password: "overview-test-password",
      display_name: "只读成员",
    },
  });
  const viewer = await viewerResponse.json();
  const added = await page.request.post(
    `/api/v1/projects/${env.project.id}/members`,
    {
      headers: env.headers,
      data: { email: viewer.user.email, role: "viewer" },
    },
  );
  expect(added.status()).toBe(201);
  const archived = await page.request.patch(
    `/api/v1/projects/${env.project.id}`,
    { headers: env.headers, data: { archived: true } },
  );
  expect(archived.status()).toBe(200);
  await open(page);
  await expect(
    page.getByText(
      "项目已归档，当前总览仅供查阅；恢复项目后才能继续生产与审核。",
      { exact: true },
    ),
  ).toBeVisible();
  await page.getByRole("button", { name: "进入第 1 集", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 总览剧集1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "开始创作", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "项目设置与归档", exact: true }),
  ).toHaveCount(0);
});
