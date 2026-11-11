import { createHash } from "node:crypto";
import { readFile } from "node:fs/promises";
import { expect, test } from "@playwright/test";
import {
  defaultTimelineFixture,
  deferred,
  renderFixture,
} from "./render-helpers";
import { openDirectory } from "./workspace-helpers";

test("RENDER-12：预览清单绑定精确时间轴，重复点击只提交一个幂等任务", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const panel = await env.open();
  await expect(
    page.getByRole("radio", { name: "渲染与成片", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await expect(panel.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    env.timelines[0].id,
  );
  await panel
    .getByRole("button", { name: "创建预览清单", exact: true })
    .click();
  await expect
    .poll(
      () =>
        env.mutations.filter((row) => row.path.endsWith("/render-manifests"))
          .length,
    )
    .toBe(1);
  const created = env.mutations.find((row) =>
    row.path.endsWith("/render-manifests"),
  )!;
  expect(created.path).toBe(
    `/api/v1/timelines/${env.timelines[0].id}/render-manifests`,
  );
  expect(created.body).toMatchObject({ profile: "PREVIEW" });
  expect(created.body.creation_key).toMatch(
    /^[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}$/,
  );
  const frozen = env.manifests[0];
  await expect(panel.getByLabel("渲染清单", { exact: true })).toHaveValue(
    frozen.id,
  );
  const gate = deferred();
  env.control.jobGate = gate.promise;
  const submit = panel.getByRole("button", { name: "提交渲染", exact: true });
  await submit.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  try {
    await expect
      .poll(
        () => env.mutations.filter((row) => row.path.endsWith("/jobs")).length,
      )
      .toBe(1);
    await expect(submit).toBeDisabled();
  } finally {
    gate.release();
  }
  await expect.poll(() => env.jobs.length).toBe(1);
  await expect(panel.getByLabel("当前渲染任务", { exact: true })).toContainText(
    env.jobs[0].id,
  );
  expect(
    env.mutations.filter((row) => row.path.endsWith("/jobs"))[0].path,
  ).toBe(`/api/v1/render-manifests/${frozen.id}/jobs`);
});

test("RENDER-12：存储受限时提交结果未知仍同键重试，刷新恢复持久任务", async ({
  page,
}) => {
  const env = await renderFixture(page);
  await page.addInitScript(() => {
    const get = Storage.prototype.getItem;
    const set = Storage.prototype.setItem;
    Storage.prototype.getItem = function (key) {
      return key.startsWith("arclight:render-creation:")
        ? null
        : get.call(this, key);
    };
    Storage.prototype.setItem = function (key, value) {
      if (key.startsWith("arclight:render-creation:"))
        throw new DOMException("Storage is full", "QuotaExceededError");
      return set.call(this, key, value);
    };
  });
  env.control.jobNetworkFailure = true;
  let panel = await env.open();
  await panel.getByRole("button", { name: "提交渲染", exact: true }).click();
  await expect(panel.getByRole("alert")).toContainText("网络");
  await panel.getByRole("button", { name: "提交渲染", exact: true }).click();
  await expect
    .poll(
      () => env.mutations.filter((row) => row.path.endsWith("/jobs")).length,
    )
    .toBe(2);
  const submitted = env.mutations.filter((row) => row.path.endsWith("/jobs"));
  expect(submitted).toHaveLength(2);
  expect(submitted[1].body).toEqual(submitted[0].body);
  expect(env.jobs).toHaveLength(1);
  await expect(panel.getByLabel("当前渲染任务", { exact: true })).toContainText(
    env.jobs[0].id,
  );
  await page.reload();
  panel = page.getByRole("region", { name: "渲染与成片", exact: true });
  await expect(panel.getByLabel("当前渲染任务", { exact: true })).toContainText(
    env.jobs[0].id,
  );
  await expect(page).toHaveURL(env.href());
});

test("RENDER-12：正式出口需精确 Owner 决定与独立恢复，再提交同一清单", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const panel = await env.open();
  await panel.getByLabel("输出用途", { exact: true }).selectOption("FORMAL");
  await panel
    .getByRole("button", { name: "创建正式清单", exact: true })
    .click();
  await expect
    .poll(
      () =>
        env.mutations.filter((row) => row.path.endsWith("/render-manifests"))
          .length,
    )
    .toBe(1);
  const frozen = env.manifests[0];
  expect(frozen.snapshot.output.name).toBe("FORMAL");
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeDisabled();
  await panel
    .getByRole("button", { name: "检查正式导出条件", exact: true })
    .click();
  await expect(
    panel.getByRole("alert").filter({ hasText: "尚未获拥有者批准" }).first(),
  ).toBeVisible();
  expect(
    env.mutations.filter((row) => row.path.endsWith("/jobs")),
  ).toHaveLength(0);
  await panel
    .getByRole("button", { name: "申请正式导出审核", exact: true })
    .click();
  await expect.poll(() => env.reviews.length).toBe(1);
  const review = env.reviews[0];
  const decide = panel.getByRole("button", {
    name: "确认接受本次导出",
    exact: true,
  });
  await expect(decide).toBeDisabled();
  await panel
    .getByLabel("人工处理意见", { exact: true })
    .fill("已核对本次精确清单及输入版本。");
  await expect(decide).toBeDisabled();
  await panel
    .getByRole("checkbox", { name: "我已核对本次精确清单", exact: true })
    .check();
  await decide.click();
  await expect.poll(() => review.status).toBe("DECIDED");
  const mutation = env.mutations.find((row) =>
    row.path.endsWith("/decisions"),
  )!;
  expect(mutation.body).toMatchObject({
    version_id: frozen.id,
    request_sha256: review.request_sha256,
    decision: "APPROVE",
    comment: "已核对本次精确清单及输入版本。",
  });
  expect(
    env.mutations.filter((row) => row.path.endsWith("/resume")),
  ).toHaveLength(0);
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeDisabled();
  await panel
    .getByRole("button", { name: "恢复本次导出", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeEnabled();
  expect(
    env.mutations.find((row) => row.path.endsWith("/resume"))?.body,
  ).toEqual({
    decision_id: review.decision!.id,
    request_sha256: review.request_sha256,
  });
  await panel.getByRole("button", { name: "提交渲染", exact: true }).click();
  await expect.poll(() => env.jobs.length).toBe(1);
  expect(
    env.mutations.filter((row) => row.path.endsWith("/jobs")),
  ).toHaveLength(1);
  expect(env.jobs[0].manifest_id).toBe(frozen.id);
  expect(env.jobs[0].profile).toBe("FORMAL");
});

test("RENDER-12：真实任务进度轮询，取消使用最新 revision", async ({ page }) => {
  const env = await renderFixture(page);
  const job = env.job(env.manifests[0]);
  Object.assign(job, {
    status: "RUNNING",
    revision: 7,
    progress: 37,
    phase: "ENCODING",
    attempt_count: 1,
  });
  env.jobs.push(job);
  const panel = await env.open();
  const detail = panel.getByLabel("当前渲染任务", { exact: true });
  await expect(detail).toContainText("37");
  const gate = deferred();
  const completed = deferred();
  const stale = structuredClone([job]);
  let refreshStarted = false;
  await page.route(
    `**/api/v1/render-manifests/${job.manifest_id}/jobs?*`,
    async (route) => {
      refreshStarted = true;
      await gate.promise;
      await route.fulfill({ json: stale }).catch(() => {});
      completed.release();
    },
  );
  await panel
    .getByRole("button", { name: "刷新渲染任务", exact: true })
    .click();
  await expect.poll(() => refreshStarted).toBe(true);
  Object.assign(job, { revision: 8, progress: 62, phase: "INSPECTING" });
  try {
    await expect(detail).toContainText("62", { timeout: 10000 });
  } finally {
    gate.release();
  }
  await completed.promise;
  await expect(detail).toContainText("62");
  await panel.getByRole("button", { name: "取消渲染", exact: true }).click();
  await expect(detail).toContainText("取消");
  const cancelled = env.mutations.find((row) => row.path.endsWith("/cancel"))!;
  expect(cancelled.body).toEqual({ expected_revision: 8 });
  expect(job.status).toBe("CANCELLED");
});

test("RENDER-12：失败保留技术错误和清理状态，重渲染关联原任务", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const job = env.job(env.manifests[0]);
  Object.assign(job, {
    status: "FAILED",
    revision: 5,
    progress: 83,
    phase: "INSPECTING",
    error_code: "RENDER_OUTPUT_DURATION",
    cleanup_status: "FAILED",
    finished_at: "2026-11-11T07:01:00Z",
  });
  env.jobs.push(job);
  const panel = await env.open();
  const detail = panel.getByLabel("当前渲染任务", { exact: true });
  await expect(detail).toContainText("RENDER_OUTPUT_DURATION");
  await expect(detail).toContainText("清理");
  await panel.getByRole("button", { name: "重新渲染", exact: true }).click();
  await expect.poll(() => env.jobs.length).toBe(2);
  const submitted = env.mutations.find((row) => row.path.endsWith("/jobs"))!;
  expect(submitted.body).toMatchObject({ retry_of: job.id });
  expect(env.jobs[0].retry_of_id).toBe(job.id);
  expect(env.jobs[0].id).not.toBe(job.id);
});

test("RENDER-12：机器报告呈现检查与完整精确输入来源", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const env = await renderFixture(page);
  const panel = await env.open();
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  const report = panel.getByRole("region", {
    name: "机器验收报告",
    exact: true,
  });
  await expect(report).toBeVisible();
  await report.getByText("精确输入与成片追溯", { exact: true }).click();
  await report.getByText("素材、字体与渲染软件来源", { exact: true }).click();
  await expect(report).toContainText(env.source.asset_version_id);
  await expect(report).toContainText(env.source.sha256);
  await expect(report).toContainText(env.deliveries[0].manifest_sha256);
  await expect(report).toContainText(env.deliveries[0].sha256);
  await expect(report).toContainText("解码");
  await expect(report).toContainText("字幕");
  await page.screenshot({
    path: info.outputPath("render12-report-desktop.png"),
    fullPage: true,
  });
});

test("RENDER-12：警告逐条展示，Owner 明确接受后仍须独立恢复", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await renderFixture(page);
  env.control.warnings = true;
  const initialReview = env.review(
    "DELIVERY",
    env.deliveries[0].asset_version_id,
  );
  env.reviews.push(initialReview);
  const oldReview = structuredClone(initialReview);
  const reviewGate = deferred();
  let initialReviewReads = 0;
  let settledReviewReads = 0;
  await page.route(
    `**/api/v1/human-reviews/${initialReview.id}`,
    async (route) => {
      if (env.mutations.some((row) => row.path.endsWith("/warning-review")))
        return route.fallback();
      initialReviewReads++;
      await reviewGate.promise;
      await route.fulfill({ json: oldReview }).catch(() => {});
      settledReviewReads++;
    },
  );
  Object.assign(env.deliveries[0], {
    is_delivery: false,
    acceptance: {
      ...env.deliveries[0].acceptance,
      phase: "DELIVERY_QA",
      is_delivery: false,
      review_id: initialReview.id,
    },
  });
  try {
    const panel = await env.open();
    await expect.poll(() => initialReviewReads).toBeGreaterThan(0);
    await panel.getByRole("button", { name: "查看报告", exact: true }).click();
    const report = panel.getByRole("region", {
      name: "机器验收报告",
      exact: true,
    });
    for (const warning of env.warnings) {
      await expect(report).toContainText(warning.reason);
      await expect(report).toContainText(warning.line_id!);
    }
    await panel
      .getByRole("button", { name: "申请交付警告确认", exact: true })
      .click();
    const approve = panel.getByRole("button", {
      name: "确认接受全部警告",
      exact: true,
    });
    await expect(approve).toBeDisabled();
    await panel
      .getByLabel("人工处理意见", { exact: true })
      .fill("已核对两句台词，接受本版字幕及静默决定。");
    await expect(approve).toBeDisabled();
    await panel
      .getByRole("checkbox", { name: "我已核对并接受全部警告", exact: true })
      .check();
    await approve.click();
    const review = env.reviews[0];
    await expect.poll(() => review.status).toBe("DECIDED");
    expect(
      env.mutations.find((row) => row.path.endsWith("/decisions"))?.body,
    ).toMatchObject({
      version_id: review.snapshot.version_id,
      request_sha256: review.request_sha256,
      decision: "APPROVE",
      comment: "已核对两句台词，接受本版字幕及静默决定。",
    });
    expect(
      env.mutations.filter((row) => row.path.endsWith("/resume")),
    ).toHaveLength(0);
    expect(env.deliveries[0].is_delivery).toBe(false);
    await panel
      .getByRole("button", { name: "恢复交付确认", exact: true })
      .click();
    await expect
      .poll(() => env.deliveries[0].acceptance.phase)
      .toBe("COMPLETED_WITH_WARNINGS");
    await expect(
      panel.getByLabel("当前成片版本", { exact: true }),
    ).toContainText("警告");
    reviewGate.release();
    await expect.poll(() => settledReviewReads).toBe(initialReviewReads);
    await expect(
      panel.getByRole("button", { name: "确认接受全部警告", exact: true }),
    ).toHaveCount(0);
    await expect(
      panel.getByRole("button", { name: "恢复交付确认", exact: true }),
    ).toHaveCount(0);
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth + 1,
      ),
    ).toBe(true);
    await page.screenshot({
      path: info.outputPath("render12-warning-mobile.png"),
      fullPage: true,
    });
  } finally {
    reviewGate.release();
  }
});

