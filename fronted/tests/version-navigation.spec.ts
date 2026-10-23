import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page } from "@playwright/test";

const content = (title: string) => ({
  title,
  summary: "女孩提灯寻找星光。",
  characters: ["女孩"],
  scenes: [
    { scene_no: 1, title: "森林", action: "女孩沿着小路前行。", dialogue: [] },
  ],
});

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "version-navigation-test",
      display_name: "新稿验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "新稿自动定位" },
    })
  ).json();
  const episode = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 1, title: "第一集" },
    })
  ).json();
  const prefix = "/api/v1/episodes/" + episode.id;
  async function script(expected: number, title: string) {
    const response = await page.request.post(prefix + "/scripts", {
      headers,
      data: { expected_version_no: expected, content: content(title) },
    });
    expect(response.status()).toBe(201);
    return response.json();
  }
  return { headers, project, episode, prefix, script };
}

async function draft(page: Page, title: string) {
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill(title);
  await page.getByLabel("梗概", { exact: true }).fill("新的人工剧本");
  await page.getByLabel("场景 1", { exact: true }).fill("森林");
  await page.getByLabel("场景动作", { exact: true }).fill("主角走进森林。");
}

const detail = (page: Page) =>
  page.getByRole("complementary", { name: "内容与属性" });

test("TXT-05：从旧剧本另存，精确选中 POST 回执而非并发最新稿，画布定位到新节点", async ({
  page,
}) => {
  const { headers, prefix, script } = await setup(page);
  const first = await script(0, "最早的原稿");
  for (let index = 1; index < 8; index++)
    await script(index, "历史稿 " + (index + 1));
  // 已保存视角下，新节点位于初始视口之外，不能依赖初次 fitView。
  expect(
    (
      await page.request.put(prefix + "/canvas/layout", {
        headers,
        data: {
          expected_revision: 0,
          positions: {},
          viewport: { x: 0, y: 0, zoom: 1 },
        },
      })
    ).ok(),
  ).toBeTruthy();
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "最早的原稿" }).click();
  await page.getByRole("button", { name: "编辑并另存新版本" }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("我的精确新稿");
  await page.getByRole("radio", { name: "创作画布", exact: true }).click();
  let createdId = "";
  await page.route("**" + prefix + "/scripts", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    const created = await response.json();
    createdId = created.id;
    await script(9, "另一位编辑的最新稿");
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存为新版本", exact: true }).click();
  await expect(detail(page).locator(".section-heading h3")).toHaveText(
    "剧本 v9",
  );
  await expect(detail(page).locator(".section-heading .status")).toHaveText(
    "待审核",
  );
  await expect(detail(page)).toBeFocused();
  await expect(page.locator(".saved-version-notice")).toContainText(
    "人工剧本已保存为新版本 v9 · 待审核",
  );
  const node = page.locator(
    '.react-flow__node[data-id="script:' + createdId + '"]',
  );
  await expect(node).toHaveClass(/selected/);
  await expect(node).toBeInViewport({ ratio: 0.9 });
  await page.screenshot({
    path: "test-results/version-navigation-desktop.png",
    fullPage: true,
  });
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(page.locator(".version-row.active")).toContainText(
    "我的精确新稿",
  );
  const rows = await (
    await page.request.get(prefix + "/scripts", { headers })
  ).json();
  expect(rows[0]).toMatchObject({ version_no: 10, status: "READY_FOR_REVIEW" });
  expect(
    rows.find((row: { id: string }) => row.id === createdId),
  ).toMatchObject({ version_no: 9, status: "READY_FOR_REVIEW", run_id: null });
  expect(
    rows.find((row: { id: string }) => row.id === first.id).content_json,
  ).toEqual(first.content_json);
  expect(
    await (await page.request.get(prefix + "/runs", { headers })).json(),
  ).toEqual([]);
});

test("TXT-05：手机从零保存自动打开待审核稿，审核后提示同步状态且不溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { headers, prefix } = await setup(page);
  await page.goto("/");
  await openWorkspacePanels(page);
  await draft(page, "手机新稿");
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(detail(page).locator(".section-heading h3")).toHaveText(
    "剧本 v1",
  );
  await expect(detail(page).locator(".section-heading .status")).toHaveText(
    "待审核",
  );
  await expect(detail(page)).toBeFocused();
  await expect(page.locator(".version-row.active")).toContainText("手机新稿");
  await expect(detail(page)).toBeInViewport();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await page.screenshot({
    path: "test-results/version-navigation-mobile.png",
    fullPage: true,
  });
  await page.getByRole("button", { name: "批准该版本", exact: true }).click();
  await expect(detail(page).locator(".section-heading .status")).toHaveText(
    "已批准",
  );
  await expect(page.locator(".saved-version-notice strong")).toHaveText(
    "人工剧本已保存为新版本 v1 · 已批准",
  );
  expect(
    (await (await page.request.get(prefix + "/scripts", { headers })).json())[0]
      .status,
  ).toBe("APPROVED");
});

