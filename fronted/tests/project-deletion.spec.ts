import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels, openDirectory } from "./workspace-helpers";

async function setup(page: Page, archived = true) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "deletion-" + crypto.randomUUID() + "@example.com",
      password: "deletion-test-password",
      display_name: "项目删除验收",
    },
  });
  expect(registration.status()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const result = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "删除与恢复验收" },
  });
  expect(result.status()).toBe(201);
  const project = await result.json();
  const episodeResult = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "保留的剧集" } },
  );
  expect(episodeResult.status()).toBe(201);
  const episode = await episodeResult.json();
  if (archived)
    expect(
      (
        await page.request.patch(`/api/v1/projects/${project.id}`, {
          headers,
          data: { archived: true },
        })
      ).status(),
    ).toBe(200);
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  return { project, episode, headers };
}

async function openDelete(page: Page) {
  await page.getByRole("button", { name: "删除项目", exact: true }).click();
  return page.getByRole("dialog", { name: "确认删除项目", exact: true });
}

for (const mobile of [false, true]) {
  test(`STORY-04：${mobile ? "移动端" : "桌面端"}删除恢复持久化与焦点`, async ({
    page,
  }, testInfo) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const { project, episode, headers } = await setup(page);
    const dialog = await openDelete(page);
    const input = dialog.getByLabel("输入完整项目名称");
    await expect(input).toBeFocused();
    await expect(
      dialog.getByRole("button", { name: "确认移入回收站" }),
    ).toBeDisabled();
    await input.fill("错误名称");
    await expect(
      dialog.getByRole("button", { name: "确认移入回收站" }),
    ).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByRole("button", { name: "删除项目", exact: true }),
    ).toBeFocused();
    await openDelete(page);
    await input.fill(project.name);
    await page.screenshot({
      path: testInfo.outputPath(
        mobile ? "deletion-mobile.png" : "deletion-desktop.png",
      ),
      fullPage: true,
    });
    await dialog.getByRole("button", { name: "确认移入回收站" }).click();
    await expect(dialog).toHaveCount(0);
    await expect(
      page.getByText("项目已移入回收站，30 天内可恢复。", { exact: true }),
    ).toBeVisible();
    await expect(
      page.getByRole("button", { name: "项目设置与归档", exact: true }),
    ).toHaveCount(0);
    expect(
      (
        await page.request.get(`/api/v1/episodes/${episode.id}`, { headers })
      ).status(),
    ).toBe(404);
    await page.reload();
    await page.getByRole("button", { name: "项目回收站", exact: true }).click();
    const trash = page.getByRole("dialog", { name: "项目回收站", exact: true });
    await expect(
      trash.getByRole("heading", { name: project.name, exact: true }),
    ).toBeVisible();
    await page.screenshot({
      path: testInfo.outputPath(
        mobile ? "trash-mobile.png" : "trash-desktop.png",
      ),
      fullPage: true,
    });
    await trash.getByRole("button", { name: "恢复项目", exact: true }).click();
    await expect(trash).toHaveCount(0);
    await expect(
      page.getByText("项目已恢复，仍为归档状态。需要编辑时请先解除归档。", {
        exact: true,
      }),
    ).toBeVisible();
    await page.reload();
    await openWorkspacePanels(page);
    await page
      .getByRole("button", { name: "项目设置与归档", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "恢复这个故事项目", exact: true }),
    ).toBeVisible();
    expect(
      (
        await page.request.get(`/api/v1/episodes/${episode.id}`, { headers })
      ).status(),
    ).toBe(200);
    expect(
      (
        await page.request.get(`/api/v1/projects/${project.id}`, { headers })
      ).status(),
    ).toBe(200);
    expect(
      await (
        await page.request.get("/api/v1/project-trash", { headers })
      ).json(),
    ).toEqual([]);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= window.innerWidth,
      ),
    ).toBe(true);
  });
}

