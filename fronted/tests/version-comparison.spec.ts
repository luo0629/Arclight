import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page } from "@playwright/test";

const scriptContent = (title: string) => ({
  title,
  summary: "女孩提灯寻找雨夜里的星光。",
  characters: ["女孩", "猫"],
  scenes: [
    {
      scene_no: 1,
      title: "雨夜小巷",
      action: "女孩撑伞走进小巷。",
      dialogue: [
        { speaker: "女孩", text: "你也在找星星吗？" },
        { speaker: "猫", text: "它藏在灯火里。" },
      ],
    },
  ],
});
const boardContent = (description: string, subtitle = "") => ({
  shots: [
    {
      shot_no: 1,
      scene_no: 1,
      duration_ms: 3000,
      shot_size: "medium",
      visual_description: description,
      characters: ["女孩"],
      camera_movement: "static",
      subtitle,
    },
  ],
});

async function setup(page: Page) {
  const email = crypto.randomUUID() + "@example.com";
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email,
      password: "version-comparison-test",
      display_name: "小夏",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "雨夜来信 · 创作手帐" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "会说话的猫", target_duration_ms: 30000 },
    })
  ).json();
  const prefix = `/api/v1/episodes/${episode.id}`;
  async function script(expected: number, title: string, base?: string) {
    const response = await page.request.post(prefix + "/scripts", {
      headers,
      data: {
        expected_version_no: expected,
        content: scriptContent(title),
        ...(base ? { base_version_id: base } : {}),
      },
    });
    expect(response.status()).toBe(201);
    return response.json();
  }
  async function approve(row: { id: string }, kind = "scripts") {
    const response = await page.request.post(
      `${prefix}/${kind}/${row.id}/review`,
      { headers, data: { decision: "APPROVE" } },
    );
    expect(response.status()).toBe(200);
  }
  async function board(
    expected: number,
    scriptId: string,
    description: string,
    base?: string,
  ) {
    const response = await page.request.post(prefix + "/storyboards", {
      headers,
      data: {
        expected_version_no: expected,
        script_version_id: scriptId,
        content: boardContent(description),
        ...(base ? { base_version_id: base } : {}),
      },
    });
    expect(response.status()).toBe(201);
    return response.json();
  }
  return {
    session,
    email,
    headers,
    project,
    episode,
    prefix,
    script,
    approve,
    board,
  };
}
async function openVersion(page: Page, text: string) {
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: text }).click();
  await page.getByRole("button", { name: "查看版本差异", exact: true }).click();
  await expect(page.getByRole("dialog")).toBeVisible();
}
async function selectBase(page: Page, label: string) {
  await page.getByRole("combobox", { name: "基准版本", exact: true }).click();
  await page.getByRole("option", { name: label, exact: true }).click();
}
// 各页面截图使用真实公开API和隔离Mock环境；不包含用户素材或生产数据。
test("TXT-07：人工另存记录精确旧来源，字段新增修改删除、草稿隔离、焦点与桌面差异", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1100 });
  const env = await setup(page);
  const first = await env.script(0, "雨夜初稿");
  await env.script(1, "另一个已保存版本", first.id);
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "雨夜初稿" }).click();
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("雨夜终稿");
  await page
    .getByLabel("梗概", { exact: true })
    .fill("女孩和猫终于找到了星光。");
  await page
    .getByRole("button", { name: "移除对白", exact: true })
    .last()
    .click();
  let submittedBase = "";
  await page.route("**" + env.prefix + "/scripts", async (route) => {
    if (route.request().method() === "POST")
      submittedBase = route.request().postDataJSON().base_version_id;
    await route.continue();
  });
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("剧本 v3");
  expect(submittedBase).toBe(first.id);
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page
    .getByLabel("剧本标题", { exact: true })
    .fill("不应进入对比的未保存草稿");
  const trigger = page.getByRole("button", {
    name: "查看版本差异",
    exact: true,
  });
  await trigger.click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("修改人：小夏");
  await expect(dialog).toContainText("编辑来源：同类 v1");
  await expect(dialog).toContainText("版本创建时间");
  await expect(dialog).toContainText("雨夜终稿");
  await expect(dialog).not.toContainText("不应进入对比的未保存草稿");
  await expect(
    dialog.getByRole("region", { name: "标题", exact: true }),
  ).toContainText("另一个已保存版本");
  await selectBase(page, "v1");
  await expect(
    dialog.getByRole("region", {
      name: `场景 1 / 对白 ${first.content_json.scenes[0].dialogue[1].line_id} / 内容`,
      exact: true,
    }),
  ).toContainText("删除");
  await expect(
    dialog.getByRole("region", { name: "标题", exact: true }),
  ).toContainText("雨夜初稿");
  await dialog.evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({ path: "test-results/pages-comparison-desktop.png" });
  await dialog
    .getByRole("region", { name: "标题", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/pages-comparison-fields-desktop.png",
  });
  await page.keyboard.press("Escape");
  await expect(dialog).not.toBeVisible();
  await expect(trigger).toBeFocused();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "不应进入对比的未保存草稿",
  );
});

