import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `revision-${crypto.randomUUID()}@example.com`,
      password: "source-revision-password",
      display_name: "来源修订验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "来源版本验收" },
    })
  ).json();
  const episode = await (
    await page.request.post(`/api/v1/projects/${project.id}/episodes`, {
      headers,
      data: { episode_no: 1, title: "来源第一集" },
    })
  ).json();
  const path = `/api/v1/episodes/${episode.id}/story-sources`;
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openSources(page);
  return { owner, headers, project, episode, path };
}
async function openSources(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "故事来源", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "故事来源分页", exact: true }),
  ).toHaveAttribute("aria-busy", "false");
}
async function fillText(page: Page, text = "雨夜，女孩走进书店。") {
  await page.getByLabel("来源正文", { exact: true }).fill(text);
  await page.getByLabel("文本来源说明", { exact: true }).fill("本人原创");
  await page.getByLabel("文本授权说明", { exact: true }).fill("本人拥有使用权");
  await page
    .getByRole("combobox", { name: "文本使用权确认", exact: true })
    .click();
  await page
    .getByRole("option", { name: "我确认有权保存和使用此文本", exact: true })
    .click();
}
const save = (page: Page) =>
  page.getByRole("button", { name: "另存来源版本", exact: true });
const preview = (page: Page) =>
  page.getByRole("region", { name: "故事来源预览", exact: true });
async function seed(
  page: Page,
  env: Awaited<ReturnType<typeof setup>>,
  text = "原稿",
) {
  const result = await page.request.post(env.path + "/text", {
    headers: env.headers,
    data: {
      import_key: crypto.randomUUID(),
      original_name: "测试原稿.txt",
      text,
      source_note: "本人原创",
      rights_note: "本人拥有使用权",
      rights_confirmed: true,
    },
  });
  expect(result.status()).toBe(201);
  return result.json();
}

test("saves original text, appends a revision, opens old bytes and exact writer lineage at 390px", async ({
  page,
}, info) => {
  const env = await setup(page);
  await page.getByRole("button", { name: "保存故事文本", exact: true }).click();
  await fillText(page, "原稿😀\n第一段保留。");
  await save(page).click();
  await expect(page.getByLabel("原始故事文本", { exact: true })).toHaveText(
    "原稿😀\n第一段保留。",
  );
  await page.getByRole("button", { name: "修订此来源", exact: true }).click();
  await fillText(page, "修订😀\n增加第二段。");
  await page.getByLabel("修订说明", { exact: true }).fill("增加转折");
  await save(page).click();
  await expect(preview(page)).toContainText("来源版本 v2 · 增加转折");
  await page.getByRole("button", { name: "查看来源 v1", exact: true }).click();
  await expect(page.getByLabel("原始故事文本", { exact: true })).toHaveText(
    "原稿😀\n第一段保留。",
  );
  await page.screenshot({
    path: info.outputPath("source-lineage-desktop.png"),
    fullPage: true,
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await preview(page)
    .getByText("来源修订历史", { exact: true })
    .scrollIntoViewIfNeeded();
  await page.screenshot({
    path: info.outputPath("source-lineage-mobile.png"),
    fullPage: true,
  });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBe(true);
  const first = (
    await (await page.request.get(env.path, { headers: env.headers })).json()
  ).find(
    (row: { provenance: { version_no: number } }) =>
      row.provenance.version_no === 1,
  );
  const original = await page.request.get(`${env.path}/${first.id}/original`, {
    headers: env.headers,
  });
  expect(await original.body()).toEqual(Buffer.from("原稿😀\n第一段保留。"));
  await page.getByRole("button", { name: "从此版本编剧", exact: true }).click();
  await expect(
    page.getByRole("radio", { name: "任务与审核", exact: true }),
  ).toHaveAttribute("aria-checked", "true");
  await openSources(page);
  await page
    .getByRole("button", { name: "导入 1 · 故事.txt", exact: true })
    .click();
  await expect(page.getByLabel("此版本的派生记录")).toContainText("编剧与分镜");
});

test("lost save receipt reuses its key, retains input and only appends once", async ({
  page,
}) => {
  const env = await setup(page);
  let lose = true;
  const keys: string[] = [];
  await page.route("**/story-sources/text", async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().postDataJSON().import_key);
    const response = await route.fetch();
    if (lose) {
      lose = false;
      await route.abort("failed");
    } else await route.fulfill({ response });
  });
  await page.getByRole("button", { name: "保存故事文本", exact: true }).click();
  await fillText(page);
  await save(page).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("来源正文", { exact: true })).toHaveValue(
    "雨夜，女孩走进书店。",
  );
  await save(page).click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  expect(keys).toHaveLength(2);
  expect(keys[0]).toBe(keys[1]);
  expect(
    await (await page.request.get(env.path, { headers: env.headers })).json(),
  ).toHaveLength(1);
  await page.getByRole("button", { name: "保存故事文本", exact: true }).click();
  await expect(page.getByLabel("来源正文", { exact: true })).toHaveValue("");
});

