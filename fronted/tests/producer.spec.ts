import { test, expect, type Page } from "@playwright/test";
import type { ProducerProgress } from "../src/features/tasks/producer-types";
import { closeDirectory } from "./workspace-helpers";

test.afterEach(async ({ page }) => {
  // Drain route.fetch/fulfill before page teardown so handler failures stay
  // attributed to this test instead of leaking into the next one.
  await page.unrouteAll({ behavior: "wait" });
});

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `producer-${crypto.randomUUID()}@example.test`,
      password: "producer-browser-acceptance",
      display_name: "生产协作验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "纸船生产协作",
      story_settings: {
        target_episode_count: 2,
        default_episode_duration_ms: 30000,
      },
    },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const response = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "纸船归途" } },
  );
  expect(response.status()).toBe(201);
  const episode = await response.json();
  const href = `/projects/${project.id}/episodes/${episode.id}?view=tasks`;
  await page.goto(href);
  await closeDirectory(page);
  await expect(
    page.getByRole("heading", { name: "Producer 生产协作", exact: true }),
  ).toBeVisible();
  return { headers, project, episode, href };
}

const progress = (page: Page) =>
  page.getByRole("region", { name: "Producer 生产进度", exact: true });

const productionGoal = (page: Page) =>
  progress(page)
    .getByRole("heading", { name: "生产目标", exact: true })
    .locator("..");

async function readProgress(
  page: Page,
  runId: string,
  headers: { Authorization: string },
): Promise<ProducerProgress> {
  const response = await page.request.get(`/api/v1/runs/${runId}/producer`, {
    headers,
  });
  expect(response.status()).toBe(200);
  return response.json();
}

async function start(page: Page, episodeId: string, goal: string) {
  await page.getByLabel("生产目标", { exact: true }).fill(goal);
  const accepted = page.waitForResponse(
    (response) =>
      response.request().method() === "POST" &&
      new URL(response.url()).pathname ===
        `/api/v1/episodes/${episodeId}/producer-runs`,
  );
  await page.getByRole("button", { name: "发起生产计划", exact: true }).click();
  const response = await accepted;
  expect(response.status()).toBe(202);
  const result = await response.json();
  await expect(progress(page)).toBeVisible();
  return result.run_id as string;
}