test("TXT-07：分镜精确引用、数值与空文本、首版和相同内容，390px无溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const env = await setup(page);
  const first = await env.script(0, "剧本来源一");
  const second = await env.script(1, "剧本来源二");
  await env.approve(first);
  await env.approve(second);
  const board1 = await env.board(0, first.id, "女孩撑伞站在巷口。");
  const board2 = await env.board(1, second.id, "猫从屋檐探出头。", board1.id);
  await page.goto("/");
  await openWorkspacePanels(page);
  await openVersion(page, "分镜版本 2");
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText(`引用剧本：v2 · ${second.id}`);
  await expect(dialog).toContainText(`引用剧本：v1 · ${first.id}`);
  await expect(dialog).toContainText("1 个镜头");
  await expect(
    dialog.getByRole("region", { name: "镜头 1 / 画面描述", exact: true }),
  ).toContainText("猫从屋檐探出头。");
  await dialog.getByRole("button", { name: "显示全部字段" }).click();
  await expect(
    dialog.getByRole("region", { name: "镜头 1 / 字幕", exact: true }),
  ).toContainText("（空文本）");
  await expect(
    dialog.getByRole("region", { name: "镜头 1 / 时长（毫秒）", exact: true }),
  ).toContainText("3000");
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  expect(
    await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth),
  ).toBeTruthy();
  await dialog.evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({ path: "test-results/pages-comparison-mobile.png" });
  await dialog
    .getByRole("region", { name: "镜头 1 / 画面描述", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: "test-results/pages-comparison-fields-mobile.png",
  });
  await selectBase(page, "v2（与自身比较）");
  await expect(dialog).toContainText("两个版本内容相同");
  await page.keyboard.press("Escape");
  await openVersion(page, "分镜版本 1");
  await expect(page.getByRole("dialog")).toContainText("这是首个版本");
  await expect(page.getByRole("dialog")).toContainText("新增");
  expect(board2.script_version_id).toBe(second.id);
});

test("TXT-07：跨100个历史版本追加和读取失败重试，基准保持精确选择", async ({
  page,
}) => {
  const env = await setup(page);
  let previous = "";
  for (let index = 0; index < 102; index++) {
    const row = await env.script(index, `历史正文 ${index + 1}`, previous);
    previous = row.id;
  }
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "历史正文 102" })
    .click();
  let failComparison = true;
  await page.route("**/comparison*", async (route) => {
    if (failComparison) {
      await route.fulfill({
        status: 503,
        json: {
          error: { code: "TEST_UNAVAILABLE", message: "对比暂时不可用" },
        },
      });
    } else await route.continue();
  });
  await page.getByRole("button", { name: "查看版本差异" }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText("对比暂时不可用");
  failComparison = false;
  await dialog.getByRole("button", { name: "重试读取对比" }).click();
  await expect(
    dialog.getByRole("region", { name: "标题", exact: true }),
  ).toContainText("历史正文 101");
  await dialog
    .getByRole("button", { name: "加载更多对比版本", exact: true })
    .click();
  await expect(
    dialog.getByRole("region", { name: "对比版本分页" }),
  ).toContainText("已加载 102 条");
  await selectBase(page, "v1");
  await expect(
    dialog.getByRole("region", { name: "标题", exact: true }),
  ).toContainText("历史正文 1");
  await dialog
    .getByRole("button", { name: "刷新对比版本", exact: true })
    .click();
  await expect(
    dialog.getByRole("combobox", { name: "基准版本", exact: true }),
  ).toContainText("v1");
});

