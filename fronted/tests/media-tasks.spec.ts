import { expect, test, type Locator, type Page } from "@playwright/test";
import { deferred, mediaFixture, secret } from "./media-tasks-helpers";
import { openDirectory } from "./workspace-helpers";

const byName = (scope: Locator, name: string) =>
  scope.getByRole("button", { name, exact: true });
const status = (detail: Locator) =>
  detail.getByLabel("媒体任务状态", { exact: true });
const revision = (detail: Locator) =>
  detail.getByLabel("媒体任务修订", { exact: true });
const confirmResolve = "我已核实证据及供应商结果，并确认提交此回执";
const confirmReplay = "我确认创建新任务，并接受重新校验预算";
const chooseResult = async (page: Page, detail: Locator, name: string) => {
  await detail.getByRole("combobox", { name: "确认结果", exact: true }).click();
  await page.getByRole("option", { name, exact: true }).click();
};
const ready = async (detail: Locator, value = 7) => {
  await expect(revision(detail)).toHaveText(`修订 ${value}`);
  await expect(detail).toHaveAttribute("aria-busy", "false");
};

test("UI-05 UI5-01/02/03/04/08：项目列表保留文字入口，历史尝试、真实队列及未知费用使用安全摘要", async ({
  page,
}, info) => {
  const env = await mediaFixture(page);
  env.terminal();
  env.deadLetter();
  const panel = await env.open();
  await expect(panel).toContainText("包含其他剧集与未分集任务");
  await expect(
    page.getByRole("heading", { name: "创作任务与审核", exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("region", { name: "媒体队列", exact: true }),
  ).toContainText("按业务任务计数");
  await expect(panel.getByText("交互队列", { exact: true })).toBeVisible();
  await expect(panel.getByText("批量队列", { exact: true })).toBeVisible();
  await expect(panel).toContainText("最长等待 83 秒");
  await expect(panel.getByRole("progressbar")).toHaveCount(0);
  const detail = await env.select();
  await ready(detail);
  await expect(status(detail)).toHaveText("服务端状态 FAILED");
  await expect(detail).toContainText(env.jobs[0].input_manifest_id);
  await expect(detail).toContainText(env.jobs[0].target_asset_id!);
  await expect(detail).toContainText(
    "historical-provider / frozen-image-model-v7",
  );
  const attempts = detail.getByRole("region", {
    name: "媒体尝试记录",
    exact: true,
  });
  await expect(attempts).toContainText("attempt-provider-1 / attempt-model-1");
  await expect(attempts.getByText("2345 毫秒", { exact: true })).toBeVisible();
  await expect(attempts).toContainText("供应商不可用");
  const costs = detail.getByRole("region", {
    name: "媒体任务费用",
    exact: true,
  });
  await expect(costs).toContainText("0.250000 USD");
  await expect(costs).toContainText("未知");
  await expect(costs).toContainText("预留代表预算占用");
  await expect(detail).toContainText("ATTEMPT_LIMIT");
  await expect(page.locator("body")).not.toContainText(secret);
  await page.setViewportSize({ width: 1440, height: 1100 });
  await detail.screenshot({ path: info.outputPath("ui05-desktop-detail.png") });
});

test("UI-05 UI5-01/08：空态与读取失败可显式刷新恢复，不伪造任务成功", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.jobs.length = 0;
  env.control.listStatus = 503;
  const panel = await env.open();
  await expect(panel).toContainText("媒体任务暂不可读");
  env.control.listStatus = undefined;
  await byName(panel, "刷新媒体任务").click();
  await expect(
    panel.getByRole("heading", { name: "还没有媒体任务", exact: true }),
  ).toBeVisible();
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-01/07：列表按项目有界分页，不把当前页冒充剧集完整集合", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.jobs.splice(
    0,
    1,
    ...Array.from({ length: 21 }, (_, index) =>
      env.createJob("FAILED", index % 2 ? env.episodes[1].id : null),
    ),
  );
  const panel = await env.open();
  await expect(byName(panel, `查看媒体任务 ${env.jobs[0].id}`)).toBeVisible();
  await expect(byName(panel, `查看媒体任务 ${env.jobs[20].id}`)).toHaveCount(0);
  await expect(panel).toContainText("其他剧集");
  await expect(panel).toContainText("未分集任务");
  await byName(panel, "下一页媒体任务").click();
  await expect(byName(panel, `查看媒体任务 ${env.jobs[20].id}`)).toBeVisible();
  await expect(byName(panel, `查看媒体任务 ${env.jobs[0].id}`)).toHaveCount(0);
  expect(
    env.reads.some((value) => {
      const url = new URL(value);
      return (
        url.pathname.endsWith("/generation-jobs") &&
        url.searchParams.get("limit") === "20" &&
        url.searchParams.get("offset") === "20" &&
        !url.searchParams.has("episode_id")
      );
    }),
  ).toBe(true);
  await byName(panel, "上一页媒体任务").click();
  await expect(byName(panel, `查看媒体任务 ${env.jobs[0].id}`)).toBeVisible();
});

