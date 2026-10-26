import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels, openDirectory } from "./workspace-helpers";

async function account(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "workspace-helper-regression",
      display_name: "工作区就绪验收",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  return { Authorization: "Bearer " + session.access_token };
}

async function selectedProject(page: Page, withEpisode = false) {
  const headers = await account(page);
  const response = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "精确选中的项目" },
  });
  expect(response.status()).toBe(201);
  const project = await response.json();
  if (withEpisode) {
    const episode = await page.request.post(
      "/api/v1/projects/" + project.id + "/episodes",
      { headers, data: { episode_no: 1, title: "辅助函数验收" } },
    );
    expect(episode.status()).toBe(201);
  }
  // 固定选中项目，不依赖项目排序或其他用例的导航行为。
  await page.addInitScript((id) => {
    localStorage.setItem("arclight-project", id);
  }, project.id);
  return project;
}

test("工作区辅助函数：登录页不要求存在工作区面板", async ({ page }) => {
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("heading", {
      name: /让一个小灵感，\s*长成好故事。/,
      exact: true,
    }),
  ).toBeVisible();
});

test("工作区辅助函数：账号无项目时识别载入完成的欢迎页", async ({ page }) => {
  await account(page);
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("heading", { name: "新的一页，等你落笔", exact: true }),
  ).toBeVisible();
});

test("工作区辅助函数：无剧集项目刷新后仍可展开目录", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const project = await selectedProject(page);
  await page.goto("/");
  for (let visit = 0; visit < 2; visit++) {
    if (visit) await page.reload();
    // 先等待最终空状态，确保不会借加载占位意外跳过就绪判断。
    await expect(
      page.getByRole("heading", { name: "写下第一集的名字吧", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "展开目录", exact: true }),
    ).toHaveAttribute("aria-expanded", "false");
    await openWorkspacePanels(page);
    await openDirectory(page);
    await expect(page.getByRole("combobox", { name: "选择项目" })).toHaveText(
      project.name,
    );
    await expect
      .poll(() => page.evaluate(() => localStorage.getItem("arclight-project")))
      .toBe(project.id);
    await expect(
      page.getByRole("button", { name: "关闭项目目录", exact: true }),
    ).toBeVisible();
  }
});

test("工作区辅助函数：有剧集时展开目录和内容且重复调用不折叠", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await selectedProject(page, true);
  await page.goto("/");
  for (let visit = 0; visit < 2; visit++) {
    if (visit) await page.reload();
    // 窄屏也以画布为主，目录按需覆盖，不占用画布空间。
    await page.getByRole("radio", { name: "创作画布", exact: true }).click();
    await openWorkspacePanels(page);
    await openWorkspacePanels(page);
    await expect(
      page.getByRole("button", { name: "展开目录", exact: true }),
    ).toHaveAttribute("aria-expanded", "false");
    await expect(
      page.getByRole("heading", { name: "先写下你的灵感", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("heading", {
        name: "第 1 集 · 辅助函数验收",
        exact: true,
      }),
    ).toBeVisible();
  }
});

test("工作区辅助函数：加载中的临时空态不能跳过内容面板展开", async ({
  page,
}) => {
  // 定向重放 trace 中的竞态：已完成的欢迎空态与下一次列表请求重叠。
  await page.setContent(`
    <section aria-label="项目分页" aria-busy="true"><p role="status">尚未加载 · 加载中…</p></section>
    <h1>新的一页，等你落笔</h1>
  `);
  let finished = false;
  const opening = openWorkspacePanels(page).then(() => {
    finished = true;
  });
  // 浏览器侧握手而非 sleep：加载时辅助函数必须仍在等待。
  await expect(page.getByRole("heading")).toBeVisible();
  expect(finished).toBe(false);
  await page.evaluate(() => {
    document.body.innerHTML = `
      <section aria-label="项目分页" aria-busy="false"><p role="status">已加载 1 条 · 已全部加载</p></section>
      <section class="workspace-toolbar"><button aria-expanded="false" onclick="this.textContent='收起内容面板';this.setAttribute('aria-expanded','true')">展开内容面板</button></section>
    `;
  });
  await opening;
  await expect(
    page.getByRole("button", { name: "收起内容面板", exact: true }),
  ).toHaveAttribute("aria-expanded", "true");
});
