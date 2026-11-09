import { expect, test } from "@playwright/test";
import { continuityFixture } from "./continuity-helpers";

test("QA-08：桌面逐图比较、精确依据、旧审查禁选与跨资产轮次", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const env = await continuityFixture(page);
  const dialog = await env.open();
  const model = env.models[1];
  await expect(
    dialog.getByText("红色箱子应保持关闭。", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("核对候选图右下角箱盖与固定道具图。", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("图中区域（比例）：x 0.5，y 0.5，宽 0.3，高 0.3", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByText("置信等级：高（未经概率校准）", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("img", {
      name: "当前检查图片 · " + model.version.id,
      exact: true,
    }),
  ).toBeVisible();
  const pair = dialog.getByTestId("continuity-image-pair");
  const boxes = await pair.locator(":scope > figure").evaluateAll((elements) =>
    elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y };
    }),
  );
  expect(boxes).toHaveLength(2);
  expect(Math.abs(boxes[0].y - boxes[1].y)).toBeLessThan(2);
  expect(boxes[1].x).toBeGreaterThan(boxes[0].x);
  await dialog
    .getByRole("heading", { name: "原图与修订图", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("qa08-desktop-comparison.png"),
    fullPage: true,
  });
  await dialog
    .getByRole("combobox", { name: "查看审查轮次", exact: true })
    .click();
  await page
    .getByRole("option", { name: "第 1 次 · 审查通过 · 历史", exact: true })
    .click();
  await expect(
    dialog.getByText(
      "正在查看历史审查。请切回最新审查后操作；旧结论不能替代当前门禁。",
      { exact: true },
    ),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "提交版本审核", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("combobox", { name: "查看审查轮次", exact: true })
    .click();
  await page
    .getByRole("option", { name: "第 2 次 · 需要修订 · 最新", exact: true })
    .click();
  const references = dialog.getByLabel("精确审查参考", { exact: true });
  await expect(references).toContainText(
    model.reviews[0].references[1].reference.resource_id,
  );
  await expect(references).toContainText(
    model.reviews[0].references[1].reference.content_sha256,
  );
  await references.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("qa08-desktop-evidence.png"),
    fullPage: true,
  });
  await dialog
    .getByText("修订第 1 轮 / 最多 3 轮 · 已排队", { exact: true })
    .click();
  await expect(
    dialog.getByText("对照修订新图与冻结参考后，保留本轮依据。", {
      exact: true,
    }),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "检查此轮图片与报告", exact: true })
    .click();
  await expect(
    dialog.getByLabel("当前候选状态", { exact: true }),
  ).toContainText("原始候选图");
  expect(
    env.reads.some((url) =>
      url.includes(
        `/assets/${env.models[0].asset.id}/versions/${env.models[0].version.id}/continuity`,
      ),
    ),
  ).toBe(true);
  expect(env.models[0].asset.id).not.toBe(model.asset.id);
  await dialog.getByRole("button", { name: "关闭比较", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "逐图比较与审核", exact: true }),
  ).toBeFocused();
  expect(env.mutations).toHaveLength(0);
});

