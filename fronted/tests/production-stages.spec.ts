import { expect, test, type Page } from "@playwright/test";
import { closeDirectory, openDirectory } from "./workspace-helpers";
import { deferred } from "./media-tasks-helpers";
import type { ProductionState } from "../src/features/episodes/production-stages";
import type { Episode } from "../src/types/api";

const secret = "UI09_PRIVATE_PROMPT_CREDENTIAL_STACK";
async function fixture(page: Page, controlled = true) {
  const register = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `ui09-${crypto.randomUUID()}@example.com`,
      password: "stage-navigation-test",
      display_name: "阶段导航验收",
    },
  });
  expect(register.status()).toBe(201);
  const headers = {
    Authorization: `Bearer ${(await register.json()).access_token}`,
  };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "剧集阶段与真实进度" },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const episodes: Episode[] = [];
  for (let i = 1; i <= 2; i++) {
    const response = await page.request.post(
      `/api/v1/projects/${project.id}/episodes`,
      { headers, data: { episode_no: i, title: `阶段第${i}集` } },
    );
    expect(response.status()).toBe(201);
    episodes.push(await response.json());
  }
  const states: ProductionState[] = [];
  for (const episode of episodes) {
    const response = await page.request.get(
      `/api/v1/episodes/${episode.id}/production-state`,
      { headers },
    );
    expect(response.status()).toBe(200);
    states.push(await response.json());
  }
  const control: { status?: number; gate?: Promise<void>; requests: number } = {
    requests: 0,
  };
  if (controlled)
    await page.route("**/api/v1/episodes/*/production-state", async (route) => {
      const index = episodes.findIndex((episode) =>
        route.request().url().includes(episode.id),
      );
      const state = structuredClone(states[index]);
      const status = control.status;
      control.requests++;
      if (control.gate) await control.gate;
      await route.fulfill(
        status
          ? {
              status,
              json: { error: { code: "PRIVATE_ERROR", message: secret } },
            }
          : { json: state },
      );
    });
  const panel = page.getByRole("region", { name: "剧集生产阶段", exact: true });
  const href = (index = 0, query = "view=list") =>
    `/projects/${project.id}/episodes/${episodes[index].id}?${query}`;
  const open = async (query = "view=list") => {
    await page.goto(href(0, query));
    await closeDirectory(page);
    await expect(panel).toBeVisible();
    await expect(
      panel.getByRole("button", { name: "刷新阶段状态", exact: true }),
    ).toBeEnabled();
    return panel;
  };
  const select = async (index: number) => {
    const button = panel
      .getByRole("navigation", { name: "制作阶段" })
      .getByRole("button")
      .nth(index);
    await button.click();
    await expect(button).toHaveAttribute("aria-current", "step");
  };
  const refresh = async () => {
    await panel
      .getByRole("button", { name: "刷新阶段状态", exact: true })
      .click();
    await expect(
      panel.getByRole("button", { name: "刷新阶段状态", exact: true }),
    ).toBeEnabled();
  };
  return {
    headers,
    project,
    episodes,
    states,
    control,
    panel,
    href,
    open,
    select,
    refresh,
  };
}

test("UI-09：真实空剧集七阶段可查看，缺前置提示且导航没有生产写请求", async ({
  page,
}) => {
  const env = await fixture(page, false);
  const writes: string[] = [];
  page.on("request", (request) => {
    if (
      request.url().includes("/api/v1/") &&
      !request.url().includes("/auth/") &&
      !["GET", "HEAD"].includes(request.method())
    )
      writes.push(request.url());
  });
  await env.open();
  await expect(env.panel).toContainText("尚未起稿");
  await expect(env.panel).toContainText("必需镜头已批准 0 / 0");
  for (let i = 0; i < 7; i++) {
    await env.select(i);
    await expect(env.panel).toContainText("查看阶段不会启动生产");
    await expect(env.panel).toContainText("尚无剧本");
  }
  await expect(env.panel).toContainText("预览不代表正式交付");
  expect(writes).toEqual([]);
});

