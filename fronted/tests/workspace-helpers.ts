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
  await expect(ready.first()).toBeVisible();
  const directory = page.getByRole("button", { name: "展开目录", exact: true });
  if (await directory.isVisible()) await directory.click();
  const content = page.getByRole("button", {
    name: "展开内容面板",
    exact: true,
  });
  if (await content.isVisible()) await content.click();
}
