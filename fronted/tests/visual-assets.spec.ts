import { test, expect, type Page } from "@playwright/test";
import { chooseProject, openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `asset-${crypto.randomUUID()}@example.com`,
      password: "asset-directory-test-password",
      display_name: "资产验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "视觉资产项目" },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "视觉资产", exact: true }),
  ).toBeVisible();
  return { headers, project, path: `/api/v1/projects/${project.id}/assets` };
}
const save = (page: Page) =>
  page.getByRole("button", { name: "保存视觉资产", exact: true });
async function create(page: Page, name: string, kind = "角色") {
  await page.getByRole("button", { name: "新建视觉资产", exact: true }).click();
  await page.getByRole("combobox", { name: "资产类型", exact: true }).click();
  await page.getByRole("option", { name: kind, exact: true }).click();
  await page.getByLabel("资产名称", { exact: true }).fill(name);
  await save(page).click();
  await expect(
    page.getByRole("heading", { name: `${name} · 修订 0`, exact: true }),
  ).toBeVisible();
}

test("ASSET-01：四类目录持久化、修改、归档恢复与移动端", async ({
  page,
}, testInfo) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { headers, path } = await setup(page);
  for (const [name, kind] of [
    ["小雨", "角色"],
    ["雨巷", "场景"],
    ["旧信", "道具"],
    ["水彩", "风格"],
  ])
    await create(page, name, kind);
  await page.getByRole("button", { name: "编辑资产名称", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "资产类型", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("资产名称", { exact: true }).fill("手绘水彩");
  await save(page).click();
  await expect(
    page.getByRole("heading", { name: "手绘水彩 · 修订 1", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "归档视觉资产", exact: true }).click();
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "恢复视觉资产", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "编辑资产名称", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "恢复视觉资产", exact: true }).click();
  await page.getByRole("button", { name: "确认", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "手绘水彩 · 修订 3", exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(path, { headers })).json()).length,
  ).toBe(4);
  await page.screenshot({
    path: testInfo.outputPath("visual-assets-desktop.png"),
    fullPage: true,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "风格 · 手绘水彩", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "手绘水彩 · 修订 3", exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByLabel("搜索已载入资产", { exact: true }).fill("旧信");
  await expect(
    page.getByRole("button", { name: "道具 · 旧信", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "角色 · 小雨", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("visual-assets-mobile.png"),
    fullPage: true,
  });
  expect(crashes).toEqual([]);
});

test("ASSET-01：保存失败保留草稿、双击单次提交与修订冲突", async ({ page }) => {
  const { path, headers } = await setup(page);
  await page.getByRole("button", { name: "新建视觉资产", exact: true }).click();
  await page.getByLabel("资产名称", { exact: true }).fill("保留草稿");
  await page.route("**" + path, (route) =>
    route.request().method() === "POST"
      ? route.abort("failed")
      : route.continue(),
  );
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("网络连接失败");
  await expect(page.getByLabel("资产名称", { exact: true })).toHaveValue(
    "保留草稿",
  );
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(
    page.getByText("放弃未保存的资产修改？", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await page.unroute("**" + path);
  let posts = 0;
  await page.route("**" + path, async (route) => {
    if (route.request().method() === "POST") {
      posts++;
      await new Promise((resolve) => setTimeout(resolve, 200));
    }
    await route.continue();
  });
  await save(page).evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(
    page.getByRole("heading", { name: "保留草稿 · 修订 0", exact: true }),
  ).toBeVisible();
  expect(posts).toBe(1);
  await page.unroute("**" + path);
  const row = (await (await page.request.get(path, { headers })).json())[0];
  await page.getByRole("button", { name: "编辑资产名称", exact: true }).click();
  expect(
    (
      await page.request.put(path + "/" + row.id, {
        headers,
        data: { name: "远程修改", expected_revision: 0 },
      })
    ).status(),
  ).toBe(200);
  await page.getByLabel("资产名称", { exact: true }).fill("本地修改");
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("资产已被修改");
  await expect(page.getByLabel("资产名称", { exact: true })).toHaveValue(
    "本地修改",
  );
  await page.keyboard.press("Escape");
  await page
    .getByRole("button", { name: "放弃修改并关闭", exact: true })
    .click();
  await page.getByRole("button", { name: "刷新视觉资产", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "远程修改 · 修订 1", exact: true }),
  ).toBeVisible();
});

test("ASSET-01：写入成功后的目录读取失败不重放创建", async ({ page }) => {
  const { path, headers } = await setup(page);
  let posts = 0;
  let created = false;
  await page.route("**" + path + "*", async (route) => {
    if (route.request().method() === "POST") {
      posts++;
      const response = await route.fetch();
      created = true;
      await route.fulfill({ response });
    } else if (created && route.request().method() === "GET")
      await route.abort("failed");
    else await route.continue();
  });
  await create(page, "已写入资产");
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(posts).toBe(1);
  await page.unroute("**" + path + "*");
  await page.getByRole("button", { name: "刷新视觉资产", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "角色 · 已写入资产", exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(path, { headers })).json()).length,
  ).toBe(1);
});

test("ASSET-01：项目归档只读与跨项目页面隔离", async ({ page }) => {
  const { path, headers, project } = await setup(page);
  await create(page, "归档可读");
  const other = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "另一个资产项目" },
    })
  ).json();
  // The second project was created outside the UI; reload its directory first.
  await page.reload();
  await openWorkspacePanels(page);
  await chooseProject(page, other);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "角色 · 归档可读", exact: true }),
  ).toHaveCount(0);
  await chooseProject(page, project);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "角色 · 归档可读", exact: true }),
  ).toBeVisible();
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
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "角色 · 归档可读", exact: true })
    .click();
  await expect(
    page.getByText("当前项目只读，可查看视觉资产。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "新建视觉资产", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "编辑资产名称", exact: true }),
  ).toHaveCount(0);
  expect(
    (await (await page.request.get(path, { headers })).json()).length,
  ).toBe(1);
});

test("ASSET-01：Viewer 可查目录和详情但不能修改", async ({ page }) => {
  const { project, headers } = await setup(page);
  await create(page, "只读资产");
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `asset-viewer-${crypto.randomUUID()}@example.com`,
      password: "asset-directory-test-password",
      display_name: "只读成员",
    },
  });
  expect(registered.status()).toBe(201);
  const viewer = await registered.json();
  expect(
    (
      await page.request.post(`/api/v1/projects/${project.id}/members`, {
        headers,
        data: { email: viewer.user.email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "角色 · 只读资产", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "只读资产 · 修订 0", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByText("当前项目只读，可查看视觉资产。", { exact: true }),
  ).toBeVisible();
  for (const name of ["新建视觉资产", "编辑资产名称", "归档视觉资产"])
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
});
