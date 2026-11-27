import { writeFile } from "node:fs/promises";
import { test, expect } from "@playwright/test";
import {
  apiFailure,
  shotProductionFixture,
  tinyPng,
} from "./shot-production-helpers";
import { openWorkspacePanels } from "./workspace-helpers";

test.afterEach(async ({}, info) => {
  await writeFile(
    info.outputPath("ui03-result.json"),
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

test("单镜头报价和提交只绑定当前镜头，丢失回执后沿用原提交", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  const [first, second] = (await env.state()).shots;
  const submissions: { shotId: string; key: string }[] = [];
  let releaseResponse!: () => void;
  const holdResponse = new Promise<void>((resolve) => {
    releaseResponse = resolve;
  });
  await page.route("**" + env.generationPath, async (route) => {
    const request = route.request();
    if (request.method() !== "POST") return route.continue();
    submissions.push({
      shotId: request.postDataJSON().review_target.shot_id,
      key: request.headers()["idempotency-key"],
    });
    const attempt = submissions.length;
    if (attempt === 2) return route.continue();
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    if (attempt === 1) return route.abort("connectionreset");
    await holdResponse;
    await route.fulfill({ response });
  });

  await page.goto(`/projects/${env.project.id}/episodes/${env.episode.id}`);
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "素材库", exact: true }).click();
  await page.locator(".library-shot").filter({ hasText: "镜头 01" }).click();
  const firstViewer = page.getByRole("dialog", {
    name: "镜头 01",
    exact: true,
  });
  await firstViewer.getByRole("button", { name: "预览数量与费用" }).click();
  const preview = firstViewer.getByRole("region", {
    name: "批量提交预览与结果",
  });
  await expect(preview.locator("[data-batch-shot]")).toHaveAttribute(
    "data-batch-shot",
    first.id,
  );
  await expect(preview).toContainText("0.000000 USD");
  expect(await env.jobs()).toHaveLength(0);
  await preview.getByRole("button", { name: "确认逐项提交" }).click();
  await expect(
    preview.getByRole("button", { name: "继续未完成提交" }),
  ).toBeEnabled();

  await firstViewer.getByRole("button", { name: "下一个镜头" }).click();
  const secondViewer = page.getByRole("dialog", {
    name: "镜头 02",
    exact: true,
  });
  await expect(secondViewer.getByText(/其他镜头的提交待确认/)).toBeVisible();
  await expect(
    secondViewer.getByRole("button", { name: "预览数量与费用" }),
  ).toBeDisabled();
  await expect(
    secondViewer.getByRole("button", { name: "继续未完成提交" }),
  ).toHaveCount(0);

  await secondViewer.getByRole("button", { name: "上一个镜头" }).click();
  await preview.getByRole("button", { name: "继续未完成提交" }).click();
  await expect(preview.getByRole("status")).toContainText("已受理 1 项");
  expect(submissions).toHaveLength(2);
  expect(submissions[0]).toEqual({ shotId: first.id, key: expect.any(String) });
  expect(submissions[1]).toEqual(submissions[0]);
  expect(await env.jobs()).toHaveLength(1);
  expect(
    (await env.state()).shots.find((shot) => shot.id === second.id)?.job,
  ).toBeNull();

  await firstViewer.getByRole("button", { name: "下一个镜头" }).click();
  await secondViewer.getByRole("button", { name: "预览数量与费用" }).click();
  await secondViewer.getByRole("button", { name: "确认逐项提交" }).click();
  await expect.poll(() => submissions.length).toBe(3);
  await secondViewer.getByRole("button", { name: "下一个镜头" }).click();
  const thirdViewer = page.getByRole("dialog", {
    name: "镜头 03",
    exact: true,
  });
  await expect(thirdViewer.getByText(/其他镜头的提交待确认/)).toBeVisible();
  releaseResponse();
  await expect
    .poll(() =>
      page.evaluate(() => {
        const key = Object.keys(sessionStorage).find((id) =>
          id.startsWith("arclight:shot-batch:v1:"),
        );
        return key
          ? JSON.parse(sessionStorage.getItem(key)!).items[0].phase
          : null;
      }),
    )
    .toBe("accepted");
  await thirdViewer.getByRole("button", { name: "刷新生产状态" }).click();
  await expect(thirdViewer.getByText(/其他镜头的提交待确认/)).toHaveCount(0);
  await expect(
    thirdViewer.getByRole("button", { name: "预览数量与费用" }),
  ).toBeEnabled();
  expect(submissions.map((row) => row.shotId)).toEqual([
    first.id,
    first.id,
    second.id,
  ]);
});