test("UI-09：真实剧本保存待审，批准后刷新读取新的权威阶段", async ({
  page,
}) => {
  const env = await fixture(page, false);
  const prefix = `/api/v1/episodes/${env.episodes[0].id}`;
  const saved = await page.request.post(`${prefix}/scripts`, {
    headers: env.headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "待批准的剧本",
        estimated_duration_ms: 30000,
        summary: "导航读取真实状态",
        characters: ["主角"],
        scenes: [
          { scene_no: 1, title: "森林", action: "主角走进森林", dialogue: [] },
        ],
      },
    },
  });
  expect(saved.status()).toBe(201);
  const script = await saved.json();
  await env.open();
  await expect(env.panel).toContainText("剧本待审核");
  await env.select(0);
  await env.panel
    .getByRole("button", { name: "查看剧本版本", exact: true })
    .click();
  await expect(page).toHaveURL(new RegExp(`node=script%3A${script.id}`));
  expect(
    (
      await page.request.post(`${prefix}/scripts/${script.id}/review`, {
        headers: env.headers,
        data: { decision: "APPROVE" },
      })
    ).ok(),
  ).toBeTruthy();
  await env.refresh();
  await expect(env.panel).toContainText("分镜制作中");
  await expect(
    env.panel.getByRole("navigation").getByRole("button").first(),
  ).toContainText("条件已满足");
});

test("UI-09：必需镜头进度与待审使用服务端数值，探索成功不冒充完成", async ({
  page,
}) => {
  const env = await fixture(page);
  Object.assign(env.states[0], {
    phase: "MEDIA_QA",
    shot_count: 5,
    approved_shot_count: 2,
    shot_counts: { APPROVED: 2, QA_PENDING: 1, NEEDS_REVIEW: 1, FAILED: 1 },
    unbound_task_counts: { SUCCEEDED: 90 },
    task_counts: { SUCCEEDED: 90, FAILED: 1 },
    has_partial_failure: true,
    issues: ["CONTINUITY_PENDING", "HUMAN_REVIEW_REQUIRED"],
  });
  await env.open();
  await env.select(3);
  await expect(env.panel).toContainText("必需镜头已批准 2 / 5");
  await expect(env.panel.getByRole("progressbar")).toHaveAttribute(
    "value",
    "2",
  );
  await expect(env.panel.getByRole("progressbar")).toHaveAttribute("max", "5");
  await expect(env.panel).toContainText("待检查／审核镜头 2");
  await expect(env.panel).toContainText("存在部分失败");
  await expect(env.panel).toContainText("生成任务 90 项");
  await expect(env.panel).toContainText("连续性审查待完成");
});

test("UI-09：失败保留阻塞阶段，未知错误与内部内容不泄漏", async ({ page }) => {
  const env = await fixture(page);
  Object.assign(env.states[0], {
    phase: "FAILED",
    blocked_phase: "STORYBOARDING",
    task_issues: ["RESULT_UNCERTAIN", secret],
    issues: ["STALE_INPUT"],
  });
  await env.open();
  await env.select(2);
  await expect(env.panel).toContainText("生产失败");
  await expect(env.panel).toContainText("阻塞位置：分镜制作中");
  await expect(env.panel).toContainText("输入已失效");
  await expect(env.panel).toContainText("请先核验，避免重复提交");
  await expect(env.panel).not.toContainText(secret);
});

for (const code of [403, 404, 500])
  test(`UI-09：${code} 清空旧状态及入口，安全提示后可重新读取`, async ({
    page,
  }) => {
    const env = await fixture(page);
    await env.open();
    await env.select(0);
    await expect(
      env.panel.getByRole("button", { name: "查看剧本版本", exact: true }),
    ).toBeVisible();
    env.control.status = code;
    await env.refresh();
    await expect(env.panel.getByRole("alert")).toContainText("旧状态已清空");
    await expect(env.panel).not.toContainText(secret);
    await expect(env.panel).not.toContainText("必需镜头已批准");
    await expect(
      env.panel.getByRole("button", { name: "查看剧本版本", exact: true }),
    ).toHaveCount(0);
    env.control.status = undefined;
    await env.refresh();
    await expect(env.panel).toContainText("尚未起稿");
  });