test("conflict retains a revision draft across reload and a successful save survives directory failure", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await seed(page, env);
  await page.reload();
  await openSources(page);
  await page
    .getByRole("button", { name: "导入 1 · 测试原稿.txt", exact: true })
    .click();
  await page.getByRole("button", { name: "修订此来源", exact: true }).click();
  await fillText(page, "本机保留的修订");
  await page.getByLabel("修订说明", { exact: true }).fill("本机编辑");
  const concurrent = await page.request.post(env.path + "/text", {
    headers: env.headers,
    data: {
      import_key: crypto.randomUUID(),
      original_name: "测试原稿.txt",
      text: "另一个标签已保存",
      source_note: "本人原创",
      rights_note: "本人拥有使用权",
      rights_confirmed: true,
      base_source_id: first.id,
      change_note: "并发修订",
    },
  });
  expect(concurrent.status()).toBe(201);
  await save(page).click();
  await expect(page.getByRole("dialog").getByRole("alert")).toContainText(
    "该来源已有新修订",
  );
  await page.reload();
  await openSources(page);
  await page
    .getByRole("button", { name: "导入 1 · 测试原稿.txt", exact: true })
    .click();
  await page.getByRole("button", { name: "修订此来源", exact: true }).click();
  await expect(page.getByLabel("来源正文", { exact: true })).toHaveValue(
    "本机保留的修订",
  );
  await page
    .getByRole("dialog")
    .getByRole("button", { name: "Close", exact: true })
    .click();
  await page.getByRole("button", { name: "保存故事文本", exact: true }).click();
  await fillText(page, "独立的新来源");
  let saved = false;
  await page.route("**/story-sources/text", async (route) => {
    const response = await route.fetch();
    saved = true;
    await route.fulfill({ response });
  });
  await page.route(
    `**/episodes/${env.episode.id}/story-sources?*`,
    async (route) =>
      saved
        ? route.fulfill({
            status: 503,
            json: { error: { code: "UNAVAILABLE", message: "目录暂不可用" } },
          })
        : route.continue(),
  );
  await save(page).click();
  await expect(page.getByLabel("原始故事文本", { exact: true })).toHaveText(
    "独立的新来源",
  );
  await expect(
    page.getByText(
      "来源版本已保存；目录刷新失败，请重新加载目录，无需再次保存。",
      { exact: true },
    ),
  ).toBeVisible();
});

test("file revision retains original file and archived viewer has only history and provenance", async ({
  page,
}) => {
  const env = await setup(page);
  const first = await seed(page, env);
  await page.reload();
  await openSources(page);
  await page
    .getByRole("button", { name: "导入 1 · 测试原稿.txt", exact: true })
    .click();
  await page.getByRole("button", { name: "使用文件修订", exact: true }).click();
  await page.getByLabel("故事文件", { exact: true }).setInputFiles({
    name: "修订.md",
    mimeType: "text/markdown",
    buffer: Buffer.from("# 新文件\r\n修订版本"),
  });
  await page.getByLabel("文件修订说明", { exact: true }).fill("上传文件修订");
  await page.getByRole("combobox", { name: "使用权确认", exact: true }).click();
  await page
    .getByRole("option", {
      name: "我确认拥有导入和使用此故事的权利",
      exact: true,
    })
    .click();
  await page.getByRole("button", { name: "保存故事来源", exact: true }).click();
  await expect(preview(page)).toContainText("来源版本 v2 · 上传文件修订");
  expect(
    await (
      await page.request.get(`${env.path}/${first.id}/original`, {
        headers: env.headers,
      })
    ).body(),
  ).toEqual(Buffer.from("原稿"));
  const viewer = await (
    await page.request.post("/api/v1/auth/register", {
      data: {
        email: `viewer-${crypto.randomUUID()}@example.com`,
        password: "source-revision-password",
        display_name: "来源只读成员",
      },
    })
  ).json();
  expect(
    (
      await page.request.post(`/api/v1/projects/${env.project.id}/members`, {
        headers: env.headers,
        data: { email: viewer.user.email, role: "viewer" },
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
  await page.reload();
  await openSources(page);
  await page
    .getByRole("button", { name: "导入 1 · 测试原稿.txt", exact: true })
    .click();
  await expect(page.getByLabel("来源修订历史")).toContainText("查看来源 v2");
  await expect(
    page.getByRole("button", { name: "修订此来源", exact: true }),
  ).toHaveCount(0);
  await expect(
    page.getByRole("button", { name: "从此版本编剧", exact: true }),
  ).toHaveCount(0);
});