test("UI-05 UI5-02/04：attempt、调用和费用明细各自分页，历史快照不受当前配置影响", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const job = env.terminal();
  job.attempt_count = 21;
  const attempts = Array.from({ length: 21 }, (_, index) =>
    env.createAttempt(job, index + 1),
  );
  env.attempts.set(job.id, attempts);
  const calls = Array.from({ length: 21 }, (_, index) => ({
    id: crypto.randomUUID(),
    job_id: job.id,
    attempt_id: attempts[0].id,
    kind: `QUERY_${index + 1}`,
    started_at: "2026-11-11T07:00:00Z",
    completed_at: "2026-11-11T07:00:01Z",
    released_at: null,
    outcome: `OUTCOME_${index + 1}`,
  }));
  env.calls.set(job.id, calls);
  env.costEntries.set(
    job.id,
    Array.from({ length: 11 }, (_, index) => ({
      id: crypto.randomUUID(),
      task_id: job.id,
      provider: `cost-provider-${index + 1}`,
      model: "cost-frozen-model",
      attempt_no: index + 1,
      attempt_id: attempts[index].id,
      call_id: calls[index].id,
      input_manifest_id: job.input_manifest_id,
      trace_id: `trace-${index + 1}`,
      actual_cost: null,
      estimated_cost: "0.250000",
      pending_estimate: "0.250000",
      cost_is_estimate: true,
    })),
  );
  await env.open();
  const detail = await env.select();
  await ready(detail);
  const attemptRegion = detail.getByRole("region", {
    name: "媒体尝试记录",
    exact: true,
  });
  await expect(
    attemptRegion.getByLabel("尝试 1", { exact: true }),
  ).toBeVisible();
  await expect(
    attemptRegion.getByLabel("尝试 21", { exact: true }),
  ).toHaveCount(0);
  await byName(detail, "下一页尝试记录").click();
  await expect(
    attemptRegion.getByLabel("尝试 21", { exact: true }),
  ).toContainText("attempt-model-21");
  await byName(detail, "下一页供应商调用").click();
  await expect(
    detail.getByRole("region", { name: "媒体供应商调用", exact: true }),
  ).toContainText("QUERY_21");
  await byName(detail, "下一页任务费用").click();
  await expect(
    detail.getByRole("region", { name: "媒体任务费用", exact: true }),
  ).toContainText("cost-provider-11");
  for (const [ending, offset, limit] of [
    ["/attempts", "20", "20"],
    ["/calls", "20", "20"],
    ["/costs/entries", "10", "10"],
  ]) {
    expect(
      env.reads.some((value) => {
        const url = new URL(value);
        return (
          url.pathname.endsWith(ending) &&
          url.searchParams.get("offset") === offset &&
          url.searchParams.get("limit") === limit
        );
      }),
    ).toBe(true);
  }
});