test("UI-09：跨剧集或无效进度回执不能显示为可用状态", async ({ page }) => {
  const env = await fixture(page);
  env.states[0].episode_id = env.episodes[1].id;
  await env.open();
  await expect(env.panel.getByRole("alert")).toBeVisible();
  env.states[0].episode_id = env.episodes[0].id;
  env.states[0].approved_shot_count = 99;
  await env.refresh();
  await expect(env.panel.getByRole("alert")).toBeVisible();
  await expect(env.panel).not.toContainText("99");
});

test("UI-09：键盘与前进后退切换阶段，保留精确任务查询", async ({ page }) => {
  const env = await fixture(page);
  const run = crypto.randomUUID();
  await env.open(`view=list&run=${run}&node=script%3A${crypto.randomUUID()}`);
  const before = new URL(page.url());
  const buttons = env.panel
    .getByRole("navigation", { name: "制作阶段" })
    .getByRole("button");
  await buttons.first().focus();
  await expect(buttons.first()).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(buttons.first()).toHaveAttribute("aria-current", "step");
  await page.keyboard.press("Tab");
  await expect(buttons.nth(1)).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(buttons.nth(1)).toHaveAttribute("aria-current", "step");
  await page.goBack();
  await expect(buttons.first()).toHaveAttribute("aria-current", "step");
  await page.goForward();
  await expect(buttons.nth(1)).toHaveAttribute("aria-current", "step");
  const after = new URL(page.url());
  for (const key of ["run", "node", "view"])
    expect(after.searchParams.get(key)).toBe(before.searchParams.get(key));
});

test("UI-09：查看阶段不重挂载起稿表单，草稿保留", async ({ page }) => {
  const env = await fixture(page);
  await env.open("view=create");
  const inspiration = page.getByLabel("故事灵感", { exact: true });
  await inspiration.fill("尚未提交的角色与故事草稿");
  await inspiration.evaluate((element) =>
    element.setAttribute("data-ui09-mounted", "same-editor"),
  );
  await env.select(2);
  await env.select(5);
  await expect(inspiration).toHaveValue("尚未提交的角色与故事草稿");
  await expect(inspiration).toHaveAttribute("data-ui09-mounted", "same-editor");
  await expect(page).toHaveURL(/view=create/);
});

test("UI-09：切集后旧请求迟到不覆盖新集状态", async ({ page }) => {
  const env = await fixture(page);
  await env.open();
  const gate = deferred();
  env.states[0].phase = "COMPLETED";
  env.control.gate = gate.promise;
  const before = env.control.requests;
  await env.panel
    .getByRole("button", { name: "刷新阶段状态", exact: true })
    .click();
  await expect.poll(() => env.control.requests).toBeGreaterThan(before);
  env.control.gate = undefined;
  try {
    await openDirectory(page);
    await page
      .getByRole("navigation", { name: "剧集列表" })
      .getByRole("button")
      .filter({ hasText: "阶段第2集" })
      .click();
    await closeDirectory(page);
    await expect(page).toHaveURL(new RegExp(env.episodes[1].id));
    await expect(env.panel).toContainText("尚未起稿");
  } finally {
    gate.release();
  }
  await expect(env.panel).not.toContainText("正式成片已完成");
});

test("UI-09：实际时间轴装配数、来源与失效提示，不假称音频完成", async ({
  page,
}) => {
  const env = await fixture(page);
  const timelineId = crypto.randomUUID();
  const timeline = {
    id: timelineId,
    version_no: 3,
    stale: true,
    snapshot: {
      episode_id: env.episodes[0].id,
      storyboard_id: crypto.randomUUID(),
      tracks: [
        { kind: "VOICE", clips: [{}, {}] },
        { kind: "SUBTITLE", clips: [{}, {}, {}] },
      ],
    },
  };
  await page.route(`**/episodes/${env.episodes[0].id}/timelines?*`, (route) =>
    route.fulfill({ json: [timeline] }),
  );
  await env.open();
  await env.select(4);
  const audio = env.panel.getByRole("region", { name: "声音与字幕现状" });
  await expect(audio).toContainText("时间轴 v3：配音片段 2，字幕片段 3");
  await expect(audio).toContainText("已失效或来源分镜已变化");
  await expect(audio).toContainText("不代表所有台词已配音或字幕已批准");
  await audio.getByText("查看来源时间轴", { exact: true }).click();
  await expect(audio).toContainText(timelineId);
});