test("RENDER-12：授权 MP4 预览可解码，下载重新带鉴权读取精确版本", async ({
  page,
}, info) => {
  const env = await renderFixture(page);
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取成片预览", exact: true })
    .click();
  const video = panel.locator("video");
  await expect(video).toHaveAttribute("src", /^blob:/);
  await expect
    .poll(() =>
      video.evaluate((element) => (element as HTMLVideoElement).readyState),
    )
    .toBeGreaterThanOrEqual(1);
  const measurement = await video.evaluate((element) => ({
    width: (element as HTMLVideoElement).videoWidth,
    height: (element as HTMLVideoElement).videoHeight,
    duration: (element as HTMLVideoElement).duration,
  }));
  expect(measurement.width).toBe(64);
  expect(measurement.height).toBe(64);
  expect(measurement.duration).toBeGreaterThan(0);
  const downloadPending = page.waitForEvent("download");
  await panel.getByRole("button", { name: "下载 MP4", exact: true }).click();
  const downloaded = await downloadPending;
  expect(downloaded.suggestedFilename()).toMatch(/\.mp4$/);
  const path = info.outputPath("render12-authorized-version.mp4");
  await downloaded.saveAs(path);
  expect(
    createHash("sha256")
      .update(await readFile(path))
      .digest("hex"),
  ).toBe(env.deliveries[0].sha256);
  expect(env.contentReads).toHaveLength(2);
  for (const transfer of env.contentReads) {
    expect(transfer.path).toBe(
      `/api/v1/episodes/${env.episodes[0].id}/deliveries/${env.deliveries[0].id}/content`,
    );
    expect(transfer.authorization).toMatch(/^Bearer .+/);
  }
  const oldUrl = await video.getAttribute("src");
  await panel
    .getByRole("button", { name: "查看成片版本 2", exact: true })
    .click();
  await expect(panel.locator(`video[src="${oldUrl}"]`)).toHaveCount(0);
});

