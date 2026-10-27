import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page, aliases = false) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `matching-${crypto.randomUUID()}@example.com`,
      password: "matching-test-password",
      display_name: "角色确认验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "角色确认项目" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "角色来源" },
    })
  ).json();
  const entries = `/api/v1/projects/${project.id}/story-entries`;
  const prefix = `/api/v1/episodes/${episode.id}`;
  let entry: {
    id: string;
    revision: number;
    name: string;
    aliases: string[];
  } | null = null;
  if (aliases)
    entry = await (
      await page.request.post(entries, {
        headers,
        data: { kind: "CHARACTER", name: "阿青", aliases: ["小青"] },
      })
    ).json();
  const created = await page.request.post(prefix + "/scripts", {
    headers,
    data: {
      expected_version_no: 0,
      content: {
        title: "角色来源稿",
        summary: "阿青寻找失主",
        estimated_duration_ms: 30000,
        characters: [aliases ? "小青" : "阿青"],
        scenes: [
          { scene_no: 1, title: "雨夜", action: "阿青走入书店", dialogue: [] },
        ],
      },
    },
  });
  expect(created.status()).toBe(201);
  const source = await created.json();
  const url = `${prefix}/scripts/${source.id}/character-matches`;
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await open(page);
  return { headers, project, episode, entries, source, prefix, url, entry };
}
async function open(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page.locator(".version-row").filter({ hasText: "角色来源稿" }).click();
  await page.getByText("角色匹配与确认", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "刷新角色匹配", exact: true }),
  ).toBeEnabled();
}
async function choose(page: Page, name: string, label: RegExp) {
  await page
    .getByRole("combobox", { name: "处理 " + name, exact: true })
    .click();
  await page.getByRole("option", { name: label }).click();
}

