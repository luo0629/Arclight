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

const versionPng =
  "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAMCAIAAADQ/GvKAAAAFUlEQVR4nGNkYPjPgA0wYRVlGMkSADciARf1YEhDAAAAAElFTkSuQmCC";
async function fillImageImport(page: Page) {
  await page
    .getByRole("button", { name: "导入新图片版本", exact: true })
    .click();
  await page.getByLabel("版本图片", { exact: true }).setInputFiles({
    name: "scene.png",
    mimeType: "image/png",
    buffer: Buffer.from(versionPng, "base64"),
  });
  await page
    .getByLabel("图片来源说明", { exact: true })
    .fill("自行绘制的验收图");
  await page
    .getByLabel("图片使用权说明", { exact: true })
    .fill("拥有测试图片使用权");
  await page
    .getByRole("checkbox", {
      name: "我确认拥有此图片的使用权并同意保存到项目私有目录",
    })
    .check();
}

test("ASSET-05：真实原图版本、人工批准、归档历史和精确回执", async ({
  page,
}, testInfo) => {
  const { path, headers } = await setup(page);
  await create(page, "图片版本场景", "场景");
  await fillImageImport(page);
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const details = page.getByLabel("精确资产版本详情", { exact: true });
  await expect(details.getByRole("heading")).toContainText(
    "版本 v1 · 候选草稿",
  );
  await expect(
    details.getByRole("img", { name: "精确资产版本原图" }),
  ).toBeVisible();
  await expect(
    details.getByText("scene.png · 8×12 · 78 字节", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "提交版本审核", exact: true }).click();
  await expect(details.getByRole("heading")).toContainText("待人工审核");
  await page.getByRole("button", { name: "批准此版本", exact: true }).click();
  await expect(details.getByRole("heading")).toContainText("已批准");
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const versionPath = path + "/" + row.id + "/versions";
  const first = (
    await (await page.request.get(versionPath, { headers })).json()
  )[0];
  expect(first.status).toBe("APPROVED");
  expect(row.current_version_id).toBeNull();
  const image = await page.request.get(
    versionPath + "/" + first.id + "/image",
    { headers },
  );
  expect((await image.body()).equals(Buffer.from(versionPng, "base64"))).toBe(
    true,
  );
  await page.getByRole("button", { name: "归档此版本", exact: true }).click();
  await expect(details.getByRole("heading")).toContainText("已归档");
  await expect(details.getByRole("img")).toBeVisible();
  await page
    .getByRole("button", { name: "恢复归档前状态", exact: true })
    .click();
  await expect(details.getByRole("heading")).toContainText("已批准");
  await fillImageImport(page);
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(details.getByRole("heading")).toContainText(
    "版本 v2 · 候选草稿",
  );
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 已批准", exact: true })
    .click();
  await expect(details.getByRole("heading")).toContainText("版本 v1 · 已批准");
  await page.screenshot({
    path: testInfo.outputPath("asset-versions-desktop.png"),
    fullPage: true,
  });
  await details.getByRole("img").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("asset-versions-preview-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("asset-versions-mobile.png"),
    fullPage: true,
  });
  await details.getByRole("img").scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("asset-versions-preview-mobile.png"),
    fullPage: true,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "场景 · 图片版本场景", exact: true })
    .click();
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 已批准", exact: true })
    .click();
  await expect(details.getByRole("img")).toBeVisible();
  expect(
    (
      await (
        await page.request.get(versionPath + "/" + first.id, { headers })
      ).json()
    ).checksum,
  ).toBe(first.checksum);
});