test("RENDER-12：预览与旧手工回执不能冒充正式交付", async ({ page }) => {
  const env = await renderFixture(page);
  const panel = await env.open();
  for (const version of [2, 1]) {
    await panel
      .getByRole("button", { name: `查看成片版本 ${version}`, exact: true })
      .click();
    const detail = panel.getByLabel("当前成片版本", { exact: true });
    await expect(
      panel.getByRole("button", {
        name: `查看成片版本 ${version}`,
        exact: true,
      }),
    ).toContainText(version === 2 ? "预览" : "历史");
    await expect(detail).not.toContainText("正式交付已完成");
    await expect(
      detail.getByRole("button", { name: "查看报告", exact: true }),
    ).toBeDisabled();
    await expect(
      panel.getByRole("button", { name: "申请交付警告确认", exact: true }),
    ).toHaveCount(0);
  }
  expect(env.mutations).toHaveLength(0);
});

test("RENDER-12：Viewer 可查报告与下载，没有生产或人工确认写入入口", async ({
  page,
}) => {
  const env = await renderFixture(page);
  env.control.role = "viewer";
  const panel = await env.open();
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "创建预览清单", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "申请正式导出审核", exact: true }),
  ).toHaveCount(0);
  await panel
    .getByLabel("渲染清单", { exact: true })
    .selectOption(
      env.manifests.find(
        (row) =>
          row.snapshot.timeline_id === env.timelines[0].id &&
          row.snapshot.output.name === "FORMAL",
      )!.id,
    );
  await expect(
    panel.getByRole("button", { name: "检查正式导出条件", exact: true }),
  ).toHaveCount(0);
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "下载 MP4", exact: true }),
  ).toBeEnabled();
  expect(env.mutations).toHaveLength(0);
});

