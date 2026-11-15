import { expect, test, type Page } from "@playwright/test";
import type { Run } from "../src/types/api";
import { controlledStreams, frame } from "./event-stream-helpers";
import { deferred, mediaFixture } from "./media-tasks-helpers";
import {
  chooseProject,
  closeDirectory,
  openDirectory,
  signOut,
} from "./workspace-helpers";

function projectChanges(projectId: string, start = 1, count = 100) {
  return Array.from({ length: count }, (_, index) => {
    const id = crypto.randomUUID();
    return frame(
      projectId,
      "project.changed",
      `v1:w:${id}:${start + index}`,
      id,
    );
  }).join("");
}

async function holdProjectRefreshes(page: Page) {
  const reads: ReturnType<typeof deferred>[] = [];
  await page.route("**/api/v1/projects?*", async (route) => {
    const gate = deferred();
    reads.push(gate);
    await gate.promise;
    await route.continue();
  });
  async function release(index: number, settled = true) {
    const response = page.waitForResponse(
      (response) =>
        new URL(response.url()).pathname === "/api/v1/projects" &&
        response.request().method() === "GET",
    );
    reads[index].release();
    const result = await response;
    expect(result.ok()).toBe(true);
    await result.finished();
    if (settled)
      await expect(page.locator(".application")).toHaveAttribute(
        "aria-busy",
        "false",
      );
  }
  const releaseAll = () => reads.forEach((gate) => gate.release());
  return { reads, release, releaseAll };
}

test("UI-06：100 条不同项目回放通知合并刷新，ready 重读和去重均保留", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await page.clock.install();
  await env.open();
  await streams.count(1);
  await expect(page.locator(".application")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await page.clock.pauseAt(new Date());
  const refreshes = await holdProjectRefreshes(page);
  try {
    await streams.push(0, [frame(env.project.id)]);
    await expect(
      page.getByLabel("实时更新连接", { exact: true }),
    ).toContainText("已连接");
    await page.clock.runFor(149);
    expect(refreshes.reads).toHaveLength(0);
    await page.clock.runFor(1);
    await expect.poll(() => refreshes.reads.length).toBe(1);
    await refreshes.release(0);

    const replay = projectChanges(env.project.id);
    await streams.push(0, [replay]);
    await page.clock.runFor(149);
    expect(refreshes.reads).toHaveLength(1);
    await page.clock.runFor(1);
    await expect.poll(() => refreshes.reads.length).toBe(2);
    await refreshes.release(1);
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(2);

    await streams.push(0, [replay]);
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(2);
  } finally {
    refreshes.releaseAll();
  }
});

test("UI-06：目录慢刷新期间的 100 条不同通知最多保留一次尾随刷新", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await page.clock.install();
  await env.open();
  await streams.count(1);
  await expect(page.locator(".application")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await page.clock.pauseAt(new Date());
  const refreshes = await holdProjectRefreshes(page);
  try {
    await streams.push(0, [frame(env.project.id)]);
    await page.clock.runFor(150);
    await expect.poll(() => refreshes.reads.length).toBe(1);
    await expect(page.locator(".application")).toHaveAttribute(
      "aria-busy",
      "true",
    );

    await streams.push(0, [projectChanges(env.project.id, 1, 50)]);
    await page.clock.runFor(150);
    expect(refreshes.reads).toHaveLength(1);
    await streams.push(0, [projectChanges(env.project.id, 51, 50)]);
    await page.clock.runFor(150);
    expect(refreshes.reads).toHaveLength(1);

    await refreshes.release(0, false);
    await expect.poll(() => refreshes.reads.length).toBe(2);
    await refreshes.release(1);
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(2);
  } finally {
    refreshes.releaseAll();
  }
});

test("UI-06：切项目后旧慢刷新完成不再启动尾随，目标项目仍可重读", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  const auth = await page.request.post("/api/v1/auth/refresh");
  expect(auth.ok()).toBe(true);
  const headers = {
    Authorization: `Bearer ${(await auth.json()).access_token}`,
  };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "慢刷新后的目标项目" },
  });
  expect(created.status()).toBe(201);
  const other = await created.json();
  const episode = await page.request.post(
    `/api/v1/projects/${other.id}/episodes`,
    { headers, data: { title: "切换后保留的剧集", episode_no: 1 } },
  );
  expect(episode.status()).toBe(201);
  await page.clock.install();
  await env.open();
  await streams.count(1);
  await expect(page.locator(".application")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await page.clock.pauseAt(new Date());
  const refreshes = await holdProjectRefreshes(page);
  try {
    await streams.push(0, [frame(env.project.id)]);
    await page.clock.runFor(150);
    await expect.poll(() => refreshes.reads.length).toBe(1);
    await streams.push(0, [projectChanges(env.project.id)]);
    await page.clock.runFor(150);

    await chooseProject(page, other);
    await streams.count(2);
    expect(
      ((await streams.snapshot())[0] as { aborted: boolean }).aborted,
    ).toBe(true);
    await refreshes.release(0, false);
    // Workbench's existing project-navigation reconciliation is still required.
    await expect.poll(() => refreshes.reads.length).toBe(2);
    await refreshes.release(1);
    await expect(
      page.getByRole("heading", {
        name: "第 1 集 · 切换后保留的剧集",
        exact: true,
      }),
    ).toBeVisible();
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(2);

    await streams.push(1, [frame(other.id)]);
    await page.clock.runFor(150);
    await expect.poll(() => refreshes.reads.length).toBe(3);
    await refreshes.release(2);
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(3);
  } finally {
    refreshes.releaseAll();
  }
});

