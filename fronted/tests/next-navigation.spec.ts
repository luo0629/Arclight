import { expect, test, type Page } from "@playwright/test";
import { closeDirectory, openDirectory } from "./workspace-helpers";
import type { Episode, Project } from "../src/types/api";

async function setup(page: Page) {
  const credentials = {
    email: `routes-${crypto.randomUUID()}@example.com`,
    password: "next-navigation-password",
    display_name: "路由验收",
  };
  const registered = await page.request.post("/api/v1/auth/register", {
    data: credentials,
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const projects: Project[] = [];
  const episodes: Episode[] = [];
  for (let i = 0; i < 2; i++) {
    const created = await page.request.post("/api/v1/projects", {
      headers,
      data: { name: `路由项目${i + 1}` },
    });
    expect(created.status()).toBe(201);
    const project = await created.json();
    projects.push(project);
    for (let j = 0; j < 2; j++) {
      const response = await page.request.post(
        `/api/v1/projects/${project.id}/episodes`,
        {
          headers,
          data: { episode_no: j + 1, title: `项目${i + 1}剧集${j + 1}` },
        },
      );
      expect(response.status()).toBe(201);
      episodes.push(await response.json());
    }
  }
  const path = (index: number) =>
    `/projects/${episodes[index].project_id}/episodes/${episodes[index].id}`;
  return { credentials, headers, projects, episodes, path };
}

test("直达剧集优先于首页偏好，视图刷新和前进后退保留 URL 状态", async ({
  page,
}) => {
  const env = await setup(page);
  const errors: string[] = [];
  page.on("pageerror", (error) => errors.push(error.message));
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    env.projects[1].id,
  );
  await page.goto(env.path(1) + "?view=list");
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 项目1剧集2", exact: true }),
  ).toBeVisible();
  const list = page.getByRole("radio", { name: "版本列表", exact: true });
  const tasks = page.getByRole("radio", { name: "任务与审核", exact: true });
  await expect(list).toHaveAttribute("aria-checked", "true");
  await tasks.click();
  await expect(page).toHaveURL(env.path(1) + "?view=tasks");
  await page.goBack();
  await expect(list).toHaveAttribute("aria-checked", "true");
  await page.goForward();
  await expect(tasks).toHaveAttribute("aria-checked", "true");
  await page.reload();
  await expect(tasks).toHaveAttribute("aria-checked", "true");
  await expect(page).toHaveURL(env.path(1) + "?view=tasks");
  expect(errors).toEqual([]);
});

test("项目和剧集导航由 Next.js 接管，共享会话且浏览器历史可恢复", async ({
  page,
}) => {
  const env = await setup(page);
  let refreshes = 0;
  page.on("request", (request) => {
    if (request.url().endsWith("/auth/refresh")) refreshes++;
  });
  await page.goto(env.path(0));
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 项目1剧集1", exact: true }),
  ).toBeVisible();
  const initialRefreshes = refreshes;
  await page.getByRole("button", { name: "项目总览", exact: true }).click();
  await expect(page).toHaveURL(
    `/projects/${env.projects[0].id}?section=overview`,
  );
  await expect(page.getByLabel("项目总览数据", { exact: true })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("项目总览数据", { exact: true })).toBeVisible();
  await openDirectory(page);
  await page.getByRole("button", { name: /项目1剧集2/ }).click();
  await expect(page).toHaveURL(env.path(1));
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 项目1剧集2", exact: true }),
  ).toBeVisible();
  await openDirectory(page);
  await page.getByRole("combobox", { name: "选择项目", exact: true }).click();
  await page
    .getByRole("option", { name: env.projects[1].name, exact: true })
    .click();
  await expect(page).toHaveURL(env.path(2));
  await closeDirectory(page);
  await page.goBack();
  await expect(page).toHaveURL(env.path(1));
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 项目1剧集2", exact: true }),
  ).toBeVisible();
  expect(refreshes).toBe(initialRefreshes + 1); // Only the explicit document reload refreshed the session.
});

test("精确版本节点通过 URL 直达，刷新后仍打开同一版本", async ({ page }) => {
  const env = await setup(page);
  let firstId = "";
  for (let version = 0; version < 2; version++) {
    const saved = await page.request.post(
      `/api/v1/episodes/${env.episodes[0].id}/scripts`,
      {
        headers: env.headers,
        data: {
          expected_version_no: version,
          content: {
            title: `路由剧本${version + 1}`,
            summary: "雨夜来信",
            estimated_duration_ms: 30000,
            characters: ["小雨"],
            scenes: [
              { scene_no: 1, title: "书店", action: "推开门", dialogue: [] },
            ],
          },
        },
      },
    );
    expect(saved.status()).toBe(201);
    const row = await saved.json();
    if (!version) firstId = row.id;
  }
  const url = env.path(0) + `?view=list&node=script%3A${firstId}`;
  await page.goto(url);
  const selected = page
    .locator(".version-row")
    .filter({ hasText: "路由剧本1" });
  await expect(selected).toHaveAttribute("aria-pressed", "true");
  await page.reload();
  await expect(selected).toHaveAttribute("aria-pressed", "true");
  await expect(page.getByLabel("内容与属性", { exact: true })).toContainText(
    "路由剧本1",
  );
});