test("UI-03：关闭工作区后停止后续项，重新打开只继续原批次未完成项", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  let entered!: () => void, release!: () => void, finished!: () => void;
  const enteredPromise = new Promise<void>((resolve) => {
    entered = resolve;
  });
  const releasePromise = new Promise<void>((resolve) => {
    release = resolve;
  });
  const finishedPromise = new Promise<void>((resolve) => {
    finished = resolve;
  });
  let submissions = 0;
  await page.route("**" + env.generationPath, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    submissions++;
    if (submissions === 1) {
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      entered();
      await releasePromise;
      await route.fulfill({ response });
      finished();
      return;
    }
    await route.continue();
  });
  let dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择生产镜头 1", exact: true })
    .check();
  await dialog
    .getByRole("checkbox", { name: "选择生产镜头 2", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await enteredPromise;
  await dialog
    .getByRole("button", { name: "关闭批量生产", exact: true })
    .click();
  release();
  await finishedPromise;
  await expect
    .poll(async () =>
      page.evaluate(() => {
        const key = Object.keys(sessionStorage).find((value) =>
          value.startsWith("arclight:shot-batch:v1:"),
        );
        return key
          ? JSON.parse(sessionStorage.getItem(key)!).items[0].phase
          : "missing";
      }),
    )
    .toBe("accepted");
  expect(submissions).toBe(1);
  expect(await env.jobs()).toHaveLength(1);
  dialog = await env.open(false);
  await dialog
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect(
    dialog
      .getByRole("region", { name: "批量提交预览与结果" })
      .getByRole("status"),
  ).toContainText("已受理 2 项");
  expect(submissions).toBe(2);
  expect(await env.jobs()).toHaveLength(2);
});

test("UI-03：真实列表多选、提交前报价、逐项生成与成功镜头保留", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const env = await shotProductionFixture(page);
  const original = await env.state();
  const dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择生产镜头 1", exact: true })
    .check();
  await dialog
    .getByRole("checkbox", { name: "选择生产镜头 3", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await expect(preview).toContainText("生成新镜头 · 2 个镜头");
  await expect(preview).toContainText("0.000000 USD");
  expect(await env.jobs()).toHaveLength(0);
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("ui03-desktop-preview.png"),
    fullPage: true,
  });
  await preview
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText("已受理 2 项");
  const jobs = await env.jobs();
  expect(jobs).toHaveLength(2);
  const current = await env.state();
  expect(current.shots[0].job?.id).not.toBe(current.shots[2].job?.id);
  expect(current.shots[1].job).toBeNull();
  expect(current.shots.map((row) => row.id)).toEqual(
    original.shots.map((row) => row.id),
  );
  await dialog
    .getByRole("checkbox", { name: "选择本页全部生产镜头", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  await expect(preview).toContainText("本次可提交 1 项 · 未提交 2 项");
  await preview
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText("已受理 1 项");
  expect(await env.jobs()).toHaveLength(3);
});

test("UI-03：部分提交失败只补失败项，沿用原幂等键且不重提已成功镜头", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  const states = await env.state();
  const calls: { shot: string; key: string }[] = [];
  let rejected = false;
  await page.route("**" + env.generationPath, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const shot = route.request().postDataJSON().review_target.shot_id;
    calls.push({ shot, key: route.request().headers()["idempotency-key"] });
    if (shot === states.shots[1].id && !rejected) {
      rejected = true;
      return apiFailure(
        route,
        "ACCOUNT_BALANCE_INSUFFICIENT",
        "账户余额不足",
        402,
      );
    }
    return route.continue();
  });
  const dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择本页全部生产镜头", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await preview
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText(
    "已受理 2 项 · 提交失败 1 项",
  );
  const before = await env.jobs();
  expect(before).toHaveLength(2);
  await preview
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText(
    "已受理 3 项 · 提交失败 0 项",
  );
  expect(calls.filter((row) => row.shot === states.shots[0].id)).toHaveLength(
    1,
  );
  expect(calls.filter((row) => row.shot === states.shots[2].id)).toHaveLength(
    1,
  );
  const retries = calls.filter((row) => row.shot === states.shots[1].id);
  expect(retries).toHaveLength(2);
  expect(retries[0].key).toBe(retries[1].key);
  expect((await env.jobs()).map((row) => row.id)).toEqual(
    expect.arrayContaining(before.map((row) => row.id)),
  );
  expect(await env.jobs()).toHaveLength(3);
});

test("UI-03：服务端已接受但响应丢失，刷新后重放原请求不重复创建", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  const keys: string[] = [];
  let lost = false;
  let lossInjected = false;
  await page.route("**" + env.generationPath, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().headers()["idempotency-key"]);
    if (!lost) {
      lost = true;
      const response = await route.fetch();
      expect(response.status()).toBe(201);
      await route.abort("connectionreset");
      lossInjected = true;
      return;
    }
    await route.continue();
  });
  let dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择生产镜头 1", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(
    dialog
      .getByRole("region", { name: "批量提交预览与结果" })
      .getByRole("status"),
  ).toContainText("待确认 1 项");
  await expect.poll(() => lossInjected).toBe(true);
  await expect(
    dialog.getByRole("button", { name: "继续未完成提交", exact: true }),
  ).toBeEnabled();
  const first = await env.jobs();
  expect(first).toHaveLength(1);
  dialog = await env.open();
  await dialog
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect(
    dialog
      .getByRole("region", { name: "批量提交预览与结果" })
      .getByRole("status"),
  ).toContainText("已受理 1 项");
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(await env.jobs()).toEqual(first);
});

