import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "bible-" + crypto.randomUUID() + "@example.com",
      password: "story-bible-test-password",
      display_name: "故事设定验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "故事设定项目" },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const path = "/api/v1/projects/" + project.id + "/story-entries";
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "故事设定", level: 1, exact: true }),
  ).toBeVisible();
  return { path, headers, project };
}
const save = (page: Page) =>
  page.getByRole("button", { name: "保存故事设定", exact: true });
async function kind(page: Page, value: string) {
  await page.getByRole("combobox", { name: "设定类型", exact: true }).click();
  await page.getByRole("option", { name: value, exact: true }).click();
}
async function create(page: Page, name: string, type = "世界观") {
  await page.getByRole("button", { name: "新建设定", exact: true }).click();
  await kind(page, type);
  await page.getByLabel("设定名称", { exact: true }).fill(name);
  await save(page).click();
  await expect(
    page.getByRole("heading", { name: name + " · 修订 0", exact: true }),
  ).toBeVisible();
}

test("STORY-02：持久化规则、稳定关联、修订、归档依赖及恢复", async ({
  page,
}, testInfo) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { path, headers } = await setup(page);
  await create(page, "旧书店", "地点");
  await create(page, "信封", "物品");
  await create(page, "重逢", "场景");
  await create(page, "小雨", "角色");
  await page
    .getByLabel("规则（每行一条，最多 10 条）", { exact: true })
    .fill("不能飞行\n不能复活");
  await page.getByRole("combobox", { name: "关联目标", exact: true }).click();
  await page
    .getByRole("option", { name: "地点 · 旧书店", exact: true })
    .click();
  await page.getByRole("button", { name: "添加关联", exact: true }).click();
  await save(page).click();
  await expect(
    page.getByRole("heading", { name: "小雨 · 修订 1", exact: true }),
  ).toBeVisible();
  const rows = await (await page.request.get(path, { headers })).json();
  const character = rows.find(
    (row: { kind: string }) => row.kind === "CHARACTER",
  );
  expect(character.rules).toEqual(["不能飞行", "不能复活"]);
  expect(character.relations).toEqual([
    {
      relation: "located_in",
      target_id: rows.find((row: { kind: string }) => row.kind === "LOCATION")
        .id,
    },
  ]);
  await page
    .getByRole("button", { name: "地点 · 旧书店", exact: true })
    .click();
  await page.getByRole("button", { name: "归档故事设定", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("仍有活动设定引用此项");
  await page.getByRole("button", { name: "角色 · 小雨", exact: true }).click();
  await page.getByRole("button", { name: "移除关联 1", exact: true }).click();
  await save(page).click();
  await expect(
    page.getByRole("heading", { name: "小雨 · 修订 2", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "地点 · 旧书店", exact: true })
    .click();
  await page.getByRole("button", { name: "归档故事设定", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "恢复故事设定", exact: true }),
  ).toBeVisible();
  await expect(save(page)).toBeDisabled();
  await page.getByRole("button", { name: "恢复故事设定", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "旧书店 · 修订 2", exact: true }),
  ).toBeVisible();
  await create(page, "无魔法世界");
  await page.screenshot({
    path: testInfo.outputPath("story-bible-desktop.png"),
    fullPage: true,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "角色 · 小雨", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "角色 · 小雨", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "故事设定编辑" }),
  ).toContainText("不能飞行 不能复活");
  expect(
    (
      await (
        await page.request.get(path + "/" + character.id, { headers })
      ).json()
    ).id,
  ).toBe(character.id);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.screenshot({
    path: testInfo.outputPath("story-bible-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  expect(crashes).toEqual([]);
});

test("STORY-02：失败保留草稿、修订冲突、放弃确认与防重复提交", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await page.getByLabel("设定名称", { exact: true }).fill("保留草稿");
  await page.route("**" + path, (route) =>
    route.request().method() === "POST"
      ? route.abort("failed")
      : route.continue(),
  );
  await save(page).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
  await expect(page.getByLabel("设定名称", { exact: true })).toHaveValue(
    "保留草稿",
  );
  await page.unroute("**" + path);
  let posts = 0;
  await page.route("**" + path, async (route) => {
    if (route.request().method() === "POST") {
      posts++;
      await new Promise((resolve) => setTimeout(resolve, 250));
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
  expect(
    (
      await page.request.put(path + "/" + row.id, {
        headers,
        data: {
          kind: row.kind,
          name: "远程修改",
          expected_revision: row.revision,
        },
      })
    ).status(),
  ).toBe(200);
  await page.getByLabel("设定名称", { exact: true }).fill("本地草稿");
  await save(page).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("设定已被修改");
  await expect(page.getByLabel("设定名称", { exact: true })).toHaveValue(
    "本地草稿",
  );
  await page.getByRole("button", { name: "刷新故事设定", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
  await page.getByRole("button", { name: "继续编辑", exact: true }).click();
  await expect(page.getByLabel("设定名称", { exact: true })).toHaveValue(
    "本地草稿",
  );
  await page.getByRole("button", { name: "刷新故事设定", exact: true }).click();
  await page
    .getByRole("button", { name: "放弃修改并继续", exact: true })
    .click();
  await expect(page.getByLabel("设定名称", { exact: true })).toHaveValue(
    "远程修改",
  );
});

test("STORY-02：项目归档后目录可读且编辑控件禁用", async ({ page }) => {
  const { headers, project } = await setup(page);
  await create(page, "归档后可读");
  expect(
    (
      await page.request.patch("/api/v1/projects/" + project.id, {
        headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  await page
    .getByRole("button", { name: "世界观 · 归档后可读", exact: true })
    .click();
  await expect(page.getByLabel("设定名称", { exact: true })).toBeDisabled();
  await expect(
    page.getByRole("combobox", { name: "设定类型", exact: true }),
  ).toBeDisabled();
  await expect(save(page)).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "新建设定", exact: true }),
  ).toHaveCount(0);
});