test("RENDER-12：归档剧集保持成片只读，禁止创建与提交", async ({ page }) => {
  const env = await renderFixture(page);
  env.control.archived = true;
  const panel = await env.open();
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "创建预览清单", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "下载 MP4", exact: true }),
  ).toBeEnabled();
  expect(env.mutations).toHaveLength(0);
});

test("RENDER-12：撤权后的刷新清除旧成片和报告，并显示 request ID", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const panel = await env.open();
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toBeVisible();
  env.control.readStatus = 403;
  await panel
    .getByRole("button", { name: "刷新渲染记录", exact: true })
    .click();
  await expect(
    panel.getByRole("alert").filter({ hasText: "当前项目权限已变化" }).first(),
  ).toContainText("render-browser-request");
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "下载 MP4", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "读取成片预览", exact: true }),
  ).toHaveCount(0);
});

test("RENDER-12：快速切换成片，迟到的报告不能覆盖当前精确版本", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const gate = deferred();
  const reportSettled = deferred();
  let intercepted = false;
  env.control.reportGate = async (route, row) => {
    intercepted = true;
    const snapshot = structuredClone(env.report(row));
    await gate.promise;
    await route.fulfill({ json: snapshot }).catch(() => {});
    reportSettled.release();
  };
  const panel = await env.open();
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  await expect.poll(() => intercepted).toBe(true);
  await panel
    .getByRole("button", { name: "查看成片版本 2", exact: true })
    .click();
  gate.release();
  await reportSettled.promise;
  const detail = panel.getByLabel("当前成片版本", { exact: true });
  await expect(detail).toContainText("预览");
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toHaveCount(0);
  // Identical bytes can have the same hash across immutable delivery versions.
  expect(env.deliveries[1].sha256).toBe(env.deliveries[0].sha256);
  await expect(detail).toContainText(env.deliveries[1].asset_version_id);
  await expect(detail).toContainText(env.deliveries[1].manifest_id);
  await expect(detail).not.toContainText(env.deliveries[0].asset_version_id);
  await expect(detail).not.toContainText(env.deliveries[0].manifest_id);
});

