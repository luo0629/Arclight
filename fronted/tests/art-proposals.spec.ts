import { expect, test, type Page } from "@playwright/test";
import { createHash } from "node:crypto";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `art-${crypto.randomUUID()}@example.com`,
      password: "art-review-browser-password",
      display_name: "美术提案验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "美术提案项目" },
  });
  const project = await created.json();
  const path = `/api/v1/projects/${project.id}/art-proposals`;
  await page.goto("/");
  await open(page);
  return { headers, project, path };
}
async function open(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "视觉资产", exact: true }).click();
  await page.getByText("美术提案与审核", { exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "美术提案", exact: true }),
  ).toBeVisible();
}
async function fill(page: Page, goal = "雨夜视觉目标") {
  await page.getByRole("button", { name: "新建美术提案", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("视觉目标", { exact: true }).fill(goal);
  await dialog
    .getByLabel("人工来源说明", { exact: true })
    .fill("原创人工视觉方案，保留原有风格");
  await dialog.getByLabel("方案名称 1", { exact: true }).fill("蓝灰方案");
  await dialog
    .getByLabel("方案依据 1", { exact: true })
    .fill("用低饱和背景突出人物");
  await dialog.getByLabel("设定名称 1.1", { exact: true }).fill("雨夜主角");
  await dialog
    .getByLabel("设定简介 1.1", { exact: true })
    .fill("黑发，衣着朴素");
  await dialog
    .getByLabel("正向提示词 1.1", { exact: true })
    .fill("水墨轮廓，灰蓝雨夜，保留纸张纹理");
  return dialog;
}
async function save(page: Page) {
  await page.getByRole("button", { name: "保存美术提案", exact: true }).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(page.getByLabel("美术提案详情", { exact: true })).toBeVisible();
}
async function approve(page: Page) {
  const detail = page.getByLabel("美术提案详情", { exact: true });
  const button = detail.getByRole("button", {
    name: "批准并生成正式版本",
    exact: true,
  });
  await expect(button).toBeDisabled();
  const checkboxes = detail.getByRole("checkbox");
  expect(await checkboxes.count()).toBeGreaterThan(0);
  for (const checkbox of await checkboxes.all()) await checkbox.check();
  await button.click();
  await expect(detail.getByText("已批准", { exact: true })).toBeVisible();
}

test("ART-04：真实人工提案、刷新审核、正式新版本、修订历史与归档恢复", async ({
  page,
}, testInfo) => {
  const { headers, project, path } = await setup(page);
  await fill(page);
  await save(page);
  await page
    .getByRole("button", { name: "提交美术提案审核", exact: true })
    .click();
  await expect(
    page.getByText("待人工审核", { exact: true }).first(),
  ).toBeVisible();
  await page.reload();
  await open(page);
  const detail = page.getByLabel("美术提案详情", { exact: true });
  await expect(detail.getByText("待人工审核", { exact: true })).toBeVisible();
  await detail
    .getByLabel("美术审核意见", { exact: true })
    .fill("同意此精确方案");
  await approve(page);
  const rows = await (await page.request.get(path, { headers })).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].outputs).toHaveLength(1);
  const assets = await (
    await page.request.get(`/api/v1/projects/${project.id}/assets`, { headers })
  ).json();
  expect(assets[0].name).toBe("雨夜主角");
  expect(assets[0].current_version_id).toBeNull();
  await detail
    .getByRole("button", { name: "修订当前提案", exact: true })
    .click();
  await page.getByLabel("视觉目标", { exact: true }).fill("雨夜视觉目标修订");
  await save(page);
  await detail
    .getByRole("button", { name: "查看提案历史", exact: true })
    .click();
  await detail
    .getByRole("button", { name: "查看提案 v1", exact: true })
    .click();
  await expect(
    detail.getByRole("heading", { name: "雨夜视觉目标", exact: true }),
  ).toBeVisible();
  await expect(
    detail.getByText("正在查看历史版本，当前版本为 v2。", { exact: true }),
  ).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("art-history-desktop.png"),
    fullPage: true,
  });
  page.on("dialog", (dialog) => dialog.accept());
  await detail
    .getByRole("button", { name: "归档美术提案", exact: true })
    .click();
  await expect(detail.getByText("已归档", { exact: true })).toBeVisible();
  await page.reload();
  await open(page);
  await detail
    .getByRole("button", { name: "恢复美术提案", exact: true })
    .click();
  await expect(detail.getByText("提案草稿", { exact: true })).toBeVisible();
});