test("TXT-05：POST 失败保留草稿，成功后刷新失败仍打开已保存稿，重试只 GET 不重复保存", async ({
  page,
}) => {
  const { headers, prefix } = await setup(page);
  await page.goto("/");
  await openWorkspacePanels(page);
  await draft(page, "不能重复保存的新稿");
  let posts = 0;
  let failPost = true;
  let failRefresh = false;
  await page.route("**" + prefix + "/**", async (route) => {
    const request = route.request();
    if (
      request.method() === "POST" &&
      new URL(request.url()).pathname === prefix + "/scripts"
    ) {
      posts++;
      if (failPost)
        return route.fulfill({
          status: 409,
          json: {
            error: { code: "VERSION_CONFLICT", message: "验收用保存冲突" },
          },
        });
      const response = await route.fetch();
      failRefresh = true;
      return route.fulfill({ response });
    }
    if (failRefresh && request.method() === "GET") return route.abort();
    return route.continue();
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("验收用保存冲突");
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "不能重复保存的新稿",
  );
  expect(
    await (await page.request.get(prefix + "/scripts", { headers })).json(),
  ).toEqual([]);
  failPost = false;
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await expect(page.locator(".saved-version-notice")).toContainText(
    "无需再次保存",
  );
  await expect(detail(page).locator(".section-heading h3")).toHaveText(
    "剧本 v1",
  );
  await expect(detail(page).locator(".section-heading .status")).toHaveText(
    "待审核",
  );
  await expect(
    page.getByRole("button", { name: "保存人工剧本", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "保存为新版本", exact: true }),
  ).toHaveCount(0);
  await page.getByRole("button", { name: "重试刷新已保存版本" }).click();
  await expect(
    page.getByRole("button", { name: "重试刷新已保存版本" }),
  ).toBeEnabled();
  expect(posts).toBe(2);
  failRefresh = false;
  await page.getByRole("button", { name: "重试刷新已保存版本" }).click();
  await expect(
    page.getByRole("button", { name: "重试刷新已保存版本" }),
  ).toHaveCount(0);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(page.locator(".version-row.active")).toContainText(
    "不能重复保存的新稿",
  );
  await expect(
    page.locator(".version-row").filter({ hasText: "不能重复保存的新稿" }),
  ).toHaveCount(1);
  expect(posts).toBe(2);
  expect(
    await (await page.request.get(prefix + "/scripts", { headers })).json(),
  ).toHaveLength(1);
});

test("TXT-05：保存响应迟到时不污染已切换的剧集", async ({ page }) => {
  const { headers, project, prefix } = await setup(page);
  const second = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 2, title: "第二集" },
    })
  ).json();
  await page.goto("/");
  await openWorkspacePanels(page);
  await draft(page, "第一集迟到稿");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let reached!: () => void;
  const saved = new Promise<void>((resolve) => {
    reached = resolve;
  });
  await page.route("**" + prefix + "/scripts", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    const response = await route.fetch();
    reached();
    await gate;
    await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存人工剧本", exact: true }).click();
  await saved;
  await expect(
    page.getByRole("button", { name: "保存中…", exact: true }),
  ).toBeDisabled();
  await page.getByRole("button", { name: "02 第二集", exact: false }).click();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 第二集" }),
  ).toBeVisible();
  const delivered = page.waitForResponse(
    (response) =>
      response.url().endsWith(prefix + "/scripts") &&
      response.request().method() === "POST",
  );
  release();
  await delivered;
  await openWorkspacePanels(page);
  await expect(
    page.getByRole("button", { name: "自己写剧本", exact: true }),
  ).toBeVisible();
  await expect(page.locator(".saved-version-notice")).toHaveCount(0);
  await expect(
    detail(page).getByRole("heading", { name: "剧本 v1" }),
  ).toHaveCount(0);
  expect(
    await (
      await page.request.get("/api/v1/episodes/" + second.id + "/scripts", {
        headers,
      })
    ).json(),
  ).toEqual([]);
  expect(
    await (await page.request.get(prefix + "/scripts", { headers })).json(),
  ).toHaveLength(1);
});
