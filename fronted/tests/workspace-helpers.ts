import { expect, type Page } from "@playwright/test";

// 旧业务验收显式展开阅读面板；默认聚焦画布的行为由 redesign 验收单独覆盖。
export async function openWorkspacePanels(page: Page) {
  await expect(
    page.locator(".login-page, .workspace-toolbar, .welcome-empty").first(),
  ).toBeVisible();
  const directory = page.getByRole("button", { name: "展开目录", exact: true });
  if (await directory.isVisible()) await directory.click();
  const content = page.getByRole("button", {
    name: "展开内容面板",
    exact: true,
  });
  if (await content.isVisible()) await content.click();
}