test("UI-05 UI5-03/05/07：轮询读取精确新revision后取消，双击只发送一次CAS命令", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  env.jobs[0].revision = 8;
  await expect(revision(detail)).toHaveText("修订 8", { timeout: 10000 });
  const gate = deferred();
  env.control.mutationGate = gate.promise;
  page.on("dialog", (dialog) => void dialog.accept());
  const cancel = byName(detail, "请求取消媒体任务");
  await cancel.evaluate((button) => {
    (button as HTMLButtonElement).click();
    (button as HTMLButtonElement).click();
  });
  try {
    await expect.poll(() => env.mutations.length).toBe(1);
    await expect(cancel).toBeDisabled();
    expect(env.mutations[0]).toMatchObject({
      command: "cancel",
      body: { expected_revision: 8 },
    });
    expect(env.mutations[0].key).toBeUndefined();
  } finally {
    gate.release();
  }
  await expect(status(detail)).toHaveText("服务端状态 CANCEL_REQUESTED");
  await expect(revision(detail)).toHaveText("修订 9");
  await expect(panel).not.toContainText("取消成功");
});

test("UI-05 UI5-05/06：未知结果的取消只记录意图，继续核验而不能盲目重试", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal("RESULT_UNKNOWN");
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await expect(byName(detail, "重试媒体任务")).toHaveCount(0);
  await expect(byName(detail, "从死信重新发起")).toHaveCount(0);
  page.on("dialog", (dialog) => void dialog.accept());
  await byName(detail, "请求取消媒体任务").click();
  await expect(status(detail)).toHaveText("服务端状态 RESULT_UNKNOWN");
  await expect(
    detail.getByLabel("媒体任务取消请求", { exact: true }),
  ).toHaveText("已请求取消");
  await byName(detail, "核验供应商结果").click();
  await expect(status(detail)).toHaveText("服务端状态 MANUAL_REVIEW");
  expect(env.mutations.map((row) => row.command)).toEqual([
    "cancel",
    "reconcile",
  ]);
  expect(env.mutations[1].body).toEqual({ expected_revision: 8 });
});

test("UI-05 UI5-03/05：retry返回200仍FAILED时展示阻塞，不误报已重试", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const job = env.terminal();
  env.control.retryUnchanged = true;
  env.schedules.set(job.id, {
    job_id: job.id,
    policy_json: { max_attempts: 1 },
    deadline_at: "2026-11-11T07:10:00Z",
    next_attempt_at: "2026-11-11T07:00:00Z",
    blocked_code: "ATTEMPT_LIMIT",
  });
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "重试媒体任务").click();
  await expect.poll(() => env.mutations.length).toBe(1);
  await ready(detail);
  await expect(status(detail)).toHaveText("服务端状态 FAILED");
  await expect(detail).toContainText("ATTEMPT_LIMIT");
  await expect(panel).not.toContainText("重试成功");
  await expect(panel).not.toContainText("已重试");
  expect(env.mutations[0].body).toEqual({ expected_revision: 7 });
});

test("UI-05 UI5-04/05：明确失败重试只排队并刷新权威费用", async ({ page }) => {
  const env = await mediaFixture(page);
  env.terminal();
  await env.open();
  const detail = await env.select();
  await ready(detail);
  env.totals.pending_estimate = "0.500000";
  await byName(detail, "重试媒体任务").click();
  await expect(status(detail)).toHaveText("服务端状态 RETRY_WAIT");
  await expect(revision(detail)).toHaveText("修订 8");
  await expect(
    detail.getByRole("region", { name: "媒体任务费用", exact: true }),
  ).toContainText("0.500000 USD");
  expect(env.mutations).toHaveLength(1);
  expect(env.mutations[0].key).toBeUndefined();
});