test("RENDER-12：切集后丢弃旧下载回执，不触发其他剧集的下载", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const gate = deferred();
  let requested = false;
  const downloads: string[] = [];
  page.on("download", (item) => downloads.push(item.suggestedFilename()));
  env.control.contentGate = async (route) => {
    requested = true;
    await gate.promise;
    await route
      .fulfill({ contentType: "video/mp4", body: Buffer.from("old content") })
      .catch(() => {});
  };
  const panel = await env.open();
  await panel.getByRole("button", { name: "下载 MP4", exact: true }).click();
  await expect.poll(() => requested).toBe(true);
  await openDirectory(page);
  await page.getByRole("button", { name: /第二集的独立成片/ }).click();
  await expect(page).toHaveURL(env.href(1).replace("?view=render", ""));
  await page.getByRole("radio", { name: "渲染与成片", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "渲染与成片", exact: true }),
  ).toBeVisible();
  gate.release();
  await expect(page.getByLabel("当前成片版本", { exact: true })).toContainText(
    env.deliveries[3].sha256,
  );
  expect(downloads).toHaveLength(0);
  await expect(page.locator("video")).toHaveCount(0);
});

test("RENDER-12：成片分页使用最后一条精确游标，不覆盖已加载历史", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const first = env.deliveries[0];
  env.deliveries.splice(
    0,
    env.deliveries.length - 1,
    ...Array.from({ length: 31 }, (_, index) => ({
      ...structuredClone(first),
      id: crypto.randomUUID(),
      version_no: 31 - index,
    })),
  );
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "加载更多成片", exact: true })
    .click();
  await expect(
    panel.getByRole("button", { name: "查看成片版本 1", exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", { name: "查看成片版本 31", exact: true }),
  ).toBeVisible();
  expect(
    env.reads.some((value) => {
      const url = new URL(value);
      return (
        url.pathname.endsWith("/deliveries") &&
        url.searchParams.get("before_id") === env.deliveries[29].id
      );
    }),
  ).toBe(true);
});