test("路由切换和刷新保留未提交草稿，草稿内容不进入 URL", async ({ page }) => {
  const env = await setup(page);
  await page.goto(env.path(0) + "?view=create");
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  const title = page.getByLabel("剧本标题", { exact: true });
  await title.fill("不会丢失的路由草稿");
  await page.getByRole("button", { name: "项目总览", exact: true }).click();
  await expect(page.getByLabel("项目总览数据", { exact: true })).toBeVisible();
  await page.goBack();
  await expect(page).toHaveURL(env.path(0) + "?view=create");
  if (!(await title.isVisible()))
    await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(title).toHaveValue("不会丢失的路由草稿");
  page.once("dialog", (dialog) => dialog.accept());
  await page.reload();
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await expect(title).toHaveValue("不会丢失的路由草稿");
  expect(page.url()).not.toContain(encodeURIComponent("不会丢失"));
});

test("未登录直达链接登录后回到原剧集与视图", async ({ page, context }) => {
  const env = await setup(page);
  await context.clearCookies();
  await page.goto(env.path(1) + "?view=tasks");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("邮箱", { exact: true }).fill(env.credentials.email);
  await page.getByLabel("密码", { exact: true }).fill(env.credentials.password);
  await page
    .getByRole("button", { name: "登录并继续创作", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 项目1剧集2", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "任务与审核", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(page).toHaveURL(env.path(1) + "?view=tasks");
});

test("不存在和跨项目剧集链接显示错误，禁止加载错误资源的编辑工作区", async ({
  page,
}) => {
  const env = await setup(page);
  const invalid = `/projects/${env.projects[0].id}/episodes/${env.episodes[2].id}`;
  const reads: string[] = [];
  page.on("request", (request) => {
    if (request.url().includes(`/episodes/${env.episodes[2].id}/`))
      reads.push(request.url());
  });
  await page.goto(invalid);
  await expect(
    page.getByRole("heading", { name: "无法打开此剧集", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("剧集工作区", { exact: true })).toHaveCount(0);
  expect(reads).toEqual([]);
  await page.goto(`/projects/${crypto.randomUUID()}`);
  await expect(
    page.getByRole("heading", { name: "无法打开此项目", exact: true }),
  ).toBeVisible();
  await page.goto(
    `/projects/${env.projects[0].id}/episodes/${crypto.randomUUID()}`,
  );
  await expect(
    page.getByRole("heading", { name: "无法打开此剧集", exact: true }),
  ).toBeVisible();
});

test("目录首批之外的项目和剧集可按 URL 精确读取", async ({ page }) => {
  const env = await setup(page);
  await page.route("**/api/v1/projects?*", async (route) => {
    const response = await route.fetch();
    await route.fulfill({
      response,
      json: (await response.json()).filter(
        (row: { id: string }) => row.id !== env.projects[1].id,
      ),
    });
  });
  await page.route(
    `**/api/v1/projects/${env.projects[1].id}/episodes?*`,
    async (route) => {
      const response = await route.fetch();
      await route.fulfill({
        response,
        json: (await response.json()).filter(
          (row: { id: string }) => row.id !== env.episodes[3].id,
        ),
      });
    },
  );
  await page.goto(env.path(3) + "?view=tasks");
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 项目2剧集2", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("radio", { name: "任务与审核", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
});

test("直达链接遵守项目成员和 Owner 权限", async ({ page }) => {
  const env = await setup(page);
  const email = `viewer-${crypto.randomUUID()}@example.com`;
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email,
      password: "viewer-routes-password",
      display_name: "查看伙伴",
    },
  });
  expect(registered.status()).toBe(201);
  await page.goto(env.path(0));
  await expect(
    page.getByRole("heading", { name: "无法打开此项目", exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("剧集工作区", { exact: true })).toHaveCount(0);
  const added = await page.request.post(
    `/api/v1/projects/${env.projects[0].id}/members`,
    {
      headers: env.headers,
      data: { email, role: "viewer" },
    },
  );
  expect(added.status()).toBe(201);
  await page.goto(env.path(0));
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 项目1剧集1", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("当前为只读模式：项目／剧集已归档，或你的角色是查看者。", {
      exact: true,
    }),
  ).toBeVisible();
  await page.goto(`/projects/${env.projects[0].id}?section=settings`);
  await expect(
    page.getByText("仅项目拥有者可以访问项目设置。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "保存设置", exact: true }),
  ).toHaveCount(0);
});