test("UI-05 UI5-05/06：明确失败但已有取消意图时不再提供重试", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const job = env.terminal();
  job.cancel_requested_at = "2026-11-11T07:00:01Z";
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await expect(status(detail)).toHaveText("服务端状态 FAILED");
  await expect(
    detail.getByLabel("媒体任务取消请求", { exact: true }),
  ).toHaveText("已请求取消");
  await expect(byName(detail, "重试媒体任务")).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-05：明确失败任务可以结束为取消，历史attempt保留且不再重试", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const job = env.terminal();
  const frozenAttempt = structuredClone(env.attempts.get(job.id));
  await env.open();
  const detail = await env.select();
  await ready(detail);
  page.on("dialog", (dialog) => void dialog.accept());
  await byName(detail, "请求取消媒体任务").click();
  await expect(status(detail)).toHaveText("服务端状态 CANCELLED");
  await expect(revision(detail)).toHaveText("修订 8");
  await expect(byName(detail, "重试媒体任务")).toHaveCount(0);
  expect(env.mutations).toHaveLength(1);
  expect(env.mutations[0]).toMatchObject({
    command: "cancel",
    body: { expected_revision: 7 },
  });
  expect(env.attempts.get(job.id)).toEqual(frozenAttempt);
});

test("UI-05 UI5-06：IMAGE连续性修订任务不能借死信入口绕过唯一修订链", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const job = env.terminal();
  env.deadLetter();
  const attempt = env.attempts.get(job.id)![0];
  attempt.input_snapshot_json = {
    context: {
      generation: { batch_size: 1, prompt: secret },
      continuity_revision: { revision_id: crypto.randomUUID() },
    },
  };
  await env.open();
  const detail = await env.select();
  await ready(detail);
  expect(job.task_type).toBe("IMAGE");
  await expect(detail).toContainText("ATTEMPT_LIMIT");
  await expect(byName(detail, "从死信重新发起")).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-05/07：409冲突重新读取最新版本后才允许再次提交", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  for (const lane of env.queue.lanes)
    Object.assign(lane, {
      ready: 0,
      backoff: 0,
      waiting_permit: 0,
      executing: 0,
      unknown: 0,
    });
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  env.jobs[0].revision = 11;
  await byName(detail, "重试媒体任务").click();
  await expect(panel).toContainText("任务版本已更新");
  await expect(revision(detail)).toHaveText("修订 11");
  await expect(byName(detail, "重试媒体任务")).toBeEnabled();
  await byName(detail, "重试媒体任务").click();
  await expect(revision(detail)).toHaveText("修订 12");
  expect(env.mutations.map((row) => row.body.expected_revision)).toEqual([
    7, 11,
  ]);
});

for (const access of ["viewer", "archived"] as const) {
  test(`UI-05 UI5-07：${access}可读取媒体详情与费用且无写操作入口`, async ({
    page,
  }) => {
    const env = await mediaFixture(page);
    env.terminal("MANUAL_REVIEW");
    if (access === "viewer") env.control.role = "viewer";
    else env.control.archived = true;
    const panel = await env.open();
    const detail = await env.select();
    await ready(detail);
    await expect(panel).toContainText("当前为只读状态");
    await expect(
      detail.getByRole("region", { name: "媒体任务费用", exact: true }),
    ).toContainText("0.250000 USD");
    for (const action of [
      "重试媒体任务",
      "请求取消媒体任务",
      "核验供应商结果",
      "人工确认结果",
      "从死信重新发起",
    ])
      await expect(byName(detail, action)).toHaveCount(0);
    expect(env.mutations).toHaveLength(0);
  });
}