test("Producer answers the exact saved question by keyboard after refresh and replays a lost response once", async ({
  page,
}, info) => {
  const env = await setup(page);
  const goal = "需要澄清：为纸船归途先完成本集剧本。";
  const runId = await start(page, env.episode.id, goal);
  const panel = progress(page);
  await expect(
    panel.getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const before = await readProgress(page, runId, env.headers);
  expect(before.operations).toHaveLength(1);
  const operation = before.operations[0];
  expect(operation.question_id).toBeTruthy();
  expect(operation.can_answer).toBe(true);
  const questionForm = panel.locator("form").filter({
    has: page.getByText("先制作本集剧本吗？", { exact: true }),
  });
  await questionForm.scrollIntoViewIfNeeded();
  await expect(questionForm).toBeInViewport();
  await questionForm.screenshot({
    path: info.outputPath("producer-saved-question-desktop.png"),
  });

  const { controlledStreams, frame } = await import("./event-stream-helpers");
  const streams = await controlledStreams(page);
  async function reloadReadyProducer() {
    await page.reload();
    await closeDirectory(page);
    const refresh = panel.getByRole("button", {
      name: "刷新生产状态",
      exact: true,
    });
    await expect(refresh).toBeEnabled();
    let streamIndex = -1;
    await expect
      .poll(async () => {
        const requests = (await streams.snapshot()) as {
          project: string;
          authorized: boolean;
          aborted: boolean;
          cancelled: boolean;
        }[];
        const active = requests
          .map((stream, index) => ({ ...stream, index }))
          .filter(
            (stream) =>
              stream.project === env.project.id &&
              stream.authorized &&
              !stream.aborted &&
              !stream.cancelled,
          );
        streamIndex = active[0]?.index ?? -1;
        return active.length;
      })
      .toBe(1);
    // A connected stream schedules another read. Wait for that read rather than
    // focusing during the briefly enabled state before stream.ready reconciles.
    const refreshed = page.waitForRequest(
      (request) =>
        request.method() === "GET" &&
        new URL(request.url()).pathname === `/api/v1/runs/${runId}/producer`,
    );
    await streams.push(streamIndex, [frame(env.project.id)]);
    const response = await (await refreshed).response();
    expect(response?.status()).toBe(200);
    expect(await response!.json()).toMatchObject({
      run_id: runId,
      project_id: env.project.id,
      episode_id: env.episode.id,
    });
    await expect(refresh).toBeEnabled();
  }

  await reloadReadyProducer();
  await expect(
    panel.getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible();
  const submitted: { body: string | null; key: string | undefined }[] = [];
  let loseResponse = true;
  await page.route(
    `**/api/v1/runs/${runId}/producer/*/answers`,
    async (route) => {
      submitted.push({
        body: route.request().postData(),
        key: route.request().headers()["idempotency-key"],
      });
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      if (loseResponse) {
        loseResponse = false;
        return route.abort("failed");
      }
      return route.fulfill({ response });
    },
  );
  const answer = panel.getByRole("radio", { name: "先制作剧本", exact: true });
  const submit = panel.getByRole("button", {
    name: "提交回答并继续",
    exact: true,
  });
  await expect(answer).toBeEnabled();
  await answer.focus();
  await expect(answer).toBeFocused();
  await page.keyboard.press("Space");
  await expect(answer).toHaveAttribute("aria-checked", "true");
  await page.keyboard.press("Tab");
  await expect(submit).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(panel.getByText(/操作结果尚未确认/)).toBeVisible();
  await expect
    .poll(async () => {
      const state = await readProgress(page, runId, env.headers);
      return {
        plans: state.plans.length,
        pending: state.pending_command !== null,
      };
    })
    .toEqual({ plans: 1, pending: false });
  const committed = await readProgress(page, runId, env.headers);
  await reloadReadyProducer();
  const replay = panel.getByRole("button", { name: "重发原操作", exact: true });
  await expect(replay).toBeEnabled();
  await replay.focus();
  await expect(replay).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(
    panel.locator("summary").filter({ hasText: "计划 v1 · 当前计划" }),
  ).toBeVisible();
  await expect(panel.getByText(/操作结果尚未确认/)).toHaveCount(0);
  expect(submitted).toHaveLength(2);
  expect(submitted[0].key).toBeTruthy();
  expect(submitted[1]).toEqual(submitted[0]);
  expect(JSON.parse(submitted[0].body!)).toEqual({
    question_id: operation.question_id,
    input_sha256: operation.input_sha256,
    decision: "ANSWER",
    answer: "先制作剧本",
  });
  const final = await readProgress(page, runId, env.headers);
  expect(final.run_id).toBe(runId);
  expect(final.operations.map((row) => row.loop_id)).toEqual(
    before.operations.map((row) => row.loop_id),
  );
  expect(final.plans).toEqual(committed.plans);
  expect(final.commands).toEqual(committed.commands);
  expect(final.budget).toEqual(committed.budget);
  const conflicting = await page.request.post(
    `/api/v1/runs/${runId}/producer/${operation.loop_id}/answers`,
    {
      headers: env.headers,
      data: {
        question_id: operation.question_id,
        input_sha256: operation.input_sha256,
        decision: "ANSWER",
        answer: "暂停规划",
      },
    },
  );
  expect(conflicting.status()).toBe(409);
  const budget = panel.getByRole("region", {
    name: "Producer 预算与用量",
    exact: true,
  });
  await expect(
    budget.getByText("按已知用量记录", { exact: true }),
  ).toBeVisible();
  await budget.scrollIntoViewIfNeeded();
  await expect(budget).toBeInViewport();
  await budget.screenshot({
    path: info.outputPath("producer-plan-budget-desktop.png"),
  });
});

test("Producer STOP survives a mobile refresh without silently producing a plan", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await setup(page);
  const runId = await start(
    page,
    env.episode.id,
    "需要澄清：暂不开展纸船生产。",
  );
  const panel = progress(page);
  await expect(
    panel.getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const before = await readProgress(page, runId, env.headers);
  await panel
    .getByRole("button", { name: "停止本次创作", exact: true })
    .click();
  await expect
    .poll(
      async () =>
        (await readProgress(page, runId, env.headers)).operations[0].status,
    )
    .toBe("STOPPED");
  await page.reload();
  await closeDirectory(page);
  const cancelled = panel.getByText("已取消", { exact: true });
  await expect(cancelled).toBeVisible();
  await expect(
    panel.locator("summary").filter({ hasText: "计划 v1" }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toHaveCount(0);
  const after = await readProgress(page, runId, env.headers);
  expect(after.plans).toEqual([]);
  expect(after.status).toBe("CANCELLED");
  expect(after.operations[0].loop_id).toBe(before.operations[0].loop_id);
  expect(after.operations[0].can_answer).toBe(false);
  await cancelled.scrollIntoViewIfNeeded();
  await expect(cancelled).toBeInViewport();
  await page.screenshot({
    path: info.outputPath("producer-stopped-mobile.png"),
    fullPage: true,
  });
});

test("a real viewer reads the Producer question while writes and unrelated project access are denied", async ({
  page,
}) => {
  const env = await setup(page);
  const runId = await start(
    page,
    env.episode.id,
    "需要澄清：只读角色检查生产问题。",
  );
  await expect(
    progress(page).getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const original = await readProgress(page, runId, env.headers);
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `producer-viewer-${crypto.randomUUID()}@example.test`,
      password: "producer-viewer-acceptance",
      display_name: "生产只读成员",
    },
  });
  expect(registered.status()).toBe(201);
  const viewer = await registered.json();
  const viewerHeaders = { Authorization: `Bearer ${viewer.access_token}` };
  expect(
    (
      await page.request.get(`/api/v1/runs/${runId}/producer`, {
        headers: viewerHeaders,
      })
    ).status(),
  ).toBe(404);
  expect(
    (
      await page.request.post(`/api/v1/projects/${env.project.id}/members`, {
        headers: env.headers,
        data: { email: viewer.user.email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  await page.goto(`${env.href}&run=${runId}`);
  await closeDirectory(page);
  const panel = progress(page);
  await expect(
    panel.getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "发起生产计划", exact: true }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toBeDisabled();
  await expect(
    panel.getByRole("button", { name: "停止本次创作", exact: true }),
  ).toBeDisabled();
  const visible = await readProgress(page, runId, viewerHeaders);
  expect(visible.can_operate).toBe(false);
  expect(visible.operations[0].can_answer).toBe(false);
  expect(visible.operations[0].question_id).toBe(
    original.operations[0].question_id,
  );
  expect(
    (
      await page.request.post(
        `/api/v1/episodes/${env.episode.id}/producer-runs`,
        {
          headers: { ...viewerHeaders, "Idempotency-Key": crypto.randomUUID() },
          data: { goal: "越权生产" },
        },
      )
    ).status(),
  ).toBe(403);
  const operation = original.operations[0];
  expect(
    (
      await page.request.post(
        `/api/v1/runs/${runId}/producer/${operation.loop_id}/answers`,
        {
          headers: viewerHeaders,
          data: {
            question_id: operation.question_id,
            input_sha256: operation.input_sha256,
            decision: "STOP",
          },
        },
      )
    ).status(),
  ).toBe(403);
  expect(
    (
      await page.request.post(`/api/v1/runs/${runId}/producer/commands`, {
        headers: { ...viewerHeaders, "Idempotency-Key": crypto.randomUUID() },
        data: { action: "CANCEL", expected_revision: visible.revision },
      })
    ).status(),
  ).toBe(403);
});

test("Producer retains the original creation request across a lost response and refresh", async ({
  page,
}) => {
  const env = await setup(page);
  const requests: { body: string | null; key: string | undefined }[] = [];
  let lost = true;
  await page.route(
    `**/api/v1/episodes/${env.episode.id}/producer-runs`,
    async (route) => {
      requests.push({
        body: route.request().postData(),
        key: route.request().headers()["idempotency-key"],
      });
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      if (lost) {
        lost = false;
        return route.abort("failed");
      }
      return route.fulfill({ response });
    },
  );
  const goal = "保留原目标和预算，制作纸船剧本。";
  await page.getByLabel("生产目标", { exact: true }).fill(goal);
  await page.getByLabel("费用上限（USD，可选）", { exact: true }).fill("0.5");
  await page.getByLabel("模型调用上限", { exact: true }).fill("16");
  await page.getByRole("button", { name: "发起生产计划", exact: true }).click();
  await expect(page.getByText(/操作结果尚未确认/)).toBeVisible();
  const runs = await (
    await page.request.get(`/api/v1/episodes/${env.episode.id}/runs`, {
      headers: env.headers,
    })
  ).json();
  expect(runs).toHaveLength(1);
  const runId = runs[0].id;
  await page.reload();
  await closeDirectory(page);
  await expect(
    page.getByRole("textbox", { name: "生产目标", exact: true }),
  ).toHaveValue(goal);
  await expect(
    page.getByRole("textbox", { name: "生产目标", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByLabel("费用上限（USD，可选）", { exact: true }),
  ).toHaveValue("0.5");
  await page
    .getByRole("button", { name: "重发原发起请求", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`run=${runId}(?:&|$)`));
  await expect(progress(page)).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[0].key).toBeTruthy();
  expect(requests[1]).toEqual(requests[0]);
  const after = await (
    await page.request.get(`/api/v1/episodes/${env.episode.id}/runs`, {
      headers: env.headers,
    })
  ).json();
  expect(after.map((run: { id: string }) => run.id)).toEqual([runId]);
  const saved = await readProgress(page, runId, env.headers);
  expect(saved.goal).toBe(goal);
  expect(Number(saved.budget.limits.maximum_cost)).toBe(0.5);
  expect(saved.budget.limits.max_model_calls).toBe(16);
});

test("Producer delegates the canonical child, preserves it in replanning and stops only the root", async ({
  page,
}, info) => {
  const env = await setup(page);
  const runId = await start(
    page,
    env.episode.id,
    "先制作可审核的纸船归途剧本。",
  );
  const panel = progress(page);
  await expect(
    panel.locator("summary").filter({ hasText: "计划 v1 · 当前计划" }),
  ).toBeVisible({ timeout: 20000 });
  await expect(
    panel.getByRole("button", { name: "委派 write", exact: true }),
  ).toBeEnabled();
  const before = await readProgress(page, runId, env.headers);
  expect(before.plans).toHaveLength(1);
  expect(before.plans[0].tasks[0].can_delegate).toBe(true);
  await panel.getByRole("button", { name: "委派 write", exact: true }).click();
  await expect
    .poll(
      async () =>
        (await readProgress(page, runId, env.headers)).plans[0].tasks[0]
          .child_run_id,
    )
    .toBeTruthy();
  const delegated = await readProgress(page, runId, env.headers);
  const childId = delegated.plans[0].tasks[0].child_run_id!;
  expect(delegated.budget.delegations).toBe(1);
  await expect
    .poll(
      async () => {
        const response = await page.request.get(`/api/v1/runs/${childId}`, {
          headers: env.headers,
        });
        expect(response.status()).toBe(200);
        return (await response.json()).status;
      },
      { timeout: 20000 },
    )
    .toBe("WAITING_REVIEW");
  await panel
    .getByRole("button", { name: "刷新生产状态", exact: true })
    .click();
  await expect(
    panel.getByRole("link", { name: "查看子任务 write", exact: true }),
  ).toHaveAttribute("href", `${env.href}&run=${childId}`);
  await expect(
    panel.getByRole("button", { name: "委派 write", exact: true }),
  ).toHaveCount(0);
  const reason = "检查已委派的工作，保留原专业任务与费用记录。";
  await panel.getByLabel("重规划说明", { exact: true }).fill(reason);
  await panel
    .getByRole("button", { name: "重新观察并规划", exact: true })
    .click();
  await expect(
    panel.locator("summary").filter({ hasText: "计划 v2 · 当前计划" }),
  ).toBeVisible({ timeout: 20000 });
  const revised = await readProgress(page, runId, env.headers);
  expect(revised.plans).toHaveLength(2);
  expect(revised.plans[1].previous_plan_id).toBe(before.plans[0].plan_id);
  expect(revised.plans[1].reason).toBe(reason);
  expect(revised.plans[1].tasks[0].child_run_id).toBe(childId);
  expect(revised.budget.delegations).toBe(1);
  const revisedPlan = panel
    .locator("summary")
    .filter({ hasText: /^计划 v2 · 当前计划$/ })
    .locator("..");
  await expect(revisedPlan.locator("summary")).toHaveText("计划 v2 · 当前计划");
  await expect(
    revisedPlan.getByRole("link", { name: "查看子任务 write", exact: true }),
  ).toBeVisible();
  await revisedPlan.scrollIntoViewIfNeeded();
  await expect(revisedPlan).toBeInViewport();
  await revisedPlan.screenshot({
    path: info.outputPath("producer-replanned-child-desktop.png"),
  });
  page.once("dialog", (dialog) => dialog.accept());
  await panel
    .getByRole("button", { name: "停止生产协作", exact: true })
    .click();
  await expect
    .poll(async () => (await readProgress(page, runId, env.headers)).status)
    .toBe("CANCELLED");
  await page.reload();
  await closeDirectory(page);
  await expect(
    panel.getByRole("button", { name: "停止生产协作", exact: true }),
  ).toBeDisabled();
  await expect(
    page.getByRole("button", { name: "重试任务", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "使用当前配置重新发起", exact: true }),
  ).toHaveCount(0);
  const child = await (
    await page.request.get(`/api/v1/runs/${childId}`, { headers: env.headers })
  ).json();
  expect(child.status).toBe("WAITING_REVIEW");
  const final = await readProgress(page, runId, env.headers);
  expect(final.plans).toEqual(revised.plans);
  expect(final.budget.delegations).toBe(1);
});

// This case controls only the public projection and command response. Real
// recovery, permissions and accounting are verified by backend interaction tests.
test("Producer displays unknown costs and sends recovery for the exact pending command", async ({
  page,
}, info) => {
  const env = await setup(page);
  const runId = await start(page, env.episode.id, "检查原命令恢复界面。");
  await expect
    .poll(async () => {
      const state = await readProgress(page, runId, env.headers);
      return {
        plans: state.plans.length,
        pending: state.pending_command !== null,
      };
    })
    .toEqual({ plans: 1, pending: false });
  const saved = await readProgress(page, runId, env.headers);
  const commandId = crypto.randomUUID();
  const pending = {
    command_id: commandId,
    revision: saved.revision,
    action: "PLAN",
    status: "WAITING_EXTERNAL",
    error_code: null,
  };
  let projection: ProducerProgress = {
    ...saved,
    pending_command: pending,
    commands: [...saved.commands, pending],
    can_recover: true,
    can_replan: false,
    budget: {
      ...saved.budget,
      actual_cost: "0.125000",
      pending_estimate: "0.250000",
      unknown_cost: true,
    },
  };
  const writes: unknown[] = [];
  await page.route(`**/api/v1/runs/${runId}/producer`, (route) =>
    route.fulfill({ json: projection }),
  );
  await page.route(
    `**/api/v1/runs/${runId}/producer/commands`,
    async (route) => {
      writes.push(route.request().postDataJSON());
      projection = { ...projection, can_recover: false };
      return route.fulfill({
        status: 202,
        json: { run_id: runId, status: "RUNNING" },
      });
    },
  );
  await page.reload();
  await closeDirectory(page);
  const panel = progress(page);
  await expect(
    panel.getByText("存在未知费用，须先核验", { exact: true }),
  ).toBeVisible();
  await expect(panel.getByText("0.125000 USD", { exact: true })).toBeVisible();
  await expect(panel.getByText("0.250000 USD", { exact: true })).toBeVisible();
  await expect(panel.getByText(/外部结果尚未确认/)).toBeVisible();
  const budget = panel.getByRole("region", {
    name: "Producer 预算与用量",
    exact: true,
  });
  await budget.scrollIntoViewIfNeeded();
  await expect(budget).toBeInViewport();
  await budget.screenshot({
    path: info.outputPath("producer-unknown-budget-desktop.png"),
  });
  const recovery = panel
    .getByRole("button", { name: "恢复待处理操作", exact: true })
    .locator("..");
  await recovery.scrollIntoViewIfNeeded();
  await expect(recovery).toBeInViewport();
  await recovery.screenshot({
    path: info.outputPath("producer-unknown-recovery-desktop.png"),
  });
  await panel
    .getByRole("button", { name: "恢复待处理操作", exact: true })
    .click();
  await expect
    .poll(() => writes)
    .toEqual([
      {
        action: "RECOVER",
        expected_revision: saved.revision,
        command_id: commandId,
      },
    ]);
  await expect(
    panel.getByRole("button", { name: "恢复待处理操作", exact: true }),
  ).toBeDisabled();
  expect(projection.run_id).toBe(saved.run_id);
  expect(projection.revision).toBe(saved.revision);
  expect(projection.budget).toEqual({
    ...saved.budget,
    actual_cost: "0.125000",
    pending_estimate: "0.250000",
    unknown_cost: true,
  });
});

// Ignore AbortSignal for one controlled GET so an obsolete response genuinely
// reaches the hook after a newer read or navigation. This tests UI ordering;
// authentication, permissions and durable commands retain their real API tests.
async function delayProducerRead(
  page: Page,
  runId: string,
  stale: ProducerProgress,
  current: ProducerProgress,
) {
  await page.evaluate(
    ({ runId, stale, current }) => {
      type Race = { reads: number; delivered: boolean; release: () => void };
      const state: Race = { reads: 0, delivered: false, release: () => {} };
      Object.assign(window, { __producerRace: state });
      const original = window.fetch;
      window.fetch = async (input, init) => {
        const url = new URL(
          typeof input === "string"
            ? input
            : input instanceof URL
              ? input.href
              : input.url,
          location.href,
        );
        if (
          url.pathname !== `/api/v1/runs/${runId}/producer` ||
          (init?.method ?? "GET") !== "GET"
        )
          return original(input, init);
        state.reads++;
        if (state.reads !== 1) return Response.json(current);
        return new Promise<Response>((resolve) => {
          state.release = () => resolve(Response.json(stale));
        }).then((response) => {
          state.delivered = true;
          return response;
        });
      };
    },
    { runId, stale, current },
  );
  return {
    reads: () =>
      page.evaluate(
        () =>
          (window as unknown as { __producerRace: { reads: number } })
            .__producerRace.reads,
      ),
    release: async () => {
      await page.evaluate(() =>
        (
          window as unknown as { __producerRace: { release: () => void } }
        ).__producerRace.release(),
      );
      await expect
        .poll(() =>
          page.evaluate(
            () =>
              (window as unknown as { __producerRace: { delivered: boolean } })
                .__producerRace.delivered,
          ),
        )
        .toBe(true);
      await page.clock.runFor(16);
    },
  };
}

async function settledProducer(
  page: Page,
  runId: string,
  headers: { Authorization: string },
) {
  await expect
    .poll(async () => {
      const state = await readProgress(page, runId, headers);
      return {
        plans: state.plans.length,
        pending: state.pending_command !== null,
      };
    })
    .toEqual({ plans: 1, pending: false });
  return readProgress(page, runId, headers);
}

test("a late Producer GET with the same revision cannot restore an obsolete question", async ({
  page,
}) => {
  const { controlledStreams, pauseClock } =
    await import("./event-stream-helpers");
  await controlledStreams(page);
  const env = await setup(page);
  const runId = await start(
    page,
    env.episode.id,
    "检查同版本状态读取的先后顺序。",
  );
  const saved = await settledProducer(page, runId, env.headers);
  const panel = progress(page);
  await expect(
    panel.locator("summary").filter({ hasText: "计划 v1" }),
  ).toBeVisible();
  await pauseClock(page);
  const stale: ProducerProgress = {
    ...saved,
    goal: "过期的生产目标不应恢复",
    operations: [
      {
        ...saved.operations[0],
        status: "WAITING_HUMAN",
        question_id: crypto.randomUUID(),
        human_request: {
          question: "过期的问题不应恢复？",
          reason: "USER_DECISION",
          options: ["过期答案"],
        },
        can_answer: true,
      },
    ],
  };
  const current = { ...saved, goal: "最新生产目标保持显示" };
  expect(stale.revision).toBe(current.revision);
  const race = await delayProducerRead(page, runId, stale, current);
  await panel
    .getByRole("button", { name: "刷新生产状态", exact: true })
    .click();
  await expect.poll(race.reads).toBe(1);
  await page.clock.runFor(3000);
  await expect.poll(race.reads).toBe(2);
  await expect(panel.getByText(current.goal, { exact: true })).toBeVisible();
  await race.release();
  await expect(panel.getByText(current.goal, { exact: true })).toBeVisible();
  await expect(panel.getByText(stale.goal, { exact: true })).toHaveCount(0);
  await expect(
    panel.getByText("过期的问题不应恢复？", { exact: true }),
  ).toHaveCount(0);
  await expect(
    panel.getByRole("button", { name: "提交回答并继续", exact: true }),
  ).toHaveCount(0);
});

test("late Producer reads cannot cross episode or project navigation", async ({
  page,
}) => {
  const { controlledStreams, pauseClock } =
    await import("./event-stream-helpers");
  const { openDirectory, chooseProject } = await import("./workspace-helpers");
  await controlledStreams(page);
  const env = await setup(page);
  const firstId = await start(page, env.episode.id, "第一集原生产目标。");
  const first = await settledProducer(page, firstId, env.headers);
  const secondResponse = await page.request.post(
    `/api/v1/projects/${env.project.id}/episodes`,
    { headers: env.headers, data: { episode_no: 2, title: "桥下回声" } },
  );
  expect(secondResponse.status()).toBe(201);
  const secondEpisode = await secondResponse.json();
  const otherResponse = await page.request.post("/api/v1/projects", {
    headers: env.headers,
    data: { name: "另一作品的生产计划" },
  });
  expect(otherResponse.status()).toBe(201);
  const otherProject = await otherResponse.json();
  const thirdResponse = await page.request.post(
    `/api/v1/projects/${otherProject.id}/episodes`,
    { headers: env.headers, data: { episode_no: 1, title: "另一条河" } },
  );
  expect(thirdResponse.status()).toBe(201);
  const thirdEpisode = await thirdResponse.json();
  const later: ProducerProgress[] = [];
  for (const [episode, goal] of [
    [secondEpisode, "第二集当前生产目标。"],
    [thirdEpisode, "另一项目当前生产目标。"],
  ] as const) {
    const accepted = await page.request.post(
      `/api/v1/episodes/${episode.id}/producer-runs`,
      {
        headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
        data: { goal },
      },
    );
    expect(accepted.status()).toBe(202);
    later.push(
      await settledProducer(page, (await accepted.json()).run_id, env.headers),
    );
  }
  // Directory data must include the two episodes created after initial load.
  // Reload before holding any response; the navigations below stay in this document.
  await page.reload();
  await closeDirectory(page);
  await expect(
    progress(page).locator("summary").filter({ hasText: "计划 v1" }),
  ).toBeVisible();
  await pauseClock(page);
  const firstStale = { ...first, goal: "迟到的第一集目标不得串集" };
  const firstRace = await delayProducerRead(page, firstId, firstStale, first);
  await progress(page)
    .getByRole("button", { name: "刷新生产状态", exact: true })
    .click();
  await expect.poll(firstRace.reads).toBe(1);
  await openDirectory(page);
  await page.getByRole("button", { name: /桥下回声/ }).click();
  await expect(page).toHaveURL(
    new RegExp(`/episodes/${secondEpisode.id}(?:\\?|$)`),
  );
  await closeDirectory(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    productionGoal(page).getByText(later[0].goal, { exact: true }),
  ).toBeVisible();
  await firstRace.release();
  await expect(
    productionGoal(page).getByText(later[0].goal, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(firstStale.goal, { exact: true })).toHaveCount(0);
  const secondStale = { ...later[0], goal: "迟到的旧项目目标不得串项目" };
  const secondRace = await delayProducerRead(
    page,
    later[0].run_id,
    secondStale,
    later[0],
  );
  await progress(page)
    .getByRole("button", { name: "刷新生产状态", exact: true })
    .click();
  await expect.poll(secondRace.reads).toBe(1);
  await chooseProject(page, otherProject);
  await expect(page).toHaveURL(
    new RegExp(
      `/projects/${otherProject.id}/episodes/${thirdEpisode.id}(?:\\?|$)`,
    ),
  );
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    productionGoal(page).getByText(later[1].goal, { exact: true }),
  ).toBeVisible();
  await secondRace.release();
  await expect(
    productionGoal(page).getByText(later[1].goal, { exact: true }),
  ).toBeVisible();
  await expect(page.getByText(secondStale.goal, { exact: true })).toHaveCount(
    0,
  );
  await expect(page.getByText(firstStale.goal, { exact: true })).toHaveCount(0);
});

// Role changes below are transport projections to test client intent retention.
// They do not prove server-side revocation; the real viewer test above does.
test("an uncertain Producer answer stays saved while read only and replays unchanged when writable again", async ({
  page,
}) => {
  const env = await setup(page);
  const runId = await start(
    page,
    env.episode.id,
    "需要澄清：降权期间保存原回答。",
  );
  const panel = progress(page);
  await expect(
    panel.getByText("先制作本集剧本吗？", { exact: true }),
  ).toBeVisible({ timeout: 20000 });
  const requests: { body: string | null; key: string | undefined }[] = [];
  let role: "owner" | "viewer" | "editor" = "owner";
  let lost = true;
  await page.route(`**/api/v1/projects/${env.project.id}/access`, (route) =>
    route.fulfill({ json: { role } }),
  );
  await page.route(`**/api/v1/runs/${runId}/producer`, async (route) => {
    const response = await route.fetch();
    const original: ProducerProgress = await response.json();
    return route.fulfill({
      response,
      json:
        role === "viewer"
          ? {
              ...original,
              can_operate: false,
              can_recover: false,
              can_replan: false,
              can_cancel: false,
              readonly_reason: "FORBIDDEN",
              operations: original.operations.map((operation) => ({
                ...operation,
                can_answer: false,
              })),
            }
          : original,
    });
  });
  await page.route(
    `**/api/v1/runs/${runId}/producer/*/answers`,
    async (route) => {
      requests.push({
        body: route.request().postData(),
        key: route.request().headers()["idempotency-key"],
      });
      const response = await route.fetch();
      expect(response.status()).toBe(202);
      if (lost) {
        lost = false;
        return route.abort("failed");
      }
      return route.fulfill({ response });
    },
  );
  await panel.getByRole("radio", { name: "先制作剧本", exact: true }).click();
  await panel
    .getByRole("button", { name: "提交回答并继续", exact: true })
    .click();
  await expect(panel.getByText(/操作结果尚未确认/)).toBeVisible();
  const committed = await settledProducer(page, runId, env.headers);
  const retained = () =>
    page.evaluate(() =>
      Object.entries(sessionStorage)
        .filter(([key]) => key.startsWith("arclight:producer-intent:"))
        .map(([, value]) => JSON.parse(value)),
    );
  const saved = await retained();
  expect(saved).toHaveLength(1);
  expect(saved[0].key).toBe(requests[0].key);
  role = "viewer";
  await page.reload();
  await closeDirectory(page);
  await expect(
    panel.getByRole("button", { name: "重发原操作", exact: true }),
  ).toBeDisabled();
  expect(await retained()).toEqual(saved);
  expect(requests).toHaveLength(1);
  role = "editor";
  await page.reload();
  await closeDirectory(page);
  await panel.getByRole("button", { name: "重发原操作", exact: true }).click();
  await expect(
    panel.getByText("操作已提交，生产状态以服务端最新记录为准。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(requests).toHaveLength(2);
  expect(requests[1]).toEqual(requests[0]);
  expect(await retained()).toEqual([]);
  const after = await readProgress(page, runId, env.headers);
  expect(after.commands).toEqual(committed.commands);
  expect(after.budget).toEqual(committed.budget);
  expect(after.plans).toEqual(committed.plans);
});
