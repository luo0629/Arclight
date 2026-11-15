import { writeFile } from "node:fs/promises";
import { test, expect } from "@playwright/test";
import { revisionOperations } from "../src/features/editor/shot-revision-types";
import { apiFailure } from "./shot-production-helpers";
import { shotRevisionFixture } from "./shot-revision-helpers";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("ui04-result.json"),
    JSON.stringify(
      {
        title: info.title,
        status: info.status,
        expectedStatus: info.expectedStatus,
        retry: info.retry,
        errors: info.errors,
      },
      null,
      2,
    ),
  );
});

for (const [operation, label] of Object.entries(revisionOperations)) {
  test(`UI-04：${label}绑定精确原图、预算确认与单张新候选`, async ({
    page,
  }, info) => {
    if (operation === "RECOMPOSE")
      await page.setViewportSize({ width: 1280, height: 1000 });
    const env = await shotRevisionFixture(page);
    const dialog = await env.open();
    await expect(dialog).toContainText("当前不提供遮罩编辑或像素保持");
    await expect(dialog).toContainText(env.candidate.version_id);
    await dialog.getByLabel(label, { exact: true }).click();
    const instruction = `调整${label}，保留人物身份与动作接续。`;
    await dialog
      .getByRole("textbox", { name: "具体修订要求", exact: true })
      .fill(instruction);
    await dialog
      .getByRole("button", { name: "预览修订与预算", exact: true })
      .click();
    const preview = dialog.getByRole("region", {
      name: "单镜头修订预览与结果",
      exact: true,
    });
    await expect(preview).toContainText(`${label} · 1 张新候选`);
    await expect(preview).toContainText("0.050000 USD");
    await expect(preview).toContainText("mock / mock-image-v1");
    expect(env.requests).toHaveLength(0);
    expect(env.previews).toEqual([
      {
        candidate_id: env.candidate.id,
        storyboard_id: env.board.id,
        shot_id: env.source.shot_id,
        operation,
        instruction,
        mode: "PROMPT_REGENERATION",
      },
    ]);
    if (operation === "RECOMPOSE") {
      await preview.scrollIntoViewIfNeeded();
      await page.screenshot({
        path: info.outputPath("ui04-desktop-preview.png"),
        fullPage: true,
      });
    }
    await preview
      .getByRole("button", { name: "确认生成修订候选", exact: true })
      .click();
    await expect(preview.getByRole("status")).toContainText("已受理修订任务");
    expect(env.receipts.size).toBe(1);
    expect(env.requests[0].command).toMatchObject(env.previews[0]);
    expect(env.requests[0].key).toMatch(/^[0-9a-f-]{36}$/);
    await expect(
      dialog.getByRole("textbox", { name: "具体修订要求", exact: true }),
    ).toBeDisabled();
  });
}

for (const [code, message] of [
  ["SHOT_REVISION_PROTECTED", "构图规范已锁定，请先另存并批准规范及分镜"],
  ["SHOT_REVISION_PREVIEW_CHANGED", "来源或报价已变化，请重新预览"],
]) {
  test(`UI-04：${code}明确阻断且不自动生成`, async ({ page }) => {
    const env = await shotRevisionFixture(page);
    const dialog = await env.open();
    await dialog
      .getByRole("textbox", { name: "具体修订要求", exact: true })
      .fill("调整画面中心");
    if (code === "SHOT_REVISION_PROTECTED")
      env.setPreviewFailure({ code, message });
    await dialog
      .getByRole("button", { name: "预览修订与预算", exact: true })
      .click();
    if (code === "SHOT_REVISION_PREVIEW_CHANGED") {
      await page.route("**" + env.endpoint, (route) =>
        apiFailure(route, code, message),
      );
      await dialog
        .getByRole("button", { name: "确认生成修订候选", exact: true })
        .click();
    }
    await expect(dialog.getByRole("alert")).toContainText(message);
    await expect(
      dialog.getByRole("button", { name: "确认生成修订候选", exact: true }),
    ).toHaveCount(0);
    expect(env.receipts.size).toBe(0);
  });
}