test("UI-06：退出后旧慢刷新和待合并项目通知不再启动请求", async ({ page }) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await page.clock.install();
  await env.open();
  await streams.count(1);
  await expect(page.locator(".application")).toHaveAttribute(
    "aria-busy",
    "false",
  );
  await page.clock.pauseAt(new Date());
  const refreshes = await holdProjectRefreshes(page);
  try {
    await streams.push(0, [frame(env.project.id)]);
    await page.clock.runFor(150);
    await expect.poll(() => refreshes.reads.length).toBe(1);
    await streams.push(0, [projectChanges(env.project.id)]);
    await page.clock.runFor(150);
    await signOut(page);
    await expect(
      page.getByRole("button", { name: "开始我的故事", exact: true }),
    ).toBeVisible();
    expect(
      ((await streams.snapshot())[0] as { aborted: boolean }).aborted,
    ).toBe(true);
    refreshes.releaseAll();
    await page.clock.runFor(1000);
    expect(refreshes.reads).toHaveLength(1);
  } finally {
    refreshes.releaseAll();
  }
});

test("UI-06：生产同源代理保持真实 SSE，外部修改沿原 API 更新剧集", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  const env = await mediaFixture(page);
  const auth = await page.request.post("/api/v1/auth/refresh");
  expect(auth.ok()).toBe(true);
  const headers = {
    Authorization: `Bearer ${(await auth.json()).access_token}`,
  };
  await env.open();
  const connection = page.getByLabel("实时更新连接", { exact: true });
  await expect(connection).toContainText("实时更新已连接");
  const read = page.waitForResponse(
    (response) =>
      response.url().includes(`/projects/${env.project.id}/episodes?`) &&
      response.ok(),
  );
  const patched = await page.request.patch(
    `/api/v1/episodes/${env.episodes[0].id}`,
    {
      headers,
      data: { title: "通知到达后的新标题" },
    },
  );
  expect(patched.ok()).toBe(true);
  await read;
  await expect(
    page.getByRole("heading", {
      name: "第 1 集 · 通知到达后的新标题",
      exact: true,
    }),
  ).toBeVisible();
  await expect(connection).toContainText("实时更新已连接");
  await page.screenshot({ path: info.outputPath("ui06-desktop-live.png") });
});