test("STORY-04：未归档禁用删除，失败保留输入并阻止重复提交", async ({
  page,
}) => {
  const { project, headers } = await setup(page, false);
  await expect(
    page.getByRole("button", { name: "删除项目", exact: true }),
  ).toBeDisabled();
  expect(
    (
      await page.request.patch(`/api/v1/projects/${project.id}`, {
        headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openWorkspacePanels(page);
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  const dialog = await openDelete(page);
  await dialog.getByLabel("输入完整项目名称").fill(project.name);
  let calls = 0;
  let release!: () => void;
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route(`**/api/v1/projects/${project.id}`, async (route) => {
    if (route.request().method() !== "DELETE") return route.continue();
    calls++;
    await pending;
    await route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "ACTIVE_WORKFLOW",
          message: "请先结束或取消正在进行的工作流",
        },
      }),
    });
  });
  await dialog.getByRole("button", { name: "确认移入回收站" }).click();
  await expect.poll(() => calls).toBe(1);
  await page.keyboard.press("Escape");
  await expect(dialog).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "正在删除…", exact: true }),
  ).toBeDisabled();
  await dialog.getByLabel("输入完整项目名称").press("Enter");
  expect(calls).toBe(1);
  release();
  await expect(dialog.getByRole("alert")).toContainText(
    "请先结束或取消正在进行的工作流",
  );
  await expect(dialog.getByLabel("输入完整项目名称")).toHaveValue(project.name);
  expect(
    (
      await page.request.get(`/api/v1/projects/${project.id}`, { headers })
    ).status(),
  ).toBe(200);
});

test("STORY-04：删除成功后目录故障不重发操作或保留旧编辑器", async ({
  page,
}) => {
  const { project } = await setup(page);
  let calls = 0;
  await page.route(`**/api/v1/projects/${project.id}`, async (route) => {
    if (route.request().method() === "DELETE") calls++;
    await route.continue();
  });
  await page.route("**/api/v1/projects?*", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "UNAVAILABLE", message: "目录暂不可用" },
      }),
    }),
  );
  const dialog = await openDelete(page);
  await dialog.getByLabel("输入完整项目名称").fill(project.name);
  await dialog.getByRole("button", { name: "确认移入回收站" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByText("项目已移入回收站，30 天内可恢复。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "删除项目", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "项目设置与归档", exact: true }),
  ).toHaveCount(0);
  expect(calls).toBe(1);
  expect(
    await page.evaluate(() => localStorage.getItem("arclight-project")),
  ).toBeNull();
  await page.getByRole("button", { name: "项目回收站", exact: true }).click();
  const trash = page.getByRole("dialog", { name: "项目回收站", exact: true });
  await expect(
    trash.getByRole("heading", { name: project.name, exact: true }),
  ).toBeVisible();
  await trash.getByRole("button", { name: "恢复项目", exact: true }).click();
  await expect(trash).toHaveCount(0);
  await expect(
    page.getByText("项目已恢复，仍为归档状态。需要编辑时请先解除归档。", {
      exact: true,
    }),
  ).toBeVisible();
  await openDirectory(page);
  await expect(
    page.getByRole("combobox", { name: "选择项目", exact: true }),
  ).toContainText(project.name);
  expect(calls).toBe(1);
});

test("STORY-04：回收站加载与恢复失败可重试，空目录仍可访问", async ({
  page,
}) => {
  const { project, headers } = await setup(page);
  expect(
    (
      await page.request.delete(`/api/v1/projects/${project.id}`, {
        headers,
        data: { confirm_name: project.name },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await page.route("**/api/v1/project-trash?*", (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({
        error: { code: "UNAVAILABLE", message: "回收站暂不可用" },
      }),
    }),
  );
  await page.getByRole("button", { name: "项目回收站", exact: true }).click();
  const trash = page.getByRole("dialog", { name: "项目回收站", exact: true });
  await expect(trash.getByRole("alert")).toContainText("回收站暂不可用");
  await page.unroute("**/api/v1/project-trash?*");
  await trash
    .getByRole("button", { name: "刷新回收站项目", exact: true })
    .click();
  await expect(
    trash.getByRole("heading", { name: project.name, exact: true }),
  ).toBeVisible();
  await page.route(`**/api/v1/projects/${project.id}/restore`, (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({
        error: {
          code: "DELETION_CONFLICT",
          message: "删除状态已变化，请刷新回收站",
        },
      }),
    }),
  );
  await trash.getByRole("button", { name: "恢复项目", exact: true }).click();
  await expect(trash.getByRole("alert").first()).toContainText(
    "删除状态已变化",
  );
  await expect(
    trash.getByRole("button", { name: "恢复项目", exact: true }),
  ).toBeEnabled();
  await page.unroute(`**/api/v1/projects/${project.id}/restore`);
  await trash.getByRole("button", { name: "恢复项目", exact: true }).click();
  await expect(trash).toHaveCount(0);
});