test("unknown role requires an explicit dialog and persists an exact-source confirmation", async ({
  page,
}, testInfo) => {
  const env = await setup(page);
  await expect(
    page.getByText("未匹配到角色，请选择已有角色，或确认新增。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(
    await (
      await page.request.get(env.entries, { headers: env.headers })
    ).json(),
  ).toEqual([]);
  await choose(page, "阿青", /^新增故事角色：阿青$/);
  await page
    .getByRole("button", { name: "确认角色 阿青", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "确认新增故事角色",
    exact: true,
  });
  await expect(dialog).toBeVisible();
  await page.screenshot({
    path: testInfo.outputPath("character-matching-dialog-desktop.png"),
    fullPage: true,
  });
  expect(
    await (
      await page.request.get(env.entries, { headers: env.headers })
    ).json(),
  ).toEqual([]);
  await dialog.getByRole("button", { name: "返回核对", exact: true }).click();
  await expect(dialog).toHaveCount(0);
  await page
    .getByRole("button", { name: "确认角色 阿青", exact: true })
    .click();
  await dialog
    .getByRole("button", { name: "确认新增角色", exact: true })
    .click();
  await expect(
    page.getByText("已确认新增：阿青 · 修订 0", { exact: true }),
  ).toBeVisible();
  const preview = await (
    await page.request.get(env.url, { headers: env.headers })
  ).json();
  expect(preview.candidates[0].confirmation.version_id).toBe(env.source.id);
  expect(
    await (
      await page.request.get(env.entries, { headers: env.headers })
    ).json(),
  ).toHaveLength(1);
  await page.screenshot({
    path: testInfo.outputPath("character-matching-desktop.png"),
    fullPage: true,
  });
  await page.reload();
  await open(page);
  await expect(
    page.getByText("已确认新增：阿青 · 修订 0", { exact: true }),
  ).toBeVisible();
  await page.setViewportSize({ width: 390, height: 844 });
  await page
    .getByRole("region", { name: "角色确认 阿青", exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: testInfo.outputPath("character-matching-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  const sources = await (
    await page.request.get(env.prefix + "/scripts", { headers: env.headers })
  ).json();
  expect(sources[0].content_json).toEqual(env.source.content_json);
  expect(sources[0].status).toBe("READY_FOR_REVIEW");
});

test("aliases reuse stable identities; response loss retries do not create another role", async ({
  page,
}) => {
  const env = await setup(page, true);
  await expect(
    page.getByText("名称或别名匹配：阿青", { exact: true }),
  ).toBeVisible();
  let first = true;
  await page.route("**" + env.url, async (route) => {
    if (route.request().method() === "POST" && first) {
      first = false;
      const response = await route.fetch();
      expect(response.status()).toBe(200);
      await route.abort("failed");
    } else await route.continue();
  });
  await page
    .getByRole("button", { name: "确认角色 小青", exact: true })
    .click();
  await expect(page.getByRole("alert")).toContainText("网络连接失败");
  await page
    .getByRole("button", { name: "确认角色 小青", exact: true })
    .click();
  await expect(
    page.getByText("已确认复用：阿青 · 修订 0", { exact: true }),
  ).toBeVisible();
  const result = await (
    await page.request.get(env.url, { headers: env.headers })
  ).json();
  expect(result.candidates[0].confirmation.entry.id).toBe(env.entry!.id);
  expect(
    await (
      await page.request.get(env.entries, { headers: env.headers })
    ).json(),
  ).toHaveLength(1);
});

test("stale matching preserves the choice and refresh exposes the new alias", async ({
  page,
}) => {
  const env = await setup(page);
  await choose(page, "阿青", /^新增故事角色：阿青$/);
  const response = await page.request.post(env.entries, {
    headers: env.headers,
    data: { kind: "CHARACTER", name: "青姐", aliases: ["阿青"] },
  });
  expect(response.status()).toBe(201);
  await page
    .getByRole("button", { name: "确认角色 阿青", exact: true })
    .click();
  const dialog = page.getByRole("dialog", {
    name: "确认新增故事角色",
    exact: true,
  });
  await dialog
    .getByRole("button", { name: "确认新增角色", exact: true })
    .click();
  await expect(dialog.getByRole("alert")).toContainText("角色设定已变化");
  await dialog.getByRole("button", { name: "返回核对", exact: true }).click();
  await expect(
    page.getByRole("combobox", { name: "处理 阿青", exact: true }),
  ).toContainText("新增故事角色：阿青");
  await page.getByRole("button", { name: "刷新角色匹配", exact: true }).click();
  await expect(
    page.getByText("名称或别名匹配：青姐", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "确认角色 阿青", exact: true }),
  ).toBeDisabled();
  await choose(page, "阿青", /^青姐 · /);
  await page
    .getByRole("button", { name: "确认角色 阿青", exact: true })
    .click();
  await expect(
    page.getByText("已确认复用：青姐 · 修订 0", { exact: true }),
  ).toBeVisible();
});

test("story bible persists aliases and blocks normalized name collisions", async ({
  page,
}) => {
  const env = await setup(page, true);
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  await page.getByRole("button", { name: "角色 · 阿青", exact: true }).click();
  const input = page.getByLabel("角色别名（每行一个，最多 10 个）", {
    exact: true,
  });
  await expect(input).toHaveValue("小青");
  await input.fill("小青\nAlice");
  await page.getByRole("button", { name: "保存故事设定", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "阿青 · 修订 1", exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "新建设定", exact: true }).click();
  await page.getByRole("combobox", { name: "设定类型", exact: true }).click();
  await page.getByRole("option", { name: "角色", exact: true }).click();
  await page.getByLabel("设定名称", { exact: true }).fill("ＡＬＩＣＥ");
  await page.getByRole("button", { name: "保存故事设定", exact: true }).click();
  await expect(page.getByRole("alert")).toContainText("角色名或别名已被使用");
  await expect(page.getByLabel("设定名称", { exact: true })).toHaveValue(
    "ＡＬＩＣＥ",
  );
  const rows = await (
    await page.request.get(env.entries, { headers: env.headers })
  ).json();
  expect(rows).toHaveLength(1);
  expect(rows[0].aliases).toEqual(["小青", "Alice"]);
});

test("switching exact versions ignores an earlier delayed matching response", async ({
  page,
}) => {
  const env = await setup(page);
  const second = await page.request.post(env.prefix + "/scripts", {
    headers: env.headers,
    data: {
      expected_version_no: 1,
      content: {
        ...env.source.content_json,
        title: "另一个角色稿",
        characters: ["小雨"],
      },
    },
  });
  expect(second.status()).toBe(201);
  const newer = await second.json();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "另一个角色稿" })
    .click();
  const newerUrl = `${env.prefix}/scripts/${newer.id}/character-matches`;
  const response = await (
    await page.request.get(newerUrl, { headers: env.headers })
  ).json();
  let release: () => void = () => {};
  const wait = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**" + newerUrl, async (route) => {
    await wait;
    await route.fulfill({ json: response });
  });
  await page.getByText("角色匹配与确认", { exact: true }).click();
  await expect(
    page.getByRole("button", { name: "正在匹配…", exact: true }),
  ).toBeVisible();
  await page.locator(".version-row").filter({ hasText: "角色来源稿" }).click();
  await page.getByText("角色匹配与确认", { exact: true }).click();
  await expect(
    page.getByRole("region", { name: "角色确认 阿青", exact: true }),
  ).toBeVisible();
  release();
  await expect(
    page.getByRole("region", { name: "角色确认 小雨", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("region", { name: "角色确认 阿青", exact: true }),
  ).toBeVisible();
});