test("ASSET-05：未知提交结果幂等重放、状态冲突保留说明和图片失败重试", async ({
  page,
}) => {
  const { path, headers } = await setup(page);
  await create(page, "重试版本");
  await fillImageImport(page);
  let dropped = false;
  await page.route("**/assets/*/versions", async (route) => {
    if (route.request().method() === "POST" && !dropped) {
      dropped = true;
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      await route.abort();
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("图片来源说明", { exact: true })).toHaveValue(
    "自行绘制的验收图",
  );
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const versionPath = path + "/" + row.id + "/versions";
  const versions = await (
    await page.request.get(versionPath, { headers })
  ).json();
  expect(versions).toHaveLength(1);
  const version = versions[0];
  await page.getByLabel("版本状态说明", { exact: true }).fill("本地审核草稿");
  const changed = await page.request.patch(
    versionPath + "/" + version.id + "/state",
    {
      headers,
      data: { expected_revision: 0, status: "READY_FOR_REVIEW" },
    },
  );
  expect(changed.status()).toBe(200);
  await page.getByRole("button", { name: "提交版本审核", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("资产版本状态已改变");
  await expect(page.getByLabel("版本状态说明", { exact: true })).toHaveValue(
    "本地审核草稿",
  );
  await page.getByRole("button", { name: "刷新资产版本", exact: true }).click();
  let imageAvailable = false;
  await page.route("**/versions/*/image", async (route) => {
    if (!imageAvailable) {
      await route.fulfill({
        status: 503,
        contentType: "application/json",
        body: JSON.stringify({
          error: { code: "TEST_UNAVAILABLE", message: "测试图片暂不可读" },
        }),
      });
    } else await route.continue();
  });
  // Remount the exact preview to exercise a failed authenticated GET and a GET-only retry.
  await fillImageImport(page);
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await page
    .getByRole("button", { name: "放弃导入并关闭", exact: true })
    .click();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "角色 · 重试版本", exact: true })
    .click();
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 待人工审核", exact: true })
    .click();
  await expect(
    page.getByRole("button", { name: "重试读取图片", exact: true }),
  ).toBeVisible();
  imageAvailable = true;
  await page.getByRole("button", { name: "重试读取图片", exact: true }).click();
  await expect(
    page.getByRole("img", { name: "精确资产版本原图" }),
  ).toBeVisible();
});

test("ASSET-04：场景与关键道具设定真实持久化、冲突及移动端", async ({
  page,
}, testInfo) => {
  const { path, headers } = await setup(page);
  await create(page, "书房", "场景");
  await page.getByRole("button", { name: "编辑场景设定", exact: true }).click();
  for (const [name, value] of [
    ["空间布局", "东窗西门"],
    ["时间段", "雨夜"],
    ["灯光", "暖台灯"],
    ["场景配色", "蓝灰"],
    ["环境描述", "安静书房"],
  ])
    await page.getByLabel(name, { exact: true }).fill(value);
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByText("东窗西门", { exact: true })).toBeVisible();
  await create(page, "铜钥匙", "道具");
  await page.getByRole("button", { name: "编辑道具设定", exact: true }).click();
  await page.getByLabel("关键道具外观", { exact: true }).fill("磨损铜钥匙");
  await page.getByLabel("剧情状态", { exact: true }).fill("缺一齿，已交给主角");
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const rows = await (await page.request.get(path, { headers })).json();
  const prop = rows.find(
    (row: { asset_type: string }) => row.asset_type === "PROP",
  );
  expect(prop.prop_profile).toEqual({
    appearance: "磨损铜钥匙",
    story_state: "缺一齿，已交给主角",
    holder: null,
  });
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "道具 · 铜钥匙", exact: true })
    .click();
  await expect(page.getByText("磨损铜钥匙", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "编辑道具设定", exact: true }).click();
  await page.getByLabel("剧情状态", { exact: true }).fill("本地草稿");
  expect(
    (
      await page.request.put(path + "/" + prop.id, {
        headers,
        data: { name: "远端钥匙", expected_revision: 1 },
      })
    ).status(),
  ).toBe(200);
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("资产已被修改");
  await expect(page.getByLabel("剧情状态", { exact: true })).toHaveValue(
    "本地草稿",
  );
  await page.screenshot({
    path: testInfo.outputPath("scene-prop-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("scene-prop-mobile.png"),
    fullPage: true,
  });
});