test("UI-03：失败重试保留成功项，未知任务不重提，丢失的重试响应先读原状态", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  const model = await env.state();
  model.shots.forEach((shot, index) => {
    shot.job = {
      id: crypto.randomUUID(),
      status: ["SUCCEEDED", "FAILED", "RESULT_UNKNOWN"][index],
      revision: 4,
      attempt_count: 1,
      maximum_cost: "0.010000",
      currency: "USD",
    };
  });
  let status = "FAILED",
    revision = 4,
    retries = 0;
  const failed = model.shots[1].job!;
  await page.route("**" + env.productionPath + "?**", (route) =>
    route.fulfill({ json: model }),
  );
  await page.route("**" + env.generationPath + "/" + failed.id, (route) =>
    route.fulfill({ json: { ...failed, status, revision } }),
  );
  await page.route(
    "**" + env.generationPath + "/" + failed.id + "/retry",
    async (route) => {
      retries++;
      expect(route.request().postDataJSON()).toEqual({ expected_revision: 4 });
      if (retries === 1)
        return route.fulfill({ json: { ...failed, status, revision } });
      status = "RETRY_WAIT";
      revision = 5;
      await route.abort("connectionreset");
    },
  );
  const dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择本页全部生产镜头", exact: true })
    .check();
  await dialog
    .getByRole("combobox", { name: "生产操作", exact: true })
    .selectOption("retry");
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await expect(preview).toContainText("本次可提交 1 项 · 未提交 2 项");
  await expect(preview).toContainText("0.010000 USD");
  await expect(preview).toContainText("已生成成功，保留原结果");
  await preview
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText(
    "已受理 0 项 · 提交失败 1 项 · 待确认 0 项",
  );
  await expect(preview).toContainText("原任务未进入重试");
  await preview
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText("待确认 1 项");
  await preview
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText("已受理 1 项");
  expect(retries).toBe(2);
  expect(await env.jobs()).toHaveLength(0);
});

test("UI-03：精确候选多选批准、必填意见及逐项CAS冲突", async ({ page }) => {
  const env = await shotProductionFixture(page);
  const model = await env.state();
  const versions = model.shots.map(() => ({
    status: "READY_FOR_REVIEW",
    revision: 2,
  }));
  model.shots.forEach((shot, index) => {
    const assetId = crypto.randomUUID();
    const versionId = crypto.randomUUID();
    shot.candidates = [
      {
        id: crypto.randomUUID(),
        asset_id: assetId,
        version_id: versionId,
        version_no: index + 1,
        revision: 2,
        status: "READY_FOR_REVIEW",
        image_path: `/projects/${env.project.id}/assets/${assetId}/versions/${versionId}/image`,
        can_submit: false,
        can_approve: true,
        can_reject: true,
        blocker: null,
      },
    ];
  });
  await page.route("**" + env.productionPath + "?**", (route) =>
    route.fulfill({ json: model }),
  );
  const patches: { index: number; body: unknown }[] = [];
  for (const [index, shot] of model.shots.entries()) {
    const candidate = shot.candidates[0];
    const path = `/api/v1/projects/${env.project.id}/assets/${candidate.asset_id}/versions/${candidate.version_id}`;
    await page.route("**" + path + "/image", (route) =>
      route.fulfill({ contentType: "image/png", body: tinyPng }),
    );
    await page.route("**" + path, (route) =>
      route.fulfill({ json: versions[index] }),
    );
    await page.route("**" + path + "/state", async (route) => {
      patches.push({ index, body: route.request().postDataJSON() });
      if (index === 1)
        return apiFailure(route, "VERSION_CONFLICT", "候选状态已变化，请刷新");
      versions[index] = { status: "APPROVED", revision: 3 };
      await route.fulfill({ json: versions[index] });
    });
  }
  const dialog = await env.open();
  for (const [index, shot] of model.shots.entries()) {
    await dialog
      .getByRole("combobox", {
        name: `镜头 ${index + 1} 审核候选`,
        exact: true,
      })
      .selectOption(shot.candidates[0].id);
    await expect(
      dialog.getByRole("img", {
        name: `镜头 ${index + 1} 候选 1 · v${shot.candidates[0].version_no}`,
        exact: true,
      }),
    ).toBeVisible();
  }
  await dialog
    .getByRole("checkbox", { name: "选择本页全部生产镜头", exact: true })
    .check();
  await dialog
    .getByRole("combobox", { name: "生产操作", exact: true })
    .selectOption("approve");
  await expect(
    dialog.getByRole("button", { name: "预览数量与费用", exact: true }),
  ).toBeDisabled();
  await dialog
    .getByRole("textbox", { name: "批量审核意见（必填）", exact: true })
    .fill("已逐图查看，接受各镜头的精确候选。");
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await preview
    .getByRole("button", { name: "确认逐项提交", exact: true })
    .click();
  await expect(preview.getByRole("status")).toContainText(
    "已受理 2 项 · 提交失败 1 项",
  );
  expect(patches).toEqual(
    [0, 1, 2].map((index) => ({
      index,
      body: {
        expected_revision: 2,
        status: "APPROVED",
        comment: "已逐图查看，接受各镜头的精确候选。",
      },
    })),
  );
  await preview
    .getByRole("button", { name: "继续未完成提交", exact: true })
    .click();
  await expect.poll(() => patches.length).toBe(4);
  expect(patches.map((row) => row.index)).toEqual([0, 1, 2, 1]);
});