test("ART-04：本机草稿刷新恢复与丢失保存响应后的幂等重试", async ({ page }) => {
  const { headers, path } = await setup(page);
  await fill(page, "本机草稿恢复目标");
  page.on("dialog", (dialog) => dialog.accept());
  await page.reload();
  await open(page);
  await page.getByRole("button", { name: "新建美术提案", exact: true }).click();
  await expect(
    page.getByText("已恢复本机美术提案草稿，保留原始基准。", { exact: true }),
  ).toBeVisible();
  await expect(page.getByLabel("视觉目标", { exact: true })).toHaveValue(
    "本机草稿恢复目标",
  );
  let dropped = false;
  await page.route(new RegExp(path + "$"), async (route) => {
    if (route.request().method() === "POST" && !dropped) {
      dropped = true;
      await route.fetch();
      await route.abort("failed");
    } else await route.continue();
  });
  await page.getByRole("button", { name: "保存美术提案", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("视觉目标", { exact: true })).toHaveValue(
    "本机草稿恢复目标",
  );
  await page.reload();
  await open(page);
  await page.getByRole("button", { name: "新建美术提案", exact: true }).click();
  await save(page);
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    1,
  );
});

test("ART-04：锁定规范逐项确认、拒绝历史与窄屏比较", async ({
  page,
}, testInfo) => {
  const { headers, project, path } = await setup(page);
  const biblePath = `/api/v1/projects/${project.id}/visual-bible`;
  const first = await page.request.post(biblePath + "/versions", {
    headers,
    data: {
      expected_version_no: 0,
      creation_key: crypto.randomUUID(),
      fields: { palette: "黑白" },
    },
  });
  expect(first.status()).toBe(201);
  const locked = await page.request.post(biblePath + "/locks", {
    headers,
    data: {
      expected_version_no: 1,
      creation_key: crypto.randomUUID(),
      locked_fields: ["palette"],
    },
  });
  expect(locked.status()).toBe(201);
  await fill(page, "锁定规范调整");
  await page
    .getByRole("combobox", { name: "设定类型 1.1", exact: true })
    .click();
  await page.getByRole("option", { name: "视觉规范", exact: true }).click();
  await page.getByLabel("主要配色提案", { exact: true }).fill("蓝灰");
  await save(page);
  const detail = page.getByLabel("美术提案详情", { exact: true });
  await expect(detail.getByText("已锁定", { exact: true })).toBeVisible();
  await page
    .getByRole("button", { name: "提交美术提案审核", exact: true })
    .click();
  await page.setViewportSize({ width: 390, height: 844 });
  await expect(
    detail.getByRole("button", { name: "批准并生成正式版本", exact: true }),
  ).toBeDisabled();
  await page.screenshot({
    path: testInfo.outputPath("art-locked-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await approve(page);
  const current = await (await page.request.get(biblePath, { headers })).json();
  expect(current.fields.palette).toBe("蓝灰");
  expect(current.locked_fields).toEqual(["palette"]);
  expect(current.version_no).toBe(3);
  await page.setViewportSize({ width: 1280, height: 900 });
  await fill(page, "被拒绝的人工提案");
  await save(page);
  await page
    .getByRole("button", { name: "提交美术提案审核", exact: true })
    .click();
  await detail
    .getByLabel("美术审核意见", { exact: true })
    .fill("保持原来的角色方向");
  await detail
    .getByRole("button", { name: "拒绝美术提案", exact: true })
    .click();
  await expect(detail.getByText("已拒绝", { exact: true })).toBeVisible();
  await page.reload();
  await open(page);
  await expect(detail.getByText(/拒绝意见：保持原来的角色方向/)).toBeVisible();
  const rows = await (await page.request.get(path, { headers })).json();
  expect(
    rows.find(
      (row: { version: { proposal_json: { visual_goal: string } } }) =>
        row.version.proposal_json.visual_goal === "被拒绝的人工提案",
    ).outputs,
  ).toEqual([]);
});

test("ART-04：并发修订冲突保留草稿并更新基准后另存", async ({ page }) => {
  const { headers, path } = await setup(page);
  await fill(page, "并发修订目标");
  await save(page);
  const original = (
    await (await page.request.get(path, { headers })).json()
  )[0];
  await page.getByRole("button", { name: "修订当前提案", exact: true }).click();
  await page.getByLabel("视觉目标", { exact: true }).fill("浏览器保留的修订");
  const concurrent = await page.request.put(`${path}/${original.id}`, {
    headers,
    data: {
      creation_key: crypto.randomUUID(),
      expected_revision: original.revision,
      proposal: {
        ...original.version.proposal_json,
        visual_goal: "其他标签页修订",
      },
    },
  });
  expect(concurrent.status()).toBe(201);
  await page.getByRole("button", { name: "保存美术提案", exact: true }).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "提案已变化",
  );
  await expect(page.getByLabel("视觉目标", { exact: true })).toHaveValue(
    "浏览器保留的修订",
  );
  const refreshed = page.waitForResponse(
    (response) =>
      response.request().method() === "GET" &&
      response.url().endsWith(`${path}/${original.id}`),
  );
  await page
    .getByRole("button", { name: "更新提案基准并保留草稿", exact: true })
    .click();
  await refreshed;
  await save(page);
  await expect(
    page
      .getByLabel("美术提案详情", { exact: true })
      .getByRole("heading", { name: "浏览器保留的修订", exact: true }),
  ).toBeVisible();
  const versions = await (
    await page.request.get(`${path}/${original.id}/versions`, { headers })
  ).json();
  expect(
    versions.map((version: { version_no: number }) => version.version_no),
  ).toEqual([3, 2, 1]);
});

test("ART-04：已保存人工决定在刷新后继续生效", async ({ page }) => {
  const { headers, path } = await setup(page);
  await fill(page, "人工决定恢复");
  await save(page);
  await page
    .getByRole("button", { name: "提交美术提案审核", exact: true })
    .click();
  await expect(
    page
      .getByLabel("美术提案详情", { exact: true })
      .getByText("待人工审核", { exact: true }),
  ).toBeVisible();
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const confirmation = row.review_confirmation;
  const canonical = JSON.stringify({
    comparison_sha256: confirmation.comparison_sha256,
    confirmed_changes: confirmation.confirmed_changes.map(
      (change: { design_id: string; field_path: string }) => ({
        design_id: change.design_id,
        field_path: change.field_path,
      }),
    ),
    option_id: confirmation.option_id,
    schema_version: confirmation.schema_version,
  });
  const accepted = await page.request.post(
    `/api/v1/human-reviews/${row.review.id}/decisions`,
    {
      headers,
      data: {
        version_id: row.version.id,
        request_sha256: row.review.request_sha256,
        decision: "APPROVE",
        comment: "独立保存的人工决定",
        impact_digest: createHash("sha256").update(canonical).digest("hex"),
      },
    },
  );
  expect(accepted.status()).toBe(200);
  await page.reload();
  await open(page);
  await page
    .getByRole("button", { name: "使已保存决定生效", exact: true })
    .click();
  await expect(
    page
      .getByLabel("美术提案详情", { exact: true })
      .getByText("已批准", { exact: true }),
  ).toBeVisible();
  const current = await (
    await page.request.get(`${path}/${row.id}`, { headers })
  ).json();
  expect(current.outputs).toHaveLength(1);
  expect(current.review.decision.decision_json.comment).toBe(
    "独立保存的人工决定",
  );
});

test("ART-04：只读成员查看提案且没有创建或批准入口", async ({ page }) => {
  const { headers, project, path } = await setup(page);
  await fill(page, "只读成员提案");
  await save(page);
  await page
    .getByRole("button", { name: "提交美术提案审核", exact: true })
    .click();
  await expect(
    page
      .getByLabel("美术提案详情", { exact: true })
      .getByText("待人工审核", { exact: true }),
  ).toBeVisible();
  const row = (await (await page.request.get(path, { headers })).json())[0];
  const email = `viewer-${crypto.randomUUID()}@example.com`;
  const viewer = await page.request.post("/api/v1/auth/register", {
    data: {
      email,
      password: "art-review-browser-password",
      display_name: "只读美术成员",
    },
  });
  expect(viewer.status()).toBe(201);
  const member = await page.request.post(
    `/api/v1/projects/${project.id}/members`,
    { headers, data: { email, role: "viewer" } },
  );
  expect(member.status()).toBe(201);
  await page.reload();
  await open(page);
  await page
    .getByRole("navigation", { name: "美术提案列表", exact: true })
    .getByRole("button", { name: /只读成员提案/ })
    .click();
  const detail = page.getByLabel("美术提案详情", { exact: true });
  await expect(
    detail.getByRole("heading", { name: "只读成员提案", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "新建美术提案", exact: true }),
  ).toHaveCount(0);
  await expect(
    detail.getByRole("button", { name: "批准并生成正式版本", exact: true }),
  ).toHaveCount(0);
  const denied = await page.request.post(`${path}/${row.id}/decisions`, {
    headers: { Authorization: "Bearer " + (await viewer.json()).access_token },
    data: {
      version_id: row.version.id,
      expected_revision: row.revision,
      request_sha256: row.review.request_sha256,
      decision: "REJECT",
    },
  });
  expect(denied.status()).toBe(403);
});
