import { expect, type Page } from "@playwright/test";

// 旧业务验收显式展开阅读面板；默认聚焦画布的行为由 redesign 验收单独覆盖。
export async function openWorkspacePanels(page: Page) {
  // 等待已载入的最终状态，避免遗漏空剧集或误把加载中的欢迎占位当作就绪。
  const ready = page
    .locator(".login-page, .workspace-toolbar")
    .or(
      page.getByRole("heading", {
        name: "新的一页，等你落笔",
        exact: true,
      }),
    )
    .or(
      page.getByRole("heading", {
        name: "写下第一集的名字吧",
        exact: true,
      }),
    );
  // StrictMode 会取消首个列表请求；其调用方可能短暂显示“已完成”空态，
  // 但后续列表仍在读取。不能把这一帧当作最终就绪并跳过面板展开。
  await expect(async () => {
    await expect(ready.first()).toBeVisible();
    const pagination = page.locator(
      '[aria-label="项目分页"], [aria-label="剧集分页"]',
    );
    for (const list of await pagination.all()) {
      await expect(list).toHaveAttribute("aria-busy", "false");
      await expect(list.locator('[role="status"]')).toContainText("已加载");
    }
    await expect(ready.first()).toBeVisible();
    if (
      await page
        .getByRole("heading", { name: "新的一页，等你落笔", exact: true })
        .isVisible()
    ) {
      await expect(
        page.locator('[aria-label="项目分页"] [role="status"]'),
      ).toContainText("已加载 0 条");
    }
    if (
      await page
        .getByRole("heading", { name: "写下第一集的名字吧", exact: true })
        .isVisible()
    ) {
      await expect(
        page.locator('[aria-label="剧集分页"] [role="status"]'),
      ).toContainText("已加载 0 条");
    }
  }).toPass({ timeout: 5000 });
  const directory = page.getByRole("button", { name: "展开目录", exact: true });
  if (await directory.isVisible()) await directory.click();
  const content = page.getByRole("button", {
    name: "展开内容面板",
    exact: true,
  });
  if (await content.isVisible()) await content.click();
}

export async function chooseProject(page: Page, project: { name: string }) {
  await page.getByRole("combobox", { name: "选择项目", exact: true }).click();
  await page.getByRole("option", { name: project.name, exact: true }).click();
}

export async function signOut(page: Page) {
  await page.getByRole("button", { name: "账号菜单", exact: true }).click();
  await page.getByRole("menuitem", { name: "退出登录", exact: true }).click();
}