test("UI-05 UI5-06/07：Editor可核验未知任务但不能人工确认或死信重新发起", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.control.role = "editor";
  env.terminal("RESULT_UNKNOWN");
  const failed = env.createJob("FAILED");
  env.jobs.push(failed);
  env.attempts.set(failed.id, [env.createAttempt(failed)]);
  env.deadLetter(failed);
  await env.open();
  let detail = await env.select();
  await ready(detail);
  await expect(byName(detail, "核验供应商结果")).toBeEnabled();
  await expect(byName(detail, "人工确认结果")).toHaveCount(0);
  detail = await env.select(failed);
  await ready(detail);
  await expect(byName(detail, "重试媒体任务")).toBeEnabled();
  await expect(byName(detail, "从死信重新发起")).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-06：Owner人工确认执行中须明确证据和确认，沿用同一attempt且不重新生成", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal("MANUAL_REVIEW");
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "人工确认结果").click();
  const submit = byName(detail, "提交人工确认");
  await expect(submit).toBeDisabled();
  await detail
    .getByLabel("证据引用", { exact: true })
    .fill("ticket:provider-audit/42");
  await detail.getByLabel(confirmResolve, { exact: true }).check();
  await submit.click();
  await expect(status(detail)).toHaveText("服务端状态 RUNNING");
  expect(env.mutations).toHaveLength(1);
  expect(env.mutations[0]).toMatchObject({
    command: "resolve",
    body: {
      expected_revision: 7,
      evidence_reference: "ticket:provider-audit/42",
      confirmed: true,
      receipt: { status: "RUNNING" },
    },
  });
  expect(env.mutations[0].key).toBeUndefined();
  expect(env.jobs).toHaveLength(1);
  expect(env.attempts.get(env.jobs[0].id)).toHaveLength(1);
});

test("UI-05 UI5-04/06：Owner成功回执提交精确输出及六位账单，费用以刷新结果为准", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal("MANUAL_REVIEW");
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "人工确认结果").click();
  await detail
    .getByLabel("证据引用", { exact: true })
    .fill("ticket:confirmed-success");
  await chooseResult(page, detail, "供应商已成功");
  await detail
    .getByLabel("输出引用（每行一项）", { exact: true })
    .fill("artifact:confirmed-image");
  await detail
    .getByLabel("实际费用（USD，可选）", { exact: true })
    .fill("0.123456");
  await detail.getByLabel("耗时（毫秒，可选）", { exact: true }).fill("2345");
  await detail.getByLabel(confirmResolve, { exact: true }).check();
  await byName(detail, "提交人工确认").click();
  await expect(status(detail)).toHaveText("服务端状态 SUCCEEDED");
  await expect(
    detail.getByRole("region", { name: "媒体任务费用", exact: true }),
  ).toContainText("0.123456 USD");
  expect(env.mutations[0].body).toEqual({
    expected_revision: 7,
    evidence_reference: "ticket:confirmed-success",
    confirmed: true,
    receipt: {
      status: "SUCCEEDED",
      result: { output_handles: ["artifact:confirmed-image"] },
      cost: "0.123456",
      latency_ms: 2345,
    },
  });
});

test("UI-05 UI5-06/08：不接受URL证据或错误数量输出，不允许把未知当作未接收", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal("MANUAL_REVIEW");
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "人工确认结果").click();
  await detail
    .getByLabel("证据引用", { exact: true })
    .fill("https://private-provider.invalid/audit");
  await detail.getByLabel(confirmResolve, { exact: true }).check();
  const submit = byName(detail, "提交人工确认");
  await submit.click();
  await expect(detail).toContainText("不要填写网址");
  expect(env.mutations).toHaveLength(0);
  await detail.getByLabel("证据引用", { exact: true }).fill("ticket:verified");
  await chooseResult(page, detail, "供应商已成功");
  await detail
    .getByLabel("输出引用（每行一项）", { exact: true })
    .fill("artifact:one\nartifact:two");
  await detail.getByLabel(confirmResolve, { exact: true }).check();
  await submit.click();
  await expect(detail).toContainText("成功回执须填写 1 个有效输出引用");
  expect(env.mutations).toHaveLength(0);
  await detail.getByRole("combobox", { name: "确认结果", exact: true }).click();
  await expect(
    page.getByRole("option", { name: /NOT_FOUND|未知|未找到/ }),
  ).toHaveCount(0);
  await page.keyboard.press("Escape");
});