test("RENDER-12：清单与任务分别有界分页并保留精确选择", async ({ page }) => {
  const env = await renderFixture(page);
  const base = env.manifests[0];
  env.manifests.splice(
    0,
    env.manifests.length,
    ...Array.from({ length: 31 }, () => ({
      ...structuredClone(base),
      id: crypto.randomUUID(),
    })),
  );
  env.jobs.push(
    ...Array.from({ length: 31 }, () => ({
      ...env.job(env.manifests[0]),
      status: "FAILED",
      finished_at: "2026-11-11T07:01:00Z",
    })),
  );
  const panel = await env.open();
  const select = panel.getByLabel("渲染清单", { exact: true });
  await expect(
    select.locator(`option[value="${env.manifests[30].id}"]`),
  ).toHaveCount(0);
  await panel
    .getByRole("button", { name: "加载更多渲染清单", exact: true })
    .click();
  await expect(
    select.locator(`option[value="${env.manifests[30].id}"]`),
  ).toHaveCount(1);
  await expect(select).toHaveValue(env.manifests[0].id);
  await panel
    .getByRole("button", { name: "加载更多渲染任务", exact: true })
    .click();
  await expect(
    panel.getByRole("button", {
      name: `查看渲染任务 ${env.jobs[30].id}`,
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    panel.getByRole("button", {
      name: `查看渲染任务 ${env.jobs[0].id}`,
      exact: true,
    }),
  ).toBeVisible();
  for (const [ending, cursor] of [
    ["/render-manifests", env.manifests[29].id],
    ["/jobs", env.jobs[29].id],
  ]) {
    expect(
      env.reads.some((value) => {
        const url = new URL(value);
        return (
          url.pathname.endsWith(ending) &&
          url.searchParams.get("before_id") === cursor
        );
      }),
    ).toBe(true);
  }
});

test("RENDER-12：Editor 不能作 Owner 正式出口或交付警告决定", async ({
  page,
}) => {
  const env = await renderFixture(page);
  env.control.role = "editor";
  env.control.warnings = true;
  env.deliveries[0].is_delivery = false;
  env.deliveries[0].acceptance.phase = "DELIVERY_QA";
  env.deliveries[0].acceptance.is_delivery = false;
  const panel = await env.open();
  await panel
    .getByLabel("渲染清单", { exact: true })
    .selectOption(
      env.manifests.find(
        (row) =>
          row.snapshot.timeline_id === env.timelines[0].id &&
          row.snapshot.output.name === "FORMAL",
      )!.id,
    );
  await expect(
    panel.getByRole("button", { name: "申请正式导出审核", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "提交渲染", exact: true }),
  ).toBeDisabled();
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toContainText(env.warnings[0].reason);
  await expect(
    panel.getByRole("button", { name: "申请交付警告确认", exact: true }),
  ).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("RENDER-12：S3 预览与下载逐次申请精确授权，传输不携带会话密钥", async ({
  page,
}) => {
  const env = await renderFixture(page);
  env.deliveries[0].storage_backend = "S3";
  const delivery = env.deliveries[0];
  const grants: { body: unknown; authorization: string | undefined }[] = [];
  const transfers: string[] = [];
  await page.route(
    `**/api/v1/projects/${env.project.id}/files/${delivery.file_id}/download-url`,
    async (route) => {
      grants.push({
        body: route.request().postDataJSON(),
        authorization: route.request().headers().authorization,
      });
      await route.fulfill({
        json: {
          file_id: delivery.file_id,
          method: "GET",
          url: `/file-transfers/render12-${grants.length}`,
          expires_at: new Date(Date.now() + 120000).toISOString(),
          checksum: delivery.sha256,
          byte_size: delivery.byte_size,
        },
      });
    },
  );
  await page.route("**/file-transfers/render12-*", async (route) => {
    expect(route.request().headers().authorization).toBeUndefined();
    expect(route.request().headers().cookie).toBeUndefined();
    transfers.push(new URL(route.request().url()).pathname);
    await route.fulfill({ contentType: "video/mp4", body: env.mediaBytes() });
  });
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取成片预览", exact: true })
    .click();
  await expect(panel.locator("video")).toHaveAttribute("src", /^blob:/);
  const pending = page.waitForEvent("download");
  await panel.getByRole("button", { name: "下载 MP4", exact: true }).click();
  await pending;
  expect(grants).toHaveLength(2);
  expect(transfers).toEqual([
    "/file-transfers/render12-1",
    "/file-transfers/render12-2",
  ]);
  for (const grant of grants) {
    expect(grant.authorization).toMatch(/^Bearer .+/);
    expect(grant.body).toEqual({ expires_in: 120 });
  }
  await expect(page).toHaveURL(env.href());
});

test("RENDER-12：成片哈希不符时清除已有预览并阻止下载", async ({ page }) => {
  const env = await renderFixture(page);
  const downloads: string[] = [];
  page.on("download", (item) => downloads.push(item.suggestedFilename()));
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "读取成片预览", exact: true })
    .click();
  await expect(panel.locator("video")).toHaveCount(1);
  const modified = env.mediaBytes();
  modified[modified.length - 1] ^= 1;
  env.control.contentGate = (route) =>
    route.fulfill({ contentType: "video/mp4", body: modified });
  await panel.getByRole("button", { name: "下载 MP4", exact: true }).click();
  await expect(
    panel.getByRole("alert").filter({ hasText: "成片校验未通过" }),
  ).toBeVisible();
  await expect(panel.locator("video")).toHaveCount(0);
  expect(downloads).toHaveLength(0);
});

test("RENDER-12：旧手工报告端点404保留缺证据结论，不伪造机器通过", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const legacy = env.deliveries[2];
  legacy.report_id = crypto.randomUUID();
  legacy.report_file_id = crypto.randomUUID();
  const panel = await env.open();
  await panel
    .getByRole("button", { name: "查看成片版本 1", exact: true })
    .click();
  const reportPath = `/api/v1/episodes/${env.episodes[0].id}/deliveries/${legacy.id}/report`;
  const reportResponse = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname === reportPath &&
      response.request().method() === "GET",
  );
  await panel.getByRole("button", { name: "查看报告", exact: true }).click();
  expect((await reportResponse).status()).toBe(404);
  await expect(
    panel.getByRole("alert").filter({
      hasText: "该成片没有可读取的机器报告。历史成片和授权预览仍可查看。",
    }),
  ).toContainText("render-browser-request");
  const detail = panel.getByLabel("当前成片版本", { exact: true });
  await expect(detail).toContainText(legacy.asset_version_id);
  await expect(detail).not.toContainText("正式交付已完成");
  await expect(
    detail.getByRole("button", { name: "读取成片预览", exact: true }),
  ).toBeEnabled();
  await expect(
    detail.getByRole("button", { name: "下载 MP4", exact: true }),
  ).toBeEnabled();
  await expect(panel.getByLabel("时间轴版本", { exact: true })).toBeEnabled();
  await expect(panel.getByLabel("渲染清单", { exact: true })).toBeEnabled();
  await expect(
    panel.getByRole("button", { name: "查看成片版本 3", exact: true }),
  ).toBeEnabled();
  await expect(
    panel.getByRole("region", { name: "机器验收报告", exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "申请交付警告确认", exact: true }),
  ).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("RENDER-12：默认时间轴显式核对完整分镜，多选用版本由用户确定", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const prepared = await defaultTimelineFixture(page, env);
  const panel = await env.open();
  await panel.getByText("从已批准分镜创建默认时间轴", { exact: true }).click();
  const board = panel.getByLabel("已批准分镜版本", { exact: true });
  await expect(board.locator("option")).toHaveCount(2);
  await board.selectOption(prepared.board.id);
  expect(
    prepared.reads.filter((url) => /\/shots\?|\/continuity\?/.test(url)),
  ).toHaveLength(0);
  await panel
    .getByRole("button", { name: "核对当前选用镜头", exact: true })
    .click();
  const selected = panel.getByLabel("镜头 1 的已选用版本", { exact: true });
  await expect(selected).toHaveValue("");
  const confirm = panel.getByRole("checkbox", {
    name: "我已核对全部镜头及精确素材版本",
    exact: true,
  });
  const create = panel.getByRole("button", {
    name: "创建默认时间轴",
    exact: true,
  });
  await expect(confirm).toBeDisabled();
  await expect(create).toBeDisabled();
  await selected.selectOption(prepared.choices[1].asset_version_id);
  await expect(confirm).toBeEnabled();
  await expect(create).toBeDisabled();
  await confirm.check();
  await create.click();
  await expect
    .poll(
      () =>
        env.mutations.filter((row) => row.path.endsWith("/timelines")).length,
    )
    .toBe(1);
  const mutation = env.mutations.find((row) =>
    row.path.endsWith("/timelines"),
  )!;
  expect(mutation.body).toMatchObject({
    storyboard_id: prepared.board.id,
    default_duration_ms: 3000,
    selected_shots: [
      {
        shot_id: prepared.shots[0].id,
        asset_version_id: prepared.choices[1].asset_version_id,
      },
      {
        shot_id: prepared.shots[1].id,
        asset_version_id: prepared.choices[2].asset_version_id,
      },
    ],
  });
  await expect(panel.getByLabel("时间轴版本", { exact: true })).toHaveValue(
    env.timelines[0].id,
  );
  expect(env.timelines[0].snapshot.duration_ms).toBe(6000);
  expect(
    prepared.reads.some((value) => {
      const url = new URL(value);
      return (
        url.pathname.endsWith("/shots") &&
        url.searchParams.get("storyboard_id") === prepared.board.id &&
        url.searchParams.get("limit") === "100"
      );
    }),
  ).toBe(true);
});

test("RENDER-12：完整分镜缺少当前已选图片时阻断，不代选普通候选", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const prepared = await defaultTimelineFixture(page, env, true);
  const panel = await env.open();
  await panel.getByText("从已批准分镜创建默认时间轴", { exact: true }).click();
  await panel
    .getByLabel("已批准分镜版本", { exact: true })
    .selectOption(prepared.board.id);
  await panel
    .getByRole("button", { name: "核对当前选用镜头", exact: true })
    .click();
  await expect(
    panel
      .getByRole("alert")
      .filter({ hasText: "镜头 2 缺少当前已选用且审查通过的图片" }),
  ).toBeVisible();
  await panel
    .getByLabel("镜头 1 的已选用版本", { exact: true })
    .selectOption(prepared.choices[0].asset_version_id);
  await expect(
    panel.getByRole("checkbox", {
      name: "我已核对全部镜头及精确素材版本",
      exact: true,
    }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "创建默认时间轴", exact: true }),
  ).toBeDisabled();
  expect(
    prepared.reads.filter((url) =>
      url.includes(`/versions/${prepared.choices[2].asset_version_id}/`),
    ),
  ).toHaveLength(0);
  expect(
    env.mutations.filter((row) => row.path.endsWith("/timelines")),
  ).toHaveLength(0);
});