test("UI-09：声音查询失败独立重试，空时间轴保持待核对", async ({ page }) => {
  const env = await fixture(page);
  let fail = true;
  await page.route(`**/episodes/${env.episodes[0].id}/timelines?*`, (route) =>
    route.fulfill(
      fail
        ? { status: 500, json: { error: { message: secret } } }
        : { json: [] },
    ),
  );
  await env.open();
  await env.select(4);
  await expect(env.panel.getByRole("alert")).toContainText("不能判断完成情况");
  await expect(env.panel).not.toContainText(secret);
  fail = false;
  await env.panel.getByRole("button", { name: "重试声音与字幕" }).click();
  await expect(env.panel).toContainText("尚无时间轴，配音与字幕装配情况待核对");
});

test("UI-09：归档只读阶段仍可查看，桌面与390px无横向溢出", async ({
  page,
}, testInfo) => {
  const env = await fixture(page, false);
  expect(
    (
      await page.request.patch(`/api/v1/episodes/${env.episodes[0].id}`, {
        headers: env.headers,
        data: { archived: true },
      })
    ).ok(),
  ).toBeTruthy();
  await env.open();
  await env.select(6);
  await expect(env.panel).toContainText("当前只读，可查看各阶段");
  await env.panel
    .getByRole("heading", { name: "制作进度", exact: true })
    .scrollIntoViewIfNeeded();
  await expect(
    env.panel.getByRole("heading", { name: "制作进度", exact: true }),
  ).toBeInViewport();
  await page.screenshot({
    path: testInfo.outputPath("ui09-stages-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await env.select(6);
  const exportButton = env.panel
    .getByRole("navigation")
    .getByRole("button")
    .last();
  await expect(exportButton).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.screenshot({
    path: testInfo.outputPath("ui09-stages-mobile.png"),
  });
});

test("UI-09：真实Viewer可读阶段和素材入口，没有创作权限", async ({ page }) => {
  const env = await fixture(page, false);
  const email = `ui09-viewer-${crypto.randomUUID()}@example.com`;
  expect(
    (
      await page.request.post("/api/v1/auth/register", {
        data: {
          email,
          password: "stage-viewer-test",
          display_name: "阶段查看者",
        },
      })
    ).status(),
  ).toBe(201);
  expect(
    (
      await page.request.post(`/api/v1/projects/${env.project.id}/members`, {
        headers: env.headers,
        data: { email, role: "viewer" },
      })
    ).ok(),
  ).toBeTruthy();
  await env.open();
  await env.select(1);
  await expect(env.panel).toContainText("当前只读，可查看各阶段");
  await expect(
    page.getByRole("button", { name: "开始创作", exact: true }),
  ).toHaveCount(0);
  await env.panel.getByRole("link", { name: "查看角色与场景素材" }).click();
  await expect(page).toHaveURL(
    new RegExp(`/projects/${env.project.id}\\?section=assets`),
  );
});

test("UI-09：导出例外未确认不标完成，全部权威条件通过后更新", async ({
  page,
}) => {
  const env = await fixture(page);
  env.states[0].phase = "DELIVERY_QA";
  env.states[0].conditions = [
    { condition: "RENDER_READY", satisfied: true, issues: [] },
    { condition: "DELIVERY_APPROVED", satisfied: true, issues: [] },
    {
      condition: "WARNINGS_CONFIRMED",
      satisfied: false,
      issues: ["WARNINGS_NOT_CONFIRMED"],
    },
  ];
  env.states[0].issues = ["WARNINGS_NOT_CONFIRMED"];
  await env.open();
  await env.select(6);
  await expect(env.panel).toContainText("交付例外尚未确认");
  await expect(
    env.panel.getByRole("navigation").getByRole("button").last(),
  ).toContainText("待审核");
  env.states[0].phase = "COMPLETED_WITH_WARNINGS";
  env.states[0].conditions[2] = {
    condition: "WARNINGS_CONFIRMED",
    satisfied: true,
    issues: [],
  };
  env.states[0].issues = [];
  await env.refresh();
  await expect(env.panel).toContainText("正式成片已完成（例外已确认）");
  await expect(
    env.panel.getByRole("navigation").getByRole("button").last(),
  ).toContainText("正式交付通过");
});
