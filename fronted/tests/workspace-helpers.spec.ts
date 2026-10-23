import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

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
    page.getByRole("heading", { name: "欢迎回来，小创作家", exact: true }),
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
    await expect(page.getByRole("combobox", { name: "选择项目" })).toHaveValue(
      project.id,
    );
    await expect(
      page.getByRole("button", { name: "收起目录", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
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
    // 窄屏默认版本列表，切到画布后才有可折叠的内容面板。
    await page.getByRole("radio", { name: "创作画布", exact: true }).click();
    await openWorkspacePanels(page);
    await openWorkspacePanels(page);
    await expect(
      page.getByRole("button", { name: "收起目录", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("button", { name: "收起内容面板", exact: true }),
    ).toHaveAttribute("aria-expanded", "true");
    await expect(
      page.getByRole("heading", {
        name: "第 1 集 · 辅助函数验收",
        exact: true,
      }),
    ).toBeVisible();
  }
});