test("UI-05 UI5-06：明确未接收回执不附费用或耗时，按服务端实际返回继续安全排队", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal("MANUAL_REVIEW");
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "人工确认结果").click();
  await detail
    .getByLabel("证据引用", { exact: true })
    .fill("ticket:provider-not-accepted");
  await chooseResult(page, detail, "供应商明确未接收");
  await detail.getByLabel(confirmResolve, { exact: true }).check();
  await byName(detail, "提交人工确认").click();
  await expect(status(detail)).toHaveText("服务端状态 RETRY_WAIT");
  expect(env.mutations[0].body).toEqual({
    expected_revision: 7,
    evidence_reference: "ticket:provider-not-accepted",
    confirmed: true,
    receipt: { status: "NOT_ACCEPTED" },
  });
  expect(env.jobs).toHaveLength(1);
});

test("UI-05 UI5-06/07：死信重新发起响应丢失后刷新仍复用同一key及原请求，只创建一个新任务", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const source = env.terminal();
  env.deadLetter();
  env.control.replayDropOnce = true;
  const panel = await env.open();
  let detail = await env.select(source);
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  await detail
    .getByLabel("重新发起原因", { exact: true })
    .fill("供应商已恢复，按原冻结输入重新发起");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect(panel).toContainText("网络");
  await expect
    .poll(() => env.mutations.filter((row) => row.command === "replay").length)
    .toBe(1);
  expect(env.replayed.size).toBe(1);
  await page.reload();
  await expect(
    byName(
      page.getByRole("region", { name: "项目媒体任务", exact: true }),
      `查看媒体任务 ${source.id}`,
    ),
  ).toBeVisible();
  detail = await env.select(source);
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  await expect(detail.getByLabel("重新发起原因", { exact: true })).toHaveValue(
    "供应商已恢复，按原冻结输入重新发起",
  );
  await expect(
    detail.getByLabel("重新发起原因", { exact: true }),
  ).toHaveAttribute("readonly", "");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect
    .poll(() => env.mutations.filter((row) => row.command === "replay").length)
    .toBe(2);
  const submitted = env.mutations.filter((row) => row.command === "replay");
  expect(submitted[0].key).toMatch(/^[!-~]{1,160}$/);
  expect(submitted[1].key).toBe(submitted[0].key);
  expect(submitted[1].body).toEqual(submitted[0].body);
  expect(submitted[1].body).toEqual({
    expected_revision: 7,
    reason: "供应商已恢复，按原冻结输入重新发起",
  });
  expect(env.replayed.size).toBe(1);
  expect(env.jobs).toHaveLength(2);
  expect(source.status).toBe("FAILED");
});

test("UI-05 UI5-06：存储不可用时禁止创建无法保留幂等键的死信新任务", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  env.deadLetter();
  await page.addInitScript(() => {
    const set = Storage.prototype.setItem;
    Storage.prototype.setItem = function (key, value) {
      if (key.toLowerCase().includes("replay"))
        throw new DOMException("No storage", "QuotaExceededError");
      return set.call(this, key, value);
    };
  });
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  await detail
    .getByLabel("重新发起原因", { exact: true })
    .fill("已确认供应商恢复");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect(panel).toContainText(/保存|存储/);
  expect(env.mutations).toHaveLength(0);
  expect(env.jobs).toHaveLength(1);
});

test("UI-05 UI5-06：首次重新发起明确422可修正原因，再用新命令提交", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  env.deadLetter();
  env.control.mutationStatus = 422;
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  const reason = detail.getByLabel("重新发起原因", { exact: true });
  await reason.fill("尚需修正的理由");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect(panel).toContainText("本次重新发起已被拒绝");
  await expect(reason).not.toHaveAttribute("readonly");
  expect(
    await page.evaluate(() =>
      Object.keys(sessionStorage).filter((key) =>
        key.startsWith("arclight:media-replay:"),
      ),
    ),
  ).toEqual([]);
  env.control.mutationStatus = undefined;
  await reason.fill("已核实供应商恢复，重新发起原输入");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect.poll(() => env.mutations.length).toBe(2);
  await expect.poll(() => env.replayed.size).toBe(1);
  expect(env.mutations[1].key).not.toBe(env.mutations[0].key);
  expect(env.mutations.map((row) => row.body.reason)).toEqual([
    "尚需修正的理由",
    "已核实供应商恢复，重新发起原输入",
  ]);
});

