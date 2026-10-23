import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page, type Route } from "@playwright/test";

async function account(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "pages-" + crypto.randomUUID() + "@example.com",
      password: "pagination-test-password",
      display_name: "分页验收",
    },
  });
  expect(response.status()).toBe(201);
  return { Authorization: "Bearer " + (await response.json()).access_token };
}
async function create(
  page: Page,
  path: string,
  headers: Record<string, string>,
  data: unknown,
) {
  const response = await page.request.post("/api/v1" + path, { headers, data });
  expect(response.status(), await response.text()).toBe(201);
  return response.json();
}
const pager = (page: Page, label: string) =>
  page.getByRole("region", { name: label + "分页", exact: true });
const content = (n: number) => ({
  title: "历史剧本" + n,
  summary: "用于真实历史分页验收。",
  characters: ["女孩"],
  scenes: [
    { scene_no: 1, title: "森林", action: "女孩提灯前行。", dialogue: [] },
  ],
});

// 通过真实公开 API 建立超过 100 条数据，不依赖业务库或收费 Provider。
test("TXT-04：真实项目和剧集超过100条、选择保留、新建与设置管理", async ({
  page,
}) => {
  test.setTimeout(180000);
  const headers = await account(page);
  const first = await create(page, "/projects", headers, {
    name: "最早的项目",
  });
  for (let i = 1; i <= 100; i++)
    await create(page, "/projects", headers, { name: "历史项目" + i });
  const project = await create(page, "/projects", headers, {
    name: "分页主项目",
  });
  for (let i = 1; i <= 105; i++)
    await create(page, "/projects/" + project.id + "/episodes", headers, {
      episode_no: i,
      title: "历史剧集" + i,
    });
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 历史剧集1", exact: true }),
  ).toBeVisible();
  await expect(pager(page, "项目")).toContainText("已加载 100 条 · 可继续加载");
  await expect(
    page.getByRole("combobox", { name: "选择项目" }).locator("option"),
  ).toHaveCount(101);
  await pager(page, "项目")
    .getByRole("button", { name: "加载更多项目", exact: true })
    .click();
  await expect(pager(page, "项目")).toContainText("已加载 102 条 · 已全部加载");
  await page.getByRole("combobox", { name: "选择项目" }).selectOption(first.id);
  await page.reload();
  await openWorkspacePanels(page);
  await expect(page.getByRole("combobox", { name: "选择项目" })).toHaveValue(
    first.id,
  );
  await expect(
    page.getByText("创作本 / 最早的项目", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("combobox", { name: "选择项目" })
    .selectOption(project.id);
  await expect(pager(page, "剧集")).toContainText("已加载 100 条 · 可继续加载");
  await expect(
    page.getByRole("navigation", { name: "剧集列表" }).getByRole("button"),
  ).toHaveCount(100);
  await pager(page, "剧集")
    .getByRole("button", { name: "加载更多剧集", exact: true })
    .click();
  await expect(pager(page, "剧集")).toContainText("已加载 105 条 · 已全部加载");
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button")
    .last()
    .click();
  await expect(
    page.getByRole("heading", { name: "第 105 集 · 历史剧集105", exact: true }),
  ).toBeVisible();
  await pager(page, "剧集")
    .getByRole("button", { name: "刷新剧集", exact: true })
    .click();
  await expect(
    page.getByRole("heading", { name: "第 105 集 · 历史剧集105", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.locator(".episode-settings > li")).toHaveCount(105);
  await page.getByRole("button", { name: "＋ 添加一集", exact: true }).click();
  await page.getByLabel("剧集标题", { exact: true }).fill("新增尾页剧集");
  await expect(page.getByLabel("集数", { exact: true })).toHaveValue("106");
  await page.getByRole("button", { name: "创建剧集", exact: true }).click();
  await expect(
    page.getByRole("heading", {
      name: "第 106 集 · 新增尾页剧集",
      exact: true,
    }),
  ).toBeVisible();
});

test("TXT-04：真实剧本与分镜分页、旧稿详情与审核、刷新保留已加载历史", async ({
  page,
}) => {
  test.setTimeout(180000);
  const headers = await account(page);
  const project = await create(page, "/projects", headers, {
    name: "版本分页",
  });
  const episode = await create(
    page,
    "/projects/" + project.id + "/episodes",
    headers,
    { episode_no: 1, title: "历史版本" },
  );
  const prefix = "/episodes/" + episode.id;
  const source = await create(page, prefix + "/scripts", headers, {
    expected_version_no: 0,
    content: content(1),
  });
  expect(
    (
      await page.request.post(
        "/api/v1" + prefix + "/scripts/" + source.id + "/review",
        { headers, data: { decision: "APPROVE" } },
      )
    ).ok(),
  ).toBeTruthy();
  for (let i = 2; i <= 105; i++)
    await create(page, prefix + "/scripts", headers, {
      expected_version_no: i - 1,
      content: content(i),
    });
  for (let i = 1; i <= 105; i++)
    await create(page, prefix + "/storyboards", headers, {
      expected_version_no: i - 1,
      script_version_id: source.id,
      content: {
        shots: [
          {
            shot_no: 1,
            scene_no: 1,
            duration_ms: 3000,
            shot_size: "medium",
            visual_description: "历史分镜" + i,
            characters: ["女孩"],
          },
        ],
      },
    });
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(pager(page, "剧本版本")).toContainText("已加载 100 条");
  await expect(page.locator(".version-row .kind-dot.script")).toHaveCount(100);
  await expect(page.locator(".version-row .kind-dot.storyboard")).toHaveCount(
    100,
  );
  await pager(page, "剧本版本")
    .getByRole("button", { name: "加载更多剧本版本", exact: true })
    .click();
  await pager(page, "分镜版本")
    .getByRole("button", { name: "加载更多分镜版本", exact: true })
    .click();
  await expect(pager(page, "剧本版本")).toContainText(
    "已加载 105 条 · 已全部加载",
  );
  await expect(pager(page, "分镜版本")).toContainText(
    "已加载 105 条 · 已全部加载",
  );
  await page
    .locator(".version-row")
    .filter({ has: page.getByText("历史剧本1", { exact: true }) })
    .click();
  await expect(page.locator(".inspector")).toContainText(
    "用于真实历史分页验收。",
  );
  await expect(
    page.getByRole("button", { name: "从此版本生成分镜", exact: true }),
  ).toBeEnabled();
  await page
    .locator(".version-row")
    .filter({ has: page.locator(".kind-dot.storyboard") })
    .filter({ has: page.getByText("版本 1", { exact: true }) })
    .click();
  await expect(page.locator(".inspector")).toContainText("历史分镜1");
  await page.getByRole("button", { name: "批准该版本", exact: true }).click();
  await expect(page.locator(".inspector .section-heading .status")).toHaveText(
    "已批准",
  );
  await page.getByRole("button", { name: "刷新内容", exact: true }).click();
  await expect(pager(page, "剧本版本")).toContainText(
    "已加载 105 条 · 已全部加载",
  );
  await expect(page.locator(".inspector")).toContainText("历史分镜1");
  // 保存可见视角，避免大图 fitView 把历史节点放到视口外。
  expect(
    (
      await page.request.put("/api/v1" + prefix + "/canvas/layout", {
        headers,
        data: {
          expected_revision: 0,
          positions: {},
          viewport: { x: -280, y: 0, zoom: 1 },
        },
      })
    ).ok(),
  ).toBeTruthy();
  // 重新进入只加载首批，再从完整画布点选旧稿，必须读取精确版本而非提示调用 API。
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(pager(page, "剧本版本")).toContainText("已加载 100 条");
  await page.getByRole("radio", { name: "创作画布", exact: true }).click();
  await page
    .locator(".react-flow__node")
    .filter({ has: page.getByText("历史剧本1", { exact: true }) })
    .click();
  await expect(page.locator(".inspector")).toContainText(
    "用于真实历史分页验收。",
  );
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(pager(page, "剧本版本")).toContainText(
    "已加载 105 条 · 已全部加载",
  );
});

// 大量任务／事件采用浏览器网络夹具；真实存储排序与 offset 契约由后端专项测试覆盖。
test("TXT-04：任务与事件追加、整页边界、失败重试、防重入、键盘和移动端", async ({
  page,
}, testInfo) => {
  const headers = await account(page);
  const project = await create(page, "/projects", headers, {
    name: "任务分页",
  });
  const episode = await create(
    page,
    "/projects/" + project.id + "/episodes",
    headers,
    { episode_no: 1, title: "任务与事件" },
  );
  const runs = Array.from({ length: 105 }, (_, i) => ({
    id: crypto.randomUUID(),
    status: "FAILED",
    revision: i,
    pending_review: null,
    error_code: null,
    created_at: new Date(2026, 0, 1, 0, i).toISOString(),
  })).reverse();
  runs[0].status = "RUNNING"; // 验证自动轮询不会把已展开历史截回首批。
  const events = Array.from({ length: 200 }, (_, i) => ({
    id: crypto.randomUUID(),
    event_type: "记录-" + (i + 1),
    created_at: new Date(2026, 0, 1, 0, i).toISOString(),
    payload_json: { index: i + 1 },
  }));
  const slices = async (route: Route, rows: unknown[]) => {
    const url = new URL(route.request().url());
    const start = Number(url.searchParams.get("offset"));
    const size = Number(url.searchParams.get("limit"));
    await route.fulfill({ json: rows.slice(start, start + size) });
  };
  await page.route("**/episodes/" + episode.id + "/runs?*", (route) =>
    slices(route, runs),
  );
  let fail = true;
  let appendRequests = 0;
  await page.route("**/runs/*/events?*", async (route) => {
    const offset = Number(
      new URL(route.request().url()).searchParams.get("offset"),
    );
    if (offset === 100) {
      appendRequests++;
      if (fail) {
        fail = false;
        await route.abort();
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 300));
    }
    await slices(route, events);
  });
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(page.locator(".task")).toHaveCount(100);
  await pager(page, "任务")
    .getByRole("button", { name: "加载更多任务", exact: true })
    .click();
  await expect(page.locator(".task")).toHaveCount(105);
  await page.waitForResponse(
    (response) =>
      response
        .url()
        .includes("/episodes/" + episode.id + "/runs?limit=100&offset=100"),
    { timeout: 10000 },
  );
  await expect(page.locator(".task")).toHaveCount(105);
  await pager(page, "任务")
    .getByRole("button", { name: "刷新任务", exact: true })
    .click();
  await expect(pager(page, "任务")).toContainText("已加载 105 条 · 已全部加载");
  await page
    .locator(".task")
    .last()
    .getByRole("button", { name: "查看执行记录" })
    .click();
  await expect(pager(page, "事件")).toContainText("已加载 100 条 · 可继续加载");
  await pager(page, "事件")
    .getByRole("button", { name: "加载更多事件", exact: true })
    .click();
  await expect(pager(page, "事件").getByRole("alert")).toContainText(
    "连接失败",
  );
  await expect(page.locator(".event-panel li")).toHaveCount(100);
  const selector = page.getByRole("combobox", { name: "事件每次追加条数" });
  await selector.focus();
  await page.keyboard.press("Space");
  await expect(
    page.getByRole("option", { name: "每次追加 100 条", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowUp");
  await expect(
    page.getByRole("option", { name: "每次追加 50 条", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(selector).toBeFocused();
  await expect(selector).toContainText("50");
  const retry = pager(page, "事件").getByRole("button", {
    name: "重试加载事件",
    exact: true,
  });
  await retry.evaluate((button: HTMLButtonElement) => {
    button.click();
    button.click();
  });
  await expect(page.locator(".event-panel li")).toHaveCount(150);
  expect(appendRequests).toBe(2);
  await pager(page, "事件").screenshot({
    path: testInfo.outputPath("pagination-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await pager(page, "事件").scrollIntoViewIfNeeded();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await pager(page, "事件").screenshot({
    path: testInfo.outputPath("pagination-mobile.png"),
  });
  await pager(page, "事件")
    .getByRole("button", { name: "加载更多事件", exact: true })
    .click();
  await expect(page.locator(".event-panel li")).toHaveCount(200);
  await expect(pager(page, "事件")).toContainText("可继续加载");
  await pager(page, "事件")
    .getByRole("button", { name: "加载更多事件", exact: true })
    .click();
  await expect(pager(page, "事件")).toContainText("已加载 200 条 · 已全部加载");
  await expect(page.locator(".event-panel li strong").first()).toHaveText(
    "记录-1",
  );
  await expect(page.locator(".event-panel li strong").last()).toHaveText(
    "记录-200",
  );
});

test("TXT-04：切换任务丢弃旧事件响应、空列表和首批失败重试", async ({
  page,
}) => {
  const headers = await account(page);
  const project = await create(page, "/projects", headers, {
    name: "请求隔离",
  });
  const episode = await create(
    page,
    "/projects/" + project.id + "/episodes",
    headers,
    { episode_no: 1, title: "隔离剧集" },
  );
  const runs = ["旧任务", "新任务"].map((name, i) => ({
    id: crypto.randomUUID(),
    status: "FAILED",
    revision: i,
    pending_review: null,
    error_code: name,
    created_at: new Date().toISOString(),
  }));
  await page.route("**/episodes/" + episode.id + "/runs?*", (route) =>
    route.fulfill({ json: runs }),
  );
  let release: () => void = () => {};
  const delayed = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started = false;
  await page.route("**/runs/" + runs[0].id + "/events?*", async (route) => {
    started = true;
    await delayed;
    await route
      .fulfill({
        json: [
          {
            id: "old-event",
            event_type: "不应显示的旧事件",
            created_at: new Date().toISOString(),
            payload_json: {},
          },
        ],
      })
      .catch(() => {});
  });
  let fail = true;
  await page.route("**/runs/" + runs[1].id + "/events?*", (route) =>
    fail ? route.abort() : route.fulfill({ json: [] }),
  );
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await page
    .locator(".task")
    .first()
    .getByRole("button", { name: "查看执行记录" })
    .click();
  await expect.poll(() => started).toBeTruthy();
  await page
    .locator(".task")
    .last()
    .getByRole("button", { name: "查看执行记录" })
    .click();
  await expect(pager(page, "事件").getByRole("alert")).toBeVisible();
  release();
  fail = false;
  await pager(page, "事件")
    .getByRole("button", { name: "重试加载事件", exact: true })
    .click();
  await expect(pager(page, "事件")).toContainText("已加载 0 条 · 已全部加载");
  await expect(page.getByText("暂无执行记录", { exact: true })).toBeVisible();
  await expect(
    page.getByRole("region", { name: "执行记录", exact: true }),
  ).toContainText(runs[1].id);
  await expect(page.getByText("不应显示的旧事件", { exact: true })).toHaveCount(
    0,
  );
});

test("TXT-04：追加去重仍按原始offset推进，切换剧集取消迟到的历史响应", async ({
  page,
}) => {
  const headers = await account(page);
  const project = await create(page, "/projects", headers, {
    name: "游标与隔离",
  });
  const a = await create(
    page,
    "/projects/" + project.id + "/episodes",
    headers,
    { episode_no: 1, title: "有历史任务" },
  );
  await create(page, "/projects/" + project.id + "/episodes", headers, {
    episode_no: 2,
    title: "空白剧集",
  });
  const rows = Array.from({ length: 205 }, (_, i) => ({
    id: crypto.randomUUID(),
    status: "SUCCEEDED",
    revision: i,
    pending_review: null,
    error_code: null,
    created_at: new Date().toISOString(),
  }));
  const offsets: number[] = [];
  let delay = false;
  let started = false;
  let release: () => void = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**/episodes/" + a.id + "/runs?*", async (route) => {
    const offset = Number(
      new URL(route.request().url()).searchParams.get("offset"),
    );
    offsets.push(offset);
    if (delay && offset === 100) {
      started = true;
      await pending;
    }
    // 模拟追加期间新增记录造成页边界重复，不允许重复行或用去重后199作为游标。
    const result =
      offset === 0
        ? rows.slice(0, 100)
        : offset === 100
          ? [rows[99], ...rows.slice(100, 199)]
          : offset === 200
            ? rows.slice(199)
            : [];
    await route.fulfill({ json: result }).catch(() => {});
  });
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await pager(page, "任务")
    .getByRole("button", { name: "加载更多任务", exact: true })
    .click();
  await expect(page.locator(".task")).toHaveCount(199);
  await pager(page, "任务")
    .getByRole("button", { name: "加载更多任务", exact: true })
    .click();
  await expect(page.locator(".task")).toHaveCount(205);
  expect(offsets).toContain(200);
  expect(offsets).not.toContain(199);
  await expect(pager(page, "任务")).toContainText("已加载 205 条 · 已全部加载");
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(page.locator(".task")).toHaveCount(100);
  delay = true;
  await pager(page, "任务")
    .getByRole("button", { name: "加载更多任务", exact: true })
    .click();
  await expect.poll(() => started).toBeTruthy();
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button", { name: /空白剧集/ })
    .click();
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  release();
  await expect(pager(page, "任务")).toContainText("已加载 0 条 · 已全部加载");
  await expect(page.locator(".task")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 空白剧集", exact: true }),
  ).toBeVisible();
});