test("UI-04：丢失提交响应后刷新并用原幂等键和冻结请求恢复", async ({
  page,
}) => {
  const env = await shotRevisionFixture(page);
  let dialog = await env.open();
  await dialog.getByLabel("表情调整", { exact: true }).click();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("轻微惊讶，保持人物身份");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  env.loseResponse();
  await dialog
    .getByRole("button", { name: "确认生成修订候选", exact: true })
    .click();
  await expect.poll(env.lossInjected).toBe(true);
  await expect(
    dialog.getByRole("button", { name: "确认原修订提交结果", exact: true }),
  ).toBeEnabled();
  expect(env.receipts.size).toBe(1);
  dialog = await env.open();
  await expect(dialog).toContainText("提交结果待确认");
  await expect(
    dialog.getByRole("textbox", { name: "具体修订要求", exact: true }),
  ).toHaveValue("轻微惊讶，保持人物身份");
  await dialog
    .getByRole("button", { name: "确认原修订提交结果", exact: true })
    .click();
  await expect(dialog.getByRole("status")).toContainText("已受理修订任务");
  expect(env.requests).toHaveLength(2);
  expect(env.requests[1]).toEqual(env.requests[0]);
  expect(env.receipts.size).toBe(1);
});

test("UI-04：受理后刷新失败保留回执，再次打开不会自动提交", async ({
  page,
}) => {
  const env = await shotRevisionFixture(page);
  let dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("修复手部指节");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await page.route("**" + env.productionPath + "?**", (route) =>
    apiFailure(route, "TEMPORARILY_UNAVAILABLE", "生产列表暂时读取失败", 503),
  );
  await dialog
    .getByRole("button", { name: "确认生成修订候选", exact: true })
    .click();
  await expect(dialog.getByRole("status")).toContainText("已受理修订任务");
  await dialog
    .getByRole("button", { name: "关闭局部修订", exact: true })
    .click();
  await expect(
    page
      .getByRole("dialog", { name: "镜头批量生成、审核与重试", exact: true })
      .getByRole("alert"),
  ).toContainText("生产列表暂时读取失败");
  await page.unroute("**" + env.productionPath + "?**");
  await page.route("**" + env.productionPath + "?**", (route) =>
    route.fulfill({ json: env.model }),
  );
  dialog = await env.open();
  await expect(dialog.getByRole("status")).toContainText("已受理修订任务");
  expect(env.requests).toHaveLength(1);
  await dialog
    .getByRole("button", { name: "重新填写修订要求", exact: true })
    .click();
  await expect(
    dialog.getByRole("textbox", { name: "具体修订要求", exact: true }),
  ).toBeEnabled();
  await expect(
    dialog.getByRole("region", { name: "单镜头修订预览与结果", exact: true }),
  ).toHaveCount(0);
  expect(env.requests).toHaveLength(1);
});

test("UI-04：撤权清空候选与待确认记录且不会重放", async ({ page }) => {
  const env = await shotRevisionFixture(page);
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("保持人物姿态调整构图");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  env.loseResponse();
  await dialog
    .getByRole("button", { name: "确认生成修订候选", exact: true })
    .click();
  await expect(
    dialog.getByRole("button", { name: "确认原修订提交结果", exact: true }),
  ).toBeEnabled();
  await page.route("**" + env.endpoint, (route) =>
    apiFailure(route, "FORBIDDEN", "当前项目角色没有此操作权限", 403),
  );
  await dialog
    .getByRole("button", { name: "确认原修订提交结果", exact: true })
    .click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("table", { name: "镜头生产列表", exact: true }),
  ).toHaveCount(0);
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) =>
        key.startsWith("arclight:shot-revision:v1:"),
      ),
    ),
  ).toEqual([]);
  expect(env.receipts.size).toBe(1);
});

test("UI-04：关闭上层工作区后迟到的预览不提交且不污染重开状态", async ({
  page,
}) => {
  const env = await shotRevisionFixture(page);
  let entered!: () => void, release!: () => void, finished!: () => void;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  const ended = new Promise<void>((resolve) => {
    finished = resolve;
  });
  await page.route("**" + env.endpoint + "/preview", async (route) => {
    entered();
    await released;
    await route.fulfill({
      status: 503,
      json: {
        error: { code: "TEMPORARILY_UNAVAILABLE", message: "旧预览读取失败" },
      },
    });
    finished();
  });
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("旧草稿");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await started;
  // Navigating away closes both dialogs while a real async request remains in flight.
  await page.goto(`/projects/${env.project.id}`);
  release();
  await ended;
  const reopened = await env.open();
  await expect(
    reopened.getByRole("textbox", { name: "具体修订要求", exact: true }),
  ).toHaveValue("");
  await expect(reopened.getByRole("alert")).toHaveCount(0);
  expect(env.requests).toHaveLength(0);
});