test("UI-05 UI5-06/07：已存在响应未知的重新发起命令，经422与撤权403后仍保留原key核验", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const source = env.terminal();
  env.deadLetter();
  env.control.replayDropOnce = true;
  let panel = await env.open();
  let detail = await env.select(source);
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  await detail
    .getByLabel("重新发起原因", { exact: true })
    .fill("核验同一次新任务");
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect(panel).toContainText("结果尚未确认");
  const original = env.mutations[0];
  for (const failureStatus of [422, 403]) {
    await page.reload();
    panel = page.getByRole("region", { name: "项目媒体任务", exact: true });
    detail = await env.select(source);
    await ready(detail);
    await byName(detail, "从死信重新发起").click();
    await expect(
      detail.getByLabel("重新发起原因", { exact: true }),
    ).toHaveValue("核验同一次新任务");
    env.control.mutationStatus = failureStatus;
    await detail.getByLabel(confirmReplay, { exact: true }).check();
    await byName(detail, "确认重新发起").click();
    await expect(panel).toContainText("结果尚未确认");
    const stored = await page.evaluate(() =>
      Object.keys(sessionStorage)
        .filter((key) => key.startsWith("arclight:media-replay:"))
        .map((key) => JSON.parse(sessionStorage.getItem(key)!)),
    );
    expect(stored).toHaveLength(1);
    expect(stored[0].key).toBe(original.key);
    env.control.mutationStatus = undefined;
  }
  await page.reload();
  detail = await env.select(source);
  await ready(detail);
  await byName(detail, "从死信重新发起").click();
  await detail.getByLabel(confirmReplay, { exact: true }).check();
  await byName(detail, "确认重新发起").click();
  await expect.poll(() => env.mutations.length).toBe(4);
  for (const submitted of env.mutations) {
    expect(submitted.key).toBe(original.key);
    expect(submitted.body).toEqual(original.body);
  }
  expect(env.replayed.size).toBe(1);
  expect(env.jobs).toHaveLength(2);
});

test("UI-05 UI5-07：较旧详情回执不能回退已显示的revision或恢复写权限", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  env.jobs[0].revision = 12;
  await byName(panel, "刷新媒体任务").click();
  await expect(revision(detail)).toHaveText("修订 12");
  env.control.jobRead = async (route, job) => {
    await route.fulfill({ json: { ...job, revision: 6 } });
    return true;
  };
  await byName(panel, "刷新媒体任务").click();
  await expect(detail).toContainText("尚未确认最新状态");
  await expect(revision(detail)).toHaveText("修订 12");
  await expect(byName(detail, "重试媒体任务")).toBeDisabled();
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-07：写动作废弃尚未完成的旧列表读取，取消结果不被旧RUNNING覆盖", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  const old = structuredClone(env.jobs[0]);
  const gate = deferred();
  let pending = false;
  await page.route(
    `**/api/v1/projects/${env.project.id}/generation-jobs?*`,
    async (route) => {
      pending = true;
      await gate.promise;
      await route.fulfill({ json: [old] }).catch(() => {});
    },
  );
  // Background list polling may overlap a detail command, so the command must
  // invalidate that request rather than accepting its older RUNNING snapshot.
  await expect.poll(() => pending, { timeout: 10000 }).toBe(true);
  page.on("dialog", (dialog) => void dialog.accept());
  try {
    await byName(detail, "请求取消媒体任务").click();
    await expect.poll(() => env.mutations.length).toBe(1);
  } finally {
    gate.release();
  }
  await page.unroute(`**/api/v1/projects/${env.project.id}/generation-jobs?*`);
  await expect(status(detail)).toHaveText("服务端状态 CANCEL_REQUESTED");
  await expect(revision(detail)).toHaveText("修订 8");
  await expect(
    panel.getByRole("article", {
      name: `媒体任务 ${env.jobs[0].id}`,
      exact: true,
    }),
  ).toContainText("服务端状态 CANCEL_REQUESTED");
});

