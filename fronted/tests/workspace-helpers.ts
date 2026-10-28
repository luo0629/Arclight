import { expect, type Page } from "@playwright/test";

// 旧业务验收显式展开阅读面板；默认聚焦画布的行为由 redesign 验收单独覆盖。
export async function openWorkspacePanels(page: Page) {
  await closeDirectory(page);
  // 等待已载入的最终状态，避免遗漏空剧集或误把加载中的欢迎占位当作就绪。
  const ready = page
    .locator(".login-page, .workspace-toolbar, .project-main.is-management")
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
    // 列表会在加载期间卸载重建。每轮检查当前快照，让外层重试重新定位，
    // 避免单个已卸载节点的自动等待耗尽整个就绪检查。
    const lists = await pagination.evaluateAll((elements) =>
      elements.map((element) => ({
        busy: element.getAttribute("aria-busy"),
        status: element.querySelector('[role="status"]')?.textContent ?? "",
      })),
    );
    for (const list of lists) {
      expect(list.busy).toBe("false");
      expect(list.status).toContain("已加载");
    }
    await expect(ready.first()).toBeVisible();
    const surfaces = await page
      .locator(".application, .project-body, .episode-studio")
      .evaluateAll((elements) =>
        elements.map((element) => element.getAttribute("aria-busy")),
      );
    for (const busy of surfaces) {
      expect(busy).toBe("false");
    }
  }).toPass({ timeout: 5000 });
  await closeDirectory(page);
  const content = page.getByRole("button", {
    name: "展开内容面板",
    exact: true,
  });
  if (await content.isVisible()) await content.click();
  // An empty canvas has no selected content. Creation is its own page.
  if (
    (await page.locator(".workspace-toolbar").isVisible()) &&
    (await page
      .getByRole("radio", { name: "创作画布", exact: true })
      .and(page.locator('[aria-checked="true"]'))
      .isVisible()) &&
    !(await page.getByLabel("内容与属性", { exact: true }).isVisible()) &&
    (await page
      .getByRole("button", { name: "开始创作", exact: true })
      .isVisible())
  ) {
    await page.getByRole("button", { name: "开始创作", exact: true }).click();
  }
}

export async function openDirectory(page: Page) {
  const panel = page.locator('#project-directory[data-state="open"]');
  const trigger = page.getByRole("button", { name: "展开目录", exact: true });
  // 路由导航可能在检查后改变面板；等待当前可操作入口或已打开面板。
  await expect
    .poll(async () => (await panel.isVisible()) || (await trigger.isVisible()))
    .toBe(true);
  if (!(await panel.isVisible())) await trigger.click();
  await expect(panel).toBeVisible();
}

export async function closeDirectory(page: Page) {
  const panel = page.locator('#project-directory[data-state="open"]');
  if (await panel.count()) {
    await panel
      .getByRole("button", { name: "关闭项目目录", exact: true })
      .click();
  }
  await expect(page.locator('[data-slot="sheet-overlay"]')).toHaveCount(0);
  await expect(page.locator("#project-directory")).toHaveCount(0);
}

export async function chooseProject(page: Page, project: { name: string }) {
  await openDirectory(page);
  await page.getByRole("combobox", { name: "选择项目", exact: true }).click();
  await page.getByRole("option", { name: project.name, exact: true }).click();
  // 等目标项目的目录挂载，避免把路由切换的卸载间隙当成已经关闭。
  await expect(
    page.getByRole("combobox", { name: "选择项目", exact: true }),
  ).toContainText(project.name);
  await closeDirectory(page);
}

export async function signOut(page: Page) {
  await page.getByRole("button", { name: "账号菜单", exact: true }).click();
  await page.getByRole("menuitem", { name: "退出登录", exact: true }).click();
}