test("RENDER-12：精确来源台词不得默认静音，每句显式处理及必填原因才可建清单", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const source = env.sourceBoards[0];
  const scriptId = source.script_version_id!;
  const newer = {
    ...source,
    id: crypto.randomUUID(),
    version_no: 2,
    script_version_id: crypto.randomUUID(),
  };
  await page.route(
    `**/api/v1/episodes/${env.episodes[0].id}/storyboards?*`,
    (route) => route.fulfill({ json: [newer, source] }),
  );
  env.dialogueLines.push(
    {
      script_version_id: scriptId,
      line_id: "line-silence",
      text: "这一句保留停顿。",
    },
    {
      script_version_id: scriptId,
      line_id: "line-subtitle",
      text: "另一句有精确字幕。",
    },
  );
  env.timelines[0].snapshot.tracks
    .find((track) => track.kind === "SUBTITLE")!
    .clips.push({
      ...env.timelines[0].snapshot.tracks[0].clips[0],
      id: crypto.randomUUID(),
      script_version_id: scriptId,
      line_id: "line-subtitle",
    });
  const panel = await env.open();
  const firstMode = panel.getByLabel("台词 1 的声音处理", { exact: true });
  const secondMode = panel.getByLabel("台词 2 的声音处理", { exact: true });
  const create = panel.getByRole("button", {
    name: "创建预览清单",
    exact: true,
  });
  await expect(firstMode).toHaveValue("");
  await expect(secondMode).toHaveValue("");
  await expect(create).toBeDisabled();
  await expect(firstMode.locator('option[value="SUBTITLE_ONLY"]')).toHaveCount(
    0,
  );
  await expect(secondMode.locator('option[value="SUBTITLE_ONLY"]')).toHaveCount(
    1,
  );
  await firstMode.selectOption("SILENT");
  await secondMode.selectOption("SUBTITLE_ONLY");
  await expect(create).toBeDisabled();
  await panel.getByLabel("台词 1 的处理原因", { exact: true }).fill("   ");
  await panel
    .getByLabel("台词 2 的处理原因", { exact: true })
    .fill("  仅字幕遵循本版分镜。  ");
  await expect(create).toBeDisabled();
  await panel
    .getByLabel("台词 1 的处理原因", { exact: true })
    .fill("  本版叙事停顿，明确无声。  ");
  await create.click();
  await expect
    .poll(
      () =>
        env.mutations.filter((row) => row.path.endsWith("/render-manifests"))
          .length,
    )
    .toBe(1);
  const body = env.mutations.find((row) =>
    row.path.endsWith("/render-manifests"),
  )!.body;
  expect(body).toMatchObject({
    profile: "PREVIEW",
    line_audio_decisions: [
      {
        script_version_id: scriptId,
        line_id: "line-silence",
        mode: "SILENT",
        reason: "本版叙事停顿，明确无声。",
      },
      {
        script_version_id: scriptId,
        line_id: "line-subtitle",
        mode: "SUBTITLE_ONLY",
        reason: "仅字幕遵循本版分镜。",
      },
    ],
  });
  expect(
    env.reads.some((value) => {
      const url = new URL(value);
      return (
        url.pathname.endsWith("/dialogue-lines") &&
        url.searchParams.get("script_version_id") === scriptId
      );
    }),
  ).toBe(true);
  expect(
    env.reads.some((value) => value.includes(newer.script_version_id)),
  ).toBe(false);
});