test("ASSET-03：固定可变特征、服装表情持久化与草稿冲突", async ({
  page,
}, testInfo) => {
  const { path, headers } = await setup(page);
  await create(page, "角色设定验收");
  await page.getByRole("button", { name: "编辑角色设定", exact: true }).click();
  await page.getByLabel("固定特征（每行一项）", { exact: true }).fill("黑发");
  await page
    .getByLabel("允许变化特征（每行一项）", { exact: true })
    .fill("姿势");
  await page.getByLabel("表情（每行一项）", { exact: true }).fill("微笑");
  await page.getByRole("button", { name: "添加可选服装", exact: true }).click();
  await page.getByLabel("服装1名称", { exact: true }).fill("蓝色常服");
  await page.getByLabel("服装1说明", { exact: true }).fill("棉布外套");
  await page.getByRole("combobox", { name: "默认服装", exact: true }).click();
  await page.getByRole("option", { name: "蓝色常服", exact: true }).click();
  await page.getByRole("button", { name: "添加可选服装", exact: true }).click();
  await page.getByLabel("服装2名称", { exact: true }).fill("雨衣");
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const row = (await (await page.request.get(path, { headers })).json())[0];
  expect(row.character_profile.fixed_features).toEqual(["黑发"]);
  expect(row.character_profile.outfits.length).toBe(2);
  expect(row.character_profile.default_outfit_id).toBe(
    row.character_profile.outfits[0].id,
  );
  expect(row.current_version_id).toBeNull();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "角色 · 角色设定验收", exact: true })
    .click();
  await expect(
    page.getByText("蓝色常服（默认） · 棉布外套", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("character-desktop.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "编辑角色设定", exact: true }).click();
  await page
    .getByLabel("固定特征（每行一项）", { exact: true })
    .fill("本地草稿");
  const url = path + "/" + row.id + "/character-profile";
  await page.route("**" + url, (route) => route.abort("failed"));
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
  await expect(
    page.getByLabel("固定特征（每行一项）", { exact: true }),
  ).toHaveValue("本地草稿");
  await page.unroute("**" + url);
  expect(
    (
      await page.request.put(path + "/" + row.id, {
        headers,
        data: { name: "远端角色", expected_revision: 1 },
      })
    ).status(),
  ).toBe(200);
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("资产已被修改");
  await page.setViewportSize({ width: 390, height: 844 });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("character-mobile.png"),
    fullPage: true,
  });
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(
    page.getByText("放弃未保存的设定？", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "放弃修改并关闭", exact: true })
    .click();
});
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