test("QA-08：移动端精确人工决定、刷新恢复、独立批准及 Owner 选用 CAS", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await continuityFixture(page, true);
  let dialog = await env.open();
  const model = env.models[1],
    row = model.human_reviews[0];
  await expect(
    dialog.getByText("缺少参考：箱盖被遮挡，需要 Owner 核实精确图片。", {
      exact: true,
    }),
  ).toBeVisible();
  const pair = dialog.getByTestId("continuity-image-pair");
  const boxes = await pair.locator(":scope > figure").evaluateAll((elements) =>
    elements.map((element) => {
      const rect = element.getBoundingClientRect();
      return { x: rect.x, y: rect.y, right: rect.right };
    }),
  );
  expect(Math.abs(boxes[0].x - boxes[1].x)).toBeLessThan(2);
  expect(boxes[1].y).toBeGreaterThan(boxes[0].y);
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
  await expect(
    dialog.getByRole("button", { name: "接受本次例外", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("textbox", { name: "人工处理意见（必填）", exact: true })
    .fill("  已逐图核实箱盖状态，接受本次遮挡例外。  ");
  await dialog
    .getByRole("button", { name: "接受本次例外", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "恢复原执行", exact: true }),
  ).toBeEnabled();
  expect(env.mutations[0]).toEqual({
    path: `/api/v1/human-reviews/${row.id}/decisions`,
    body: {
      version_id: model.version.id,
      request_sha256: row.request_sha256,
      decision: "APPROVE",
      comment: "已逐图核实箱盖状态，接受本次遮挡例外。",
    },
  });
  expect(env.mutations).toHaveLength(1);
  expect(model.version.status).toBe("DRAFT");
  expect(model.asset.current_version_id).toBeNull();
  await page.reload();
  await page
    .getByRole("button", { name: "逐图比较与审核", exact: true })
    .click();
  dialog = page.getByRole("dialog", {
    name: "逐图比较与连续性审核",
    exact: true,
  });
  const resume = dialog.getByRole("button", {
    name: "恢复原执行",
    exact: true,
  });
  await expect(resume).toBeEnabled();
  await expect(
    dialog.getByRole("button", { name: "接受本次例外", exact: true }),
  ).toHaveCount(0);
  await resume.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("qa08-mobile-recovery.png"),
    fullPage: true,
  });
  await resume.click();
  await expect(
    dialog.getByRole("button", { name: "提交版本审核", exact: true }),
  ).toBeEnabled();
  expect(env.mutations[1]).toEqual({
    path: `/api/v1/human-reviews/${row.id}/resume`,
    body: { decision_id: row.decision!.id, request_sha256: row.request_sha256 },
  });
  await dialog
    .getByRole("button", { name: "提交版本审核", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "批准此精确版本", exact: true }),
  ).toBeEnabled();
  expect(env.mutations[2].body).toEqual({
    expected_revision: 3,
    status: "READY_FOR_REVIEW",
    comment: "",
  });
  await dialog
    .getByRole("button", { name: "批准此精确版本", exact: true })
    .click();
  const select = dialog.getByRole("button", {
    name: "正式选用此图片",
    exact: true,
  });
  await expect(select).toBeEnabled();
  expect(env.mutations[3].body).toEqual({
    expected_revision: 4,
    status: "APPROVED",
    comment: "",
  });
  expect(model.asset.current_version_id).toBeNull();
  await select.click();
  await expect(select).toBeDisabled();
  expect(env.mutations[4]).toEqual({
    path: `/api/v1/projects/${env.project.id}/assets/${model.asset.id}/versions/${model.version.id}/select`,
    body: { expected_revision: 7, expected_lock_version: 2 },
  });
  expect(model.asset.current_version_id).toBe(model.version.id);
  expect(
    env.mutations.filter((item) => item.path.endsWith("/decisions")),
  ).toHaveLength(1);
});

test("QA-08：拒绝例外后恢复仍保持禁选", async ({ page }) => {
  const env = await continuityFixture(page, true);
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "人工处理意见（必填）", exact: true })
    .fill("缺少明确道具证据，拒绝本次图片。");
  await dialog
    .getByRole("button", { name: "拒绝本次例外", exact: true })
    .click();
  await dialog.getByRole("button", { name: "恢复原执行", exact: true }).click();
  await expect(
    dialog.getByText("恢复结果：已拒绝", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "提交版本审核", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  expect(env.mutations[0].body.decision).toBe("REJECT");
  expect(env.models[1].asset.current_version_id).toBeNull();
});