test("RENDER-12：初始清单及任务列表迟到不覆盖已成功创建的记录", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const gate = deferred();
  const oldManifests = structuredClone(
    env.manifests.filter(
      (row) => row.snapshot.timeline_id === env.timelines[0].id,
    ),
  );
  let waiting = 0;
  let settled = 0;
  await page.route(
    `**/api/v1/timelines/${env.timelines[0].id}/render-manifests?*`,
    async (route) => {
      waiting++;
      await gate.promise;
      await route.fulfill({ json: oldManifests }).catch(() => {});
      settled++;
    },
  );
  await page.route("**/api/v1/render-manifests/*/jobs?*", async (route) => {
    waiting++;
    await gate.promise;
    await route.fulfill({ json: [] }).catch(() => {});
    settled++;
  });
  try {
    const panel = await env.open();
    await expect.poll(() => waiting).toBeGreaterThan(0);
    await panel
      .getByRole("button", { name: "创建预览清单", exact: true })
      .click();
    await expect
      .poll(
        () =>
          env.mutations.filter((row) => row.path.endsWith("/render-manifests"))
            .length,
      )
      .toBe(1);
    const frozen = env.manifests[0];
    await expect(panel.getByLabel("渲染清单", { exact: true })).toHaveValue(
      frozen.id,
    );
    await expect.poll(() => waiting).toBeGreaterThan(1);
    await panel.getByRole("button", { name: "提交渲染", exact: true }).click();
    await expect.poll(() => env.jobs.length).toBe(1);
    const current = panel.getByLabel("当前渲染任务", { exact: true });
    await expect(current).toContainText(env.jobs[0].id);
    gate.release();
    await expect.poll(() => settled).toBe(waiting);
    await expect(panel.getByLabel("渲染清单", { exact: true })).toHaveValue(
      frozen.id,
    );
    await expect(current).toContainText(env.jobs[0].id);
  } finally {
    gate.release();
  }
});

test("RENDER-12：读取历史成功任务后请求收敛，成功不冒充正式交付", async ({
  page,
}) => {
  const env = await renderFixture(page);
  const succeeded = env.job(env.manifests[0]);
  Object.assign(succeeded, {
    status: "SUCCEEDED",
    progress: 100,
    phase: "COMPLETED",
    finished_at: "2026-11-11T07:01:00Z",
    is_delivery: false,
    delivery_phase: "PREVIEW",
  });
  env.jobs.push(succeeded);
  const panel = await env.open();
  const current = panel.getByLabel("当前渲染任务", { exact: true });
  await expect(current).toContainText("正式交付是否通过");
  let count = 0;
  let changedAt = Date.now();
  await expect
    .poll(
      () => {
        const next = env.reads.filter((value) =>
          new URL(value).pathname.endsWith("/jobs"),
        ).length;
        if (next !== count) {
          count = next;
          changedAt = Date.now();
        }
        return count > 4 ? -1 : Date.now() - changedAt;
      },
      { timeout: 5000 },
    )
    .toBeGreaterThan(500);
  await expect(current).toContainText(succeeded.id);
  expect(env.mutations).toHaveLength(0);
});