test("UI-03：无报价的镜头仍可提交且已知报价保留精度", async ({ page }) => {
  const env = await shotProductionFixture(page);
  const model = await env.state();
  await page.route(
    `**/api/v1/projects/${env.project.id}/generation-costs/estimate`,
    async (route) => {
      const shotId = route.request().postDataJSON().review_target.shot_id;
      const index = model.shots.findIndex((shot) => shot.id === shotId);
      if (index === 2)
        return route.fulfill({
          json: {
            estimate: null,
          },
        });
      await route.fulfill({
        json: {
          estimate: {
            maximum_cost: index === 0 ? "0.100001" : "0.200002",
            currency: "USD",
          },
        },
      });
    },
  );
  const dialog = await env.open();
  await dialog
    .getByRole("checkbox", { name: "选择本页全部生产镜头", exact: true })
    .check();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await expect(preview).toContainText("预计费用：部分项目暂无报价");
  await expect(preview).toContainText("0.100001 USD");
  await expect(preview).toContainText("0.200002 USD");
  await expect(preview).toContainText("本次可提交 3 项 · 未提交 0 项");
  await expect(
    preview.locator(`[data-batch-shot="${model.shots[2].id}"]`),
  ).toContainText("无可用报价");
  expect(await env.jobs()).toHaveLength(0);
});

test("UI-03：撤权后清空旧镜头、候选和批次，Viewer仍可只读打开", async ({
  page,
}) => {
  const env = await shotProductionFixture(page);
  const model = await env.state();
  let denied = false;
  model.writable = false;
  model.owner = false;
  await page.route("**" + env.productionPath + "?**", (route) =>
    denied
      ? apiFailure(route, "FORBIDDEN", "当前项目角色没有此操作权限", 403)
      : route.fulfill({ json: model }),
  );
  const dialog = await env.open();
  await expect(dialog).toContainText("当前项目角色或归档状态只允许查看");
  await expect(
    dialog.getByRole("button", { name: "预览数量与费用", exact: true }),
  ).toBeDisabled();
  denied = true;
  await dialog
    .getByRole("button", { name: "刷新生产状态", exact: true })
    .click();
  await expect(dialog.getByRole("table", { name: "镜头生产列表" })).toHaveCount(
    0,
  );
  await expect(
    dialog.getByRole("region", { name: "批量提交预览与结果" }),
  ).toHaveCount(0);
  await expect(dialog).not.toContainText(model.shots[0].id);
});

test("UI-03：手机列表键盘多选与预算预览，横向滚动局限于列表", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await shotProductionFixture(page);
  const dialog = await env.open();
  const check = dialog.getByRole("checkbox", {
    name: "选择生产镜头 2",
    exact: true,
  });
  await check.focus();
  await page.keyboard.press("Space");
  await expect(check).toBeChecked();
  await dialog
    .getByRole("button", { name: "预览数量与费用", exact: true })
    .click();
  const preview = dialog.getByRole("region", { name: "批量提交预览与结果" });
  await expect(preview).toContainText("生成新镜头 · 1 个镜头");
  await expect(
    preview.getByRole("button", { name: "确认逐项提交", exact: true }),
  ).toBeEnabled();
  const list = dialog.getByRole("region", { name: "镜头生产列表滚动区域" });
  expect(
    await list.evaluate((element) => element.scrollWidth > element.clientWidth),
  ).toBe(true);
  expect(
    await dialog.evaluate(
      (element) => element.scrollWidth <= element.clientWidth + 1,
    ),
  ).toBe(true);
  await preview.scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("ui03-mobile-preview.png"),
    fullPage: true,
  });
  expect(await env.jobs()).toHaveLength(0);
});