test("UI-05 UI5-07：切集取消旧详情请求，迟到旧响应不能复活选择", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  const panel = await env.open();
  const gate = deferred();
  let started = false;
  let aborted = false;
  const job = env.jobs[0];
  page.on("requestfailed", (request) => {
    if (new URL(request.url()).pathname.endsWith(`/generation-jobs/${job.id}`))
      aborted ||= request.failure()?.errorText.includes("ERR_ABORTED") ?? false;
  });
  env.control.jobRead = async (route, row) => {
    started = true;
    await gate.promise;
    await route.fulfill({ json: row }).catch(() => {});
    return true;
  };
  await byName(panel, `查看媒体任务 ${job.id}`).click();
  await expect.poll(() => started).toBe(true);
  await openDirectory(page);
  await page
    .getByRole("button", { name: new RegExp(env.episodes[1].title) })
    .click();
  try {
    await expect(page).toHaveURL(new RegExp(env.episodes[1].id));
    await expect.poll(() => aborted).toBe(true);
  } finally {
    gate.release();
  }
  await expect(
    page.getByRole("region", { name: `媒体任务详情 ${job.id}`, exact: true }),
  ).toHaveCount(0);
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-04/08：费用暂无事实与局部读取失败不伪装成零，也不影响其他详情", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  env.control.costStatus = 404;
  env.control.queueStatus = 503;
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  await expect(panel).toContainText("队列暂不可读");
  await expect(status(detail)).toHaveText("服务端状态 FAILED");
  await expect(
    detail.getByRole("region", { name: "媒体任务费用", exact: true }),
  ).not.toContainText("0.000000 USD");
  await expect(detail).toContainText(/费用|记录/);
  env.control.costStatus = undefined;
  env.control.queueStatus = undefined;
  await byName(panel, "刷新媒体任务").click();
  await expect(
    detail.getByRole("region", { name: "媒体任务费用", exact: true }),
  ).toContainText("0.250000 USD");
});

test("UI-05 UI5-07/08：详情权限撤销后保留错误提示且不能继续写入", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  env.terminal();
  const panel = await env.open();
  const detail = await env.select();
  await ready(detail);
  env.control.detailStatus = 403;
  await byName(panel, "刷新媒体任务").click();
  await expect(detail).toContainText("此任务权限已变化");
  const retry = byName(detail, "重试媒体任务");
  if (await retry.count()) await expect(retry).toBeDisabled();
  expect(env.mutations).toHaveLength(0);
});

test("UI-05 UI5-08/09：390px未知结果恢复表单支持键盘且无横向溢出", async ({
  page,
}, info) => {
  const env = await mediaFixture(page);
  env.terminal("MANUAL_REVIEW");
  await page.setViewportSize({ width: 390, height: 844 });
  await env.open();
  const detail = await env.select();
  await ready(detail);
  await expect(status(detail)).toHaveText("服务端状态 MANUAL_REVIEW");
  const recover = byName(detail, "人工确认结果");
  await recover.focus();
  await expect(recover).toBeFocused();
  await page.keyboard.press("Enter");
  const evidence = detail.getByLabel("证据引用", { exact: true });
  await expect(evidence).toBeVisible();
  await evidence.fill("ticket:mobile-review");
  await expect(
    detail.getByRole("combobox", { name: "确认结果", exact: true }),
  ).toContainText("供应商仍在执行");
  await expect(byName(detail, "提交人工确认")).toBeDisabled();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await evidence.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("ui05-mobile-recovery.png") });
  await expect(page.locator("body")).not.toContainText(secret);
  expect(env.mutations).toHaveLength(0);
});