test("TXT-07：选择器列表失败可重试，切换基准丢弃迟到对比响应", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await env.script(0, "旧版本的正文");
  await env.script(1, "中间版本的正文");
  const third = await env.script(2, "当前版本的正文", first.id);
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "当前版本的正文" })
    .click();
  let failList = true;
  await page.route("**" + env.prefix + "/scripts?*", async (route) => {
    if (failList) {
      await route.fulfill({
        status: 503,
        json: { error: { message: "历史列表暂不可读" } },
      });
    } else await route.continue();
  });
  await page.getByRole("button", { name: "查看版本差异" }).click();
  const dialog = page.getByRole("dialog");
  const pagination = dialog.getByRole("region", { name: "对比版本分页" });
  await expect(pagination).toContainText("历史列表暂不可读");
  failList = false;
  await pagination.getByRole("button", { name: "重试加载对比版本" }).click();
  await expect(pagination).toContainText("已加载 3 条");
  const oldData = await (
    await page.request.get(
      `${env.prefix}/scripts/${third.id}/comparison?base_version_id=${first.id}`,
      { headers: env.headers },
    )
  ).json();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const requested = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(
    `**/comparison?base_version_id=${first.id}`,
    async (route) => {
      started();
      await gate;
      await route.fulfill({ json: oldData }).catch(() => {});
    },
  );
  try {
    await selectBase(page, "v1");
    await requested;
    await selectBase(page, "v3（与自身比较）");
    await expect(dialog).toContainText("两个版本内容相同");
  } finally {
    release();
  }
  await expect(
    dialog.getByRole("combobox", { name: "基准版本", exact: true }),
  ).toContainText("v3");
  await expect(dialog).toContainText("两个版本内容相同");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "查看版本差异" }).click();
  await expect(
    page.getByRole("dialog").getByRole("region", { name: "标题", exact: true }),
  ).toContainText("中间版本的正文");
});

test("TXT-07：归档项目的Viewer可只读对比，不出现编辑及审核入口", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await env.script(0, "归档前的原稿");
  await env.script(1, "归档前的终稿", first.id);
  const email = crypto.randomUUID() + "@example.com";
  const viewer = await page.request.post("/api/v1/auth/register", {
    data: {
      email,
      password: "version-comparison-viewer",
      display_name: "只读伙伴",
    },
  });
  expect(viewer.status()).toBe(201);
  expect(
    (
      await page.request.post(`/api/v1/projects/${env.project.id}/members`, {
        headers: env.headers,
        data: { email, role: "viewer" },
      })
    ).status(),
  ).toBe(201);
  expect(
    (
      await page.request.patch(`/api/v1/projects/${env.project.id}`, {
        headers: env.headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.goto("/");
  await openWorkspacePanels(page);
  await openVersion(page, "归档前的终稿");
  await expect(page.getByRole("dialog")).toContainText("归档前的原稿");
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "编辑并另存新版本" }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "批准该版本", exact: true }),
  ).toHaveCount(0);
});

test("TXT-07：当前各页面真实截图，系统生成作者与直接下游可读", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "登录创作手帐" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/pages-login-desktop.png",
    fullPage: true,
  });
  const env = await setup(page);
  const first = await env.script(0, "雨夜的第一封来信");
  await env.approve(first);
  await env.board(0, first.id, "雨夜，女孩撑着伞，一只猫坐在窗台。");
  await env.script(1, "在灯火里找到星光", first.id);
  const run = await page.request.post(env.prefix + "/storyboard-runs", {
    data: { script_version_id: first.id },
    headers: { ...env.headers, "Idempotency-Key": crypto.randomUUID() },
  });
  expect(run.status()).toBe(202);
  const runId = (await run.json()).run_id;
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get(`/api/v1/runs/${runId}`, {
              headers: env.headers,
            })
          ).json()
        ).status,
    )
    .toBe("WAITING_REVIEW");
  await page.goto("/");
  await openWorkspacePanels(page);
  await expect(page.locator(".react-flow__node").first()).toBeVisible();
  await page.screenshot({
    path: "test-results/pages-canvas-desktop.png",
    fullPage: true,
  });
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "在灯火里找到星光" })
    .click();
  await expect(
    page.getByRole("complementary", { name: "内容与属性" }),
  ).toContainText("剧本 v2");
  await page.screenshot({
    path: "test-results/pages-versions-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "查看版本差异" }).click();
  await expect(page.getByRole("dialog")).toContainText("2 个分镜版本");
  await expect(page.getByRole("dialog")).toContainText("1 个精确引用导演任务");
  await page.keyboard.press("Escape");
  await page.locator(".version-row").filter({ hasText: "分镜版本 2" }).click();
  await page.getByRole("button", { name: "查看版本差异" }).click();
  await expect(page.getByRole("dialog")).toContainText(
    "生成：系统工作流；发起人：小夏",
  );
  await page.keyboard.press("Escape");
  await page.getByRole("radio", { name: "任务与审核", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "创作任务与审核" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/pages-tasks-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "给故事找几个好伙伴" }),
  ).toBeVisible();
  await page.screenshot({
    path: "test-results/pages-members-desktop.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("视觉风格说明")).toBeVisible();
  await page.screenshot({
    path: "test-results/pages-settings-desktop.png",
    fullPage: true,
  });
});