for (const status of [403, 404])
  test(`QA-08：${status} 撤权或跨项目后清空旧报告与动作`, async ({ page }) => {
    const env = await continuityFixture(page);
    const dialog = await env.open();
    await expect(
      dialog.getByLabel("连续性审查报告", { exact: true }),
    ).toBeVisible();
    env.control.readStatus = status;
    await dialog
      .getByRole("button", { name: "刷新审查与权限", exact: true })
      .click();
    await expect(
      dialog.getByText("当前项目权限已变化，请重新核对。", { exact: true }),
    ).toBeVisible();
    await expect(
      dialog.getByLabel("连续性审查报告", { exact: true }),
    ).toHaveCount(0);
    await expect(dialog.getByRole("img")).toHaveCount(0);
    await expect(
      dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
    ).toHaveCount(0);
    expect(env.mutations).toHaveLength(0);
  });

test("QA-08：服务端拒绝过期选用后刷新门禁，操作不自动重试", async ({
  page,
}) => {
  const env = await continuityFixture(page);
  const model = env.models[1];
  model.version.status = "APPROVED";
  model.actions.blocker = null;
  model.actions.can_select = true;
  const dialog = await env.open();
  env.control.mutationError = (current) => {
    current.actions.can_select = false;
    current.actions.blocker = {
      code: "CONTINUITY_FEEDBACK_PENDING",
      message: "候选的新反馈尚未完成或交接",
    };
    current.asset.revision++;
  };
  await dialog
    .getByRole("button", { name: "正式选用此图片", exact: true })
    .click();
  await expect(
    dialog.getByText("版本或审查已变化，请刷新后重试。", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByText("候选的新反馈尚未完成或交接", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  expect(env.mutations).toHaveLength(1);
  expect(model.asset.current_version_id).toBeNull();
});

test("QA-08：翻页遇到新审查时重新读取最新页", async ({ page }) => {
  const env = await continuityFixture(page);
  const model = env.models[1];
  model.next_offset = 20;
  model.reviews = model.reviews.slice(0, 1);
  const dialog = await env.open();
  env.control.interceptRead = async (route, current) => {
    const query = new URL(route.request().url()).searchParams;
    if (query.get("offset") === "20") {
      current.latest_review_id = crypto.randomUUID();
      current.reviews[0].record.review.result_id = current.latest_review_id;
      current.reviews[0].record.sequence = 3;
      current.next_offset = null;
    }
    await route.fulfill({ json: current });
  };
  await dialog
    .getByRole("button", { name: "加载更早审查", exact: true })
    .click();
  await expect(
    dialog.getByText("已出现新一轮审查，已刷新为最新记录，请重新核对后操作。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("combobox", { name: "查看审查轮次", exact: true }),
  ).toContainText("第 3 次");
  expect(env.reads.at(-1)).not.toContain("offset=20");
  expect(env.mutations).toHaveLength(0);
});

for (const missing of ["review", "media"])
  test(`QA-08：缺少 ${missing} 证据时可查看而禁选`, async ({ page }) => {
    const env = await continuityFixture(page);
    const model = env.models[1];
    model.reviews = [];
    model.latest_review_id = null;
    model.actions.blocker = {
      code:
        missing === "media"
          ? "MEDIA_CHECK_REQUIRED"
          : "CONTINUITY_REVIEW_REQUIRED",
      message: "候选图片缺少必需的精确证据",
    };
    if (missing === "media") model.candidate!.media_check = null;
    const dialog = await env.open();
    await expect(
      dialog.getByRole("heading", { name: "尚无连续性审查记录", exact: true }),
    ).toBeVisible();
    await expect(
      dialog.getByRole("button", { name: "批准此精确版本", exact: true }),
    ).toBeDisabled();
    await expect(
      dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
    ).toBeDisabled();
    expect(env.mutations).toHaveLength(0);
  });

test("QA-08：切换精确图片取消旧请求，迟到数据不回填", async ({ page }) => {
  const env = await continuityFixture(page);
  let release = () => {};
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let waiting = false;
  env.control.interceptRead = async (route, model) => {
    if (model === env.models[0]) {
      waiting = true;
      await delayed;
    }
    await route.fulfill({ json: model }).catch(() => {});
  };
  await page.goto(env.href(0));
  await page
    .getByRole("button", { name: "逐图比较与审核", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "逐图比较与连续性审核",
    exact: true,
  });
  await expect.poll(() => waiting).toBe(true);
  await expect(
    dialog.getByText("正在读取精确图片与审查记录…", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "关闭比较", exact: true }).click();
  await page.evaluate(
    (href) => window.history.pushState(null, "", href),
    env.href(1),
  );
  await expect(page.getByLabel("画布资产详情", { exact: true })).toContainText(
    "修订候选图",
  );
  await page
    .getByRole("button", { name: "逐图比较与审核", exact: true })
    .click();
  await expect(
    dialog.getByLabel("当前候选状态", { exact: true }),
  ).toContainText(env.models[1].version.id);
  release();
  await expect(
    dialog.getByLabel("当前候选状态", { exact: true }),
  ).not.toContainText(env.models[0].version.id);
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  expect(env.mutations).toHaveLength(0);
});

test("QA-08：尚未完成审查的人工请求仅允许拒绝", async ({ page }) => {
  const env = await continuityFixture(page, true);
  const model = env.models[1];
  model.reviews = [];
  model.latest_review_id = null;
  model.human_reviews[0].can_approve = false;
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "人工处理意见（必填）", exact: true })
    .fill("审查未完成，保留原图并拒绝本次交接。");
  await expect(
    dialog.getByRole("button", { name: "接受本次例外", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("button", { name: "拒绝本次例外", exact: true }),
  ).toBeEnabled();
  await expect(
    dialog.getByText(
      "本轮尚未完成必需审查，只能拒绝本次请求；人工决定不能替代审查结论。",
      { exact: true },
    ),
  ).toBeVisible();
  await dialog
    .getByRole("button", { name: "拒绝本次例外", exact: true })
    .click();
  await dialog.getByRole("button", { name: "恢复原执行", exact: true }).click();
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  expect(env.mutations[0].body.decision).toBe("REJECT");
});

test("QA-08：Viewer 读取报告而无人工处理或选用权限", async ({ page }) => {
  const env = await continuityFixture(page, true);
  const model = env.models[1];
  model.actions.owner = false;
  model.actions.writable = false;
  model.human_reviews[0].can_approve = false;
  const dialog = await env.open();
  await expect(
    dialog.getByLabel("连续性审查报告", { exact: true }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "接受本次例外", exact: true }),
  ).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  await expect(
    dialog.getByRole("textbox", {
      name: "版本审核意见（拒绝时必填）",
      exact: true,
    }),
  ).toBeDisabled();
  expect(env.mutations).toHaveLength(0);
});

test("QA-08：原图缺失暂停版本选用，刷新成功后才能操作", async ({ page }) => {
  const env = await continuityFixture(page);
  const model = env.models[1];
  model.version.status = "APPROVED";
  model.actions.blocker = null;
  model.actions.can_select = true;
  const pattern = `**/api/v1/projects/${env.project.id}/assets/**/image`;
  await page.route(pattern, (route) =>
    route.fulfill({
      status: 409,
      json: {
        error: {
          code: "ASSET_OBJECT_UNAVAILABLE",
          message: "图片缺失或完整性校验失败",
        },
      },
    }),
  );
  const dialog = await env.open();
  const error = dialog.getByText(
    "原图读取未完成，版本操作已暂停。请刷新后重新核对：图片缺失或完整性校验失败",
    { exact: true },
  );
  await expect(error).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeDisabled();
  await page.unroute(pattern);
  await dialog
    .getByRole("button", { name: "刷新审查与权限", exact: true })
    .click();
  await expect(
    dialog.getByRole("img", {
      name: "当前检查图片 · " + model.version.id,
      exact: true,
    }),
  ).toBeVisible();
  await expect(error).toHaveCount(0);
  await expect(
    dialog.getByRole("button", { name: "正式选用此图片", exact: true }),
  ).toBeEnabled();
  expect(env.mutations).toHaveLength(0);
});