test("UI-04：来源不一致或无可信报价时不允许确认", async ({ page }) => {
  const env = await shotRevisionFixture(page);
  const dialog = await env.open();
  env.setPrepareOverride((preview) => ({
    ...preview,
    source: { ...preview.source, version_id: crypto.randomUUID() },
  }));
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("调整构图");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "当前精确候选或可信报价不一致",
  );
  await expect(
    dialog.getByRole("button", { name: "确认生成修订候选", exact: true }),
  ).toHaveCount(0);
  env.setPrepareOverride((preview) => ({
    ...preview,
    quote: { ...preview.quote, maximum_cost: "unknown" },
  }));
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText(
    "当前精确候选或可信报价不一致",
  );
  expect(env.requests).toHaveLength(0);
});

test("UI-04：手机键盘填写与预算确认，原图可见且对话框无横向溢出", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await shotRevisionFixture(page);
  const dialog = await env.open();
  const operation = dialog.getByLabel("手部修复", { exact: true });
  await operation.focus();
  await page.keyboard.press("Space");
  const instruction = dialog.getByRole("textbox", {
    name: "具体修订要求",
    exact: true,
  });
  await instruction.fill("修复握灯的手指，保持握持动作与人物身份。");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await expect(
    dialog.getByRole("region", { name: "单镜头修订预览与结果", exact: true }),
  ).toContainText("手部修复 · 1 张新候选");
  await expect(
    dialog.getByRole("button", { name: "确认生成修订候选", exact: true }),
  ).toBeEnabled();
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
  await dialog
    .getByRole("region", { name: "单镜头修订预览与结果", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("ui04-mobile-preview.png"),
    fullPage: true,
  });
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  expect(env.requests).toHaveLength(0);
});

test("UI-04：Viewer只读候选可见且局部修订入口禁用", async ({ page }) => {
  const env = await shotRevisionFixture(page);
  env.model.writable = false;
  env.model.owner = false;
  const dialog = await env.openProduction();
  await dialog
    .getByRole("combobox", { name: "镜头 1 审核候选", exact: true })
    .selectOption(env.candidate.id);
  await expect(
    dialog.getByRole("img", {
      name: `镜头 1 精确候选 ${env.candidate.version_id}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    dialog.getByRole("button", { name: "局部修订镜头 1", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("dialog", { name: "单镜头局部修订", exact: true }),
  ).toHaveCount(0);
  expect(env.requests).toHaveLength(0);
});

test("UI-04：无法持久保存幂等键时阻断提交", async ({ page }) => {
  const env = await shotRevisionFixture(page);
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("保持人物调整构图");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  await page.evaluate(() => {
    const original = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("arclight:shot-revision:v1:"))
        throw new Error("无法保存修订恢复记录");
      return original.call(this, key, value);
    };
  });
  await dialog
    .getByRole("button", { name: "确认生成修订候选", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText("无法保存修订恢复记录");
  await expect(
    dialog.getByRole("button", { name: "确认生成修订候选", exact: true }),
  ).toBeDisabled();
  expect(env.requests).toHaveLength(0);
});

test("UI-04：后台撤权后迟到的受理响应不能重建本地回执", async ({ page }) => {
  const env = await shotRevisionFixture(page);
  env.model.shots[0].job = {
    id: crypto.randomUUID(),
    status: "RUNNING",
    revision: 1,
    attempt_count: 1,
    maximum_cost: "0.050000",
    currency: "USD",
  };
  let entered!: () => void, release!: () => void;
  const started = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const released = new Promise<void>((resolve) => {
    release = resolve;
  });
  env.setSubmissionGate(async () => {
    entered();
    await released;
  });
  const dialog = await env.open();
  await dialog
    .getByRole("textbox", { name: "具体修订要求", exact: true })
    .fill("保持人物调整构图");
  await dialog
    .getByRole("button", { name: "预览修订与预算", exact: true })
    .click();
  const response = page.waitForResponse((result) =>
    result.url().endsWith(env.endpoint),
  );
  await dialog
    .getByRole("button", { name: "确认生成修订候选", exact: true })
    .click();
  await started;
  env.denyRead();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("table", { name: "镜头生产列表", exact: true }),
  ).toHaveCount(0);
  release();
  const lateResponse = await response;
  expect(lateResponse.status()).toBe(201);
  await lateResponse.finished();
  // Wait until the browser has consumed the late response, not an arbitrary sleep.
  await expect.poll(async () => env.receipts.size).toBe(1);
  await page.evaluate(
    () =>
      new Promise<void>((resolve) =>
        requestAnimationFrame(() => requestAnimationFrame(() => resolve())),
      ),
  );
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) =>
        key.startsWith("arclight:shot-revision:v1:"),
      ),
    ),
  ).toEqual([]);
  expect(env.requests).toHaveLength(1);
});