test("UI-06：通知重读权威任务，重复通知不重复行，记录分页仍可用", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  const run: Run = {
    id: crypto.randomUUID(),
    episode_id: env.episodes[0].id,
    workflow_type: "script_storyboard",
    status: "COMPLETED",
    revision: 1,
    pending_review: null,
    error_code: null,
    created_at: "2026-11-15T00:00:00Z",
  };
  let label = "workflow.finished";
  const eventId = crypto.randomUUID();
  await page.route(`**/api/v1/episodes/${env.episodes[0].id}/runs?*`, (route) =>
    route.fulfill({ json: [run] }),
  );
  await page.route(`**/api/v1/runs/${run.id}/events?*`, (route) =>
    route.fulfill({
      json: [
        {
          id: eventId,
          event_type: label,
          created_at: run.created_at,
          payload_json: {},
        },
      ],
    }),
  );
  await env.open();
  await streams.count(1);
  const cursor = `v1:w:${eventId}:1`;
  await streams.push(0, [frame(env.project.id, "stream.ready", cursor)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  const card = page.locator(`#run-${run.id}`);
  await card.getByRole("button", { name: "查看执行记录", exact: true }).click();
  const history = page.getByRole("region", { name: "执行记录", exact: true });
  await expect(history).toContainText(label);
  label = "review.completed";
  run.status = "FAILED";
  const next = crypto.randomUUID();
  const wire = frame(env.project.id, "run.completed", `v1:w:${next}:2`, next);
  await streams.push(0, [wire.slice(0, 18), wire.slice(18), wire]);
  await expect(card.locator(".status")).toHaveText("失败");
  await expect(history).toContainText(label);
  await expect(history.locator("li")).toHaveCount(1);
  await expect(
    card.getByRole("button", { name: "重试任务", exact: true }),
  ).toBeVisible();
});

test("UI-06：断线携带已接收 ID，401 复用会话刷新后续传", async ({ page }) => {
  const streams = await controlledStreams(page, [200, 401, 200]);
  const env = await mediaFixture(page);
  let refreshes = 0;
  page.on("request", (request) => {
    if (new URL(request.url()).pathname === "/api/v1/auth/refresh") refreshes++;
  });
  await env.open();
  await streams.count(1);
  const cursor = `v1:w:${crypto.randomUUID()}:7`;
  await streams.push(0, [frame(env.project.id, "stream.ready", cursor)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  const before = refreshes;
  await streams.end(0);
  await streams.count(3);
  const requests = (await streams.snapshot()) as {
    cursor: string;
    authorized: boolean;
  }[];
  expect(requests.map((row) => row.cursor)).toEqual(["", cursor, cursor]);
  expect(requests.every((row) => row.authorized)).toBe(true);
  expect(refreshes).toBe(before + 1);
  await streams.push(2, [frame(env.project.id, "stream.ready", cursor)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
});

test("UI-06：连续断线按退避重连，中止后不残留重连计时器", async ({ page }) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await env.open();
  await streams.count(1);
  await streams.push(0, [frame(env.project.id)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  await page.clock.install();
  await page.clock.pauseAt(new Date());
  await streams.end(0, true);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "自动重连",
  );
  await page.clock.runFor(999);
  expect((await streams.snapshot()).length).toBe(1);
  await page.clock.runFor(1);
  await streams.count(2);
  await streams.end(1, true);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "自动重连",
  );
  await page.clock.runFor(1999);
  expect((await streams.snapshot()).length).toBe(2);
  await page.clock.runFor(1);
  await streams.count(3);
  await signOut(page);
  await page.clock.runFor(30000);
  expect((await streams.snapshot()).length).toBe(3);
  await expect(
    page.getByRole("button", { name: "开始我的故事", exact: true }),
  ).toBeVisible();
});

test("UI-06：同项目切集共用连接，迟到通知只更新当前剧集", async ({ page }) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await env.open();
  await streams.count(1);
  await streams.push(0, [frame(env.project.id)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  await openDirectory(page);
  await page
    .getByRole("navigation", { name: "剧集列表", exact: true })
    .getByRole("button")
    .filter({ hasText: env.episodes[1].title })
    .click();
  await closeDirectory(page);
  await expect(
    page.getByRole("heading", {
      name: `第 2 集 · ${env.episodes[1].title}`,
      exact: true,
    }),
  ).toBeVisible();
  const id = crypto.randomUUID();
  await streams.push(0, [
    frame(env.project.id, "run.completed", `v1:w:${id}:3`, id),
  ]);
  await expect(
    page.getByRole("heading", {
      name: `第 2 集 · ${env.episodes[1].title}`,
      exact: true,
    }),
  ).toBeVisible();
  expect((await streams.snapshot()).length).toBe(1);
  await expect(
    page.getByRole("heading", {
      name: `第 1 集 · ${env.episodes[0].title}`,
      exact: true,
    }),
  ).toHaveCount(0);
});

test("UI-06：流中撤权清空已显示任务并停止连接", async ({ page }) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await env.open();
  await streams.count(1);
  await streams.push(0, [frame(env.project.id)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  await streams.push(0, [
    'event: stream.error\ndata: {"code":"NOT_FOUND","status":404}\n\n',
  ]);
  await expect(
    page.getByText("当前项目不可访问，请检查权限或重新选择项目。", {
      exact: true,
    }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "项目媒体任务", exact: true }),
  ).toHaveCount(0);
  await expect
    .poll(
      async () =>
        ((await streams.snapshot())[0] as { cancelled: boolean }).cancelled,
    )
    .toBe(true);
  expect((await streams.snapshot()).length).toBe(1);
});

test("UI-06：真实 Viewer 撤权后清除内容并显示不可访问，避免停在加载占位", async ({
  page,
}) => {
  const env = await mediaFixture(page);
  const owner = await page.request.post("/api/v1/auth/refresh");
  expect(owner.ok()).toBe(true);
  const headers = {
    Authorization: `Bearer ${(await owner.json()).access_token}`,
  };
  const email = `events-viewer-${crypto.randomUUID()}@example.com`;
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email,
      password: "events-viewer-password",
      display_name: "只读订阅者",
    },
  });
  expect(registered.status()).toBe(201);
  const viewer = await registered.json();
  const added = await page.request.post(
    `/api/v1/projects/${env.project.id}/members`,
    {
      headers,
      data: { email, role: "viewer" },
    },
  );
  expect(added.status()).toBe(201);
  env.control.role = "viewer";
  await env.open();
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  const removed = await page.request.delete(
    `/api/v1/projects/${env.project.id}/members/${viewer.user.id}`,
    { headers },
  );
  expect(removed.status()).toBe(204);
  await expect(
    page.getByRole("heading", { name: "无法打开此项目", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("region", { name: "项目媒体任务", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("link", { name: "返回我的项目", exact: true }),
  ).toBeVisible();
});

test("UI-06：切项目中止旧连接，新连接和查询只属于目标项目", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  const auth = await page.request.post("/api/v1/auth/refresh");
  expect(auth.ok()).toBe(true);
  const headers = {
    Authorization: `Bearer ${(await auth.json()).access_token}`,
  };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "另一个实时项目" },
  });
  expect(created.status()).toBe(201);
  const other = await created.json();
  const episode = await page.request.post(
    `/api/v1/projects/${other.id}/episodes`,
    { headers, data: { title: "目标项目的剧集", episode_no: 1 } },
  );
  expect(episode.status()).toBe(201);
  await env.open();
  await streams.count(1);
  await streams.push(0, [frame(env.project.id)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  await chooseProject(page, other);
  await streams.count(2);
  await streams.push(1, [frame(other.id)]);
  await expect(
    page.getByRole("heading", {
      name: "第 1 集 · 目标项目的剧集",
      exact: true,
    }),
  ).toBeVisible();
  const requests = (await streams.snapshot()) as {
    project: string;
    aborted: boolean;
  }[];
  expect(requests.map((row) => row.project)).toEqual([
    env.project.id,
    other.id,
  ]);
  expect(requests[0].aborted).toBe(true);
  await expect(
    page.getByRole("heading", {
      name: `第 1 集 · ${env.episodes[0].title}`,
      exact: true,
    }),
  ).toHaveCount(0);
});

test("UI-06：媒体通知沿原任务详情重读，不把事件类型当执行结果", async ({
  page,
}) => {
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  env.terminal("FAILED");
  await env.open();
  await streams.count(1);
  await streams.push(0, [frame(env.project.id)]);
  await expect(page.getByLabel("实时更新连接", { exact: true })).toContainText(
    "已连接",
  );
  const detail = await env.select();
  await expect(detail.getByLabel("媒体任务状态", { exact: true })).toHaveText(
    "服务端状态 FAILED",
  );
  // A completion notification cannot overrule the authoritative failed job.
  const first = crypto.randomUUID();
  const read = page.waitForResponse(
    (response) =>
      new URL(response.url()).pathname.endsWith(
        `/generation-jobs/${env.jobs[0].id}`,
      ) && response.ok(),
  );
  await streams.push(0, [
    frame(env.project.id, "generation.completed", `v1:w:${first}:2`, first),
  ]);
  await read;
  await expect(detail.getByLabel("媒体任务状态", { exact: true })).toHaveText(
    "服务端状态 FAILED",
  );
  env.terminal("SUCCEEDED");
  const next = crypto.randomUUID();
  await streams.push(0, [
    frame(env.project.id, "generation.progress", `v1:w:${next}:3`, next),
  ]);
  await expect(detail.getByLabel("媒体任务状态", { exact: true })).toHaveText(
    "服务端状态 SUCCEEDED",
  );
  expect(env.mutations).toHaveLength(0);
});

test("UI-06：重置后按查询恢复，手机显示连接与可恢复动作", async ({
  page,
}, info) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const streams = await controlledStreams(page);
  const env = await mediaFixture(page);
  await env.open();
  await streams.count(1);
  await streams.push(0, [
    frame(env.project.id, "stream.ready", `v1:w:${crypto.randomUUID()}:8`),
  ]);
  const connection = page.getByLabel("实时更新连接", { exact: true });
  await expect(connection).toContainText("已连接");
  const read = page.waitForResponse(
    (response) =>
      response.url().includes(`/episodes/${env.episodes[0].id}/runs?`) &&
      response.ok(),
  );
  await streams.push(0, [frame(env.project.id, "stream.reset", "")]);
  await read;
  await streams.end(0, true);
  await expect(connection).toContainText("自动重连");
  await expect(
    connection.getByRole("button", { name: "重新连接", exact: true }),
  ).toBeVisible();
  await connection.scrollIntoViewIfNeeded();
  await page.screenshot({ path: info.outputPath("ui06-mobile-recovery.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  await connection
    .getByRole("button", { name: "重新连接", exact: true })
    .click();
  await streams.count(2);
  expect(((await streams.snapshot())[1] as { cursor: string }).cursor).toBe("");
  await streams.push(1, [frame(env.project.id)]);
  await expect(connection).toContainText("已连接");
});