test("ASSET-07：选用锁定、只读、解锁及并发冲突", async ({ page }) => {
  const { path, headers } = await setup(page);
  await create(page, "选用锁", "场景");
  await fillImageImport(page);
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await page.getByRole("button", { name: "提交版本审核", exact: true }).click();
  await page.getByRole("button", { name: "批准此版本", exact: true }).click();
  await page.getByRole("button", { name: "选用此版本", exact: true }).click();
  await page.getByRole("button", { name: "锁定当前选用", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "解锁当前选用", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "导入新图片版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "编辑资产名称", exact: true }),
  ).toBeDisabled();
  const row = (await (await page.request.get(path, { headers })).json())[0];
  expect(row.status).toBe("LOCKED");
  expect(row.lock_version).toBe(2);
  await page.reload();
  await page
    .getByRole("button", { name: "场景 · 选用锁", exact: true })
    .click();
  await page.getByRole("button", { name: "解锁当前选用", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "编辑资产名称", exact: true }),
  ).toBeEnabled();
  const latest = (await (await page.request.get(path, { headers })).json())[0];
  await page.request.post(
    path + "/" + row.id + "/versions/" + row.current_version_id + "/lock",
    {
      headers,
      data: {
        expected_revision: latest.revision,
        expected_lock_version: latest.lock_version,
      },
    },
  );
  await page.getByRole("button", { name: "锁定当前选用", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("已锁定");
  await page.getByRole("button", { name: "刷新视觉资产", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "解锁当前选用", exact: true }),
  ).toBeVisible();
});

test("ASSET-06：人工选用精确版本、历史另存与重新审核", async ({ page }) => {
  const { path, headers } = await setup(page);
  await create(page, "选用与恢复", "场景");
  await fillImageImport(page);
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  const details = page.getByLabel("精确资产版本详情", { exact: true });
  await page.getByRole("button", { name: "提交版本审核", exact: true }).click();
  await page.getByRole("button", { name: "批准此版本", exact: true }).click();
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const versionPath = path + "/" + row.id + "/versions";
  const first = (
    await (await page.request.get(versionPath, { headers })).json()
  )[0];
  expect(row.current_version_id).toBeNull();
  await page.getByRole("button", { name: "选用此版本", exact: true }).click();
  await expect(
    page.getByText("当前选用：" + first.id, { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "归档此版本", exact: true }),
  ).toBeDisabled();
  await page
    .getByLabel("版本状态说明", { exact: true })
    .fill("恢复构图并重新审核");
  await page
    .getByRole("button", { name: "从此版本另存新候选", exact: true })
    .click();
  await expect(details.getByRole("heading")).toContainText(
    "版本 v2 · 候选草稿",
  );
  await expect(details).toContainText("恢复来源版本：" + first.id);
  await expect(
    page.getByText("当前选用：" + first.id, { exact: true }),
  ).toBeVisible();
  await expect(
    details.getByRole("button", { name: "选用此版本", exact: true }),
  ).toHaveCount(0);
  const records = await (
    await page.request.get(versionPath, { headers })
  ).json();
  expect(records[0]).toEqual(first);
  expect(records[1].restored_from_version_id).toBe(first.id);
  await page.reload();
  await page
    .getByRole("button", { name: "场景 · 选用与恢复", exact: true })
    .click();
  await expect(
    page.getByText("当前选用：" + first.id, { exact: true }),
  ).toBeVisible();
});

test("ASSET-03：参考图选择控件绑定精确版本并允许人工标准选择", async ({
  page,
}) => {
  const { path, project } = await setup(page);
  await create(page, "参考图角色");
  const assetId = crypto.randomUUID(),
    versionId = crypto.randomUUID();
  // UI contract fixture only; actual reference ownership/status persistence is tested in the backend.
  const referencesPath = "/api/v1/projects/" + project.id + "/reference-images";
  let failed = true;
  await page.route("**" + referencesPath + "?*", (route) =>
    failed
      ? route.abort("failed")
      : route.fulfill({
          json: [
            {
              id: versionId,
              asset_id: assetId,
              asset_name: "已批准参考",
              asset_type: "IMAGE",
              version_no: 7,
              status: "APPROVED",
              stale: false,
            },
          ],
        }),
  );
  await page.getByRole("button", { name: "编辑角色设定", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重试加载参考图版本", exact: true }),
  ).toBeVisible();
  failed = false;
  await page
    .getByRole("button", { name: "重试加载参考图版本", exact: true })
    .click();
  await page
    .getByRole("combobox", { name: "已有精确图片版本", exact: true })
    .click();
  await page
    .getByRole("option", { name: "已批准参考 · V7 · APPROVED", exact: true })
    .click();
  await page.getByRole("combobox", { name: "参考图用途", exact: true }).click();
  await page.getByRole("option", { name: "初始设定图", exact: true }).click();
  await page.getByLabel("参考图名称", { exact: true }).fill("人工标准正面");
  await page
    .getByRole("button", { name: "添加精确参考图", exact: true })
    .click();
  await page.getByRole("combobox", { name: "标准参考图", exact: true }).click();
  await page.getByRole("option", { name: "人工标准正面", exact: true }).click();
  let payload: Record<string, any> | null = null;
  await page.route("**" + path + "/*/character-profile", (route) => {
    payload = route.request().postDataJSON();
    return route.abort("failed");
  });
  await page.getByRole("button", { name: "保存设定", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
  expect(payload!.profile.images[0].source).toEqual({
    asset_id: assetId,
    asset_version_id: versionId,
  });
  expect(payload!.profile.standard_image_id).toBe(
    payload!.profile.images[0].id,
  );
  await page
    .getByRole("button", { name: "移除参考图 人工标准正面", exact: true })
    .click();
  await expect(
    page.getByRole("combobox", { name: "标准参考图", exact: true }),
  ).toContainText("未指定");
});

test("ASSET-02：来源、外观和声明范围持久化及移动端", async ({
  page,
}, testInfo) => {
  const { headers, path, project } = await setup(page);
  const episode = await page.request.post(
    "/api/v1/projects/" + project.id + "/episodes",
    { headers, data: { episode_no: 1, title: "范围集" } },
  );
  expect(episode.status()).toBe(201);
  await page.getByRole("button", { name: "新建视觉资产", exact: true }).click();
  await page.getByLabel("资产名称", { exact: true }).fill("资料角色");
  await page.getByRole("combobox", { name: "创建来源", exact: true }).click();
  await page.getByRole("option", { name: "人工登记导入", exact: true }).click();
  await page.getByLabel("来源说明", { exact: true }).fill("授权参考登记");
  await save(page).click();
  await page.getByRole("button", { name: "编辑资产资料", exact: true }).click();
  await page.getByLabel("资产简介", { exact: true }).fill("雨中的少年");
  await page.getByLabel("资产标签（逗号分隔）").fill("主角，雨夜");
  await page.getByLabel("发型", { exact: true }).fill("黑色短发");
  await page.getByRole("combobox", { name: "使用范围", exact: true }).click();
  await page.getByRole("option", { name: "指定剧集", exact: true }).click();
  await page.getByRole("checkbox", { name: "第1集 · 范围集" }).click();
  await page.getByRole("button", { name: "保存资产资料", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  const row = (await (await page.request.get(path, { headers })).json())[0];
  expect(row.metadata.description).toBe("雨中的少年");
  expect(row.metadata.tags).toEqual(["主角", "雨夜"]);
  expect(row.metadata.usage_scope.episode_ids).toEqual([
    (await episode.json()).id,
  ]);
  expect(row.creation_source).toEqual({
    kind: "IMPORTED",
    note: "授权参考登记",
  });
  expect(row.created_by).toBeTruthy();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page
    .getByRole("button", { name: "角色 · 资料角色", exact: true })
    .click();
  await expect(page.getByText("黑色短发", { exact: true })).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("metadata-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await page.getByRole("button", { name: "编辑资产资料", exact: true }).click();
  await expect(page.getByLabel("资产简介", { exact: true })).toHaveValue(
    "雨中的少年",
  );
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("metadata-mobile.png"),
    fullPage: true,
  });
});

test("ASSET-02：失败保留资料草稿、冲突拒绝覆盖与防重提交", async ({ page }) => {
  const { headers, path } = await setup(page);
  await create(page, "冲突资料");
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const url = path + "/" + row.id + "/metadata";
  await page.getByRole("button", { name: "编辑资产资料", exact: true }).click();
  await page.getByLabel("资产简介", { exact: true }).fill("本地资料");
  await page.route("**" + url, (route) => route.abort("failed"));
  await page.getByRole("button", { name: "保存资产资料", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
  await expect(page.getByLabel("资产简介", { exact: true })).toHaveValue(
    "本地资料",
  );
  await page.unroute("**" + url);
  let puts = 0;
  await page.route("**" + url, async (route) => {
    puts++;
    await new Promise((resolve) => setTimeout(resolve, 200));
    await route.continue();
  });
  await page
    .getByRole("button", { name: "保存资产资料", exact: true })
    .evaluate((button: HTMLButtonElement) => {
      button.click();
      button.click();
    });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(puts).toBe(1);
  await page.unroute("**" + url);
  await page.getByRole("button", { name: "编辑资产资料", exact: true }).click();
  expect(
    (
      await page.request.put(path + "/" + row.id, {
        headers,
        data: { name: "远程名称", expected_revision: 1 },
      })
    ).status(),
  ).toBe(200);
  await page.getByLabel("资产简介", { exact: true }).fill("过期资料草稿");
  await page.getByRole("button", { name: "保存资产资料", exact: true }).click();
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("资产已被修改");
  await expect(page.getByLabel("资产简介", { exact: true })).toHaveValue(
    "过期资料草稿",
  );
  await page.getByRole("button", { name: "取消", exact: true }).click();
  await expect(
    page.getByText("放弃未保存的资产资料？", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "放弃修改并关闭", exact: true })
    .click();
  expect(
    (await (await page.request.get(path + "/" + row.id, { headers })).json())
      .metadata.description,
  ).toBe("本地资料");
});

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
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("网络连接失败");
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
  await expect(
    page.getByRole("alert").and(page.locator('[data-slot="alert"]')),
  ).toContainText("资产已被修改");
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
  await fillImageImport(page);
  await page
    .getByRole("button", { name: "保存新候选版本", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
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
    page.getByRole("heading", { name: "只读资产 · 修订 1", exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "查看资产版本 v1 · 候选草稿", exact: true })
    .click();
  await expect(
    page.getByRole("img", { name: "精确资产版本原图" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "导入新图片版本", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "提交版本审核", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByText("当前项目只读，可查看视觉资产。", { exact: true }),
  ).toBeVisible();
  for (const name of [
    "新建视觉资产",
    "编辑资产名称",
    "编辑资产资料",
    "编辑角色设定",
    "归档视觉资产",
  ])
    await expect(page.getByRole("button", { name, exact: true })).toHaveCount(
      0,
    );
});
