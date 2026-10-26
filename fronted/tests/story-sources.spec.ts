import { test, expect, type Page } from "@playwright/test";
import { openWorkspacePanels } from "./workspace-helpers";

async function setup(page: Page) {
  const registration = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `sources-${crypto.randomUUID()}@example.com`,
      password: "sources-test-password",
      display_name: "故事导入验收",
    },
  });
  expect(registration.status()).toBe(201);
  const owner = await registration.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const result = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "故事来源验收" },
  });
  expect(result.status()).toBe(201);
  const project = await result.json();
  const episodeResult = await page.request.post(
    `/api/v1/projects/${project.id}/episodes`,
    { headers, data: { episode_no: 1, title: "导入原稿" } },
  );
  expect(episodeResult.status()).toBe(201);
  const episode = await episodeResult.json();
  const path = `/api/v1/episodes/${episode.id}/story-sources`;
  await page.addInitScript(
    (id) => localStorage.setItem("arclight-project", id),
    project.id,
  );
  await page.goto("/");
  await openSources(page);
  return { headers, project, episode, path };
}

async function openSources(page: Page) {
  await openWorkspacePanels(page);
  await page.getByRole("radio", { name: "故事来源", exact: true }).click();
  await expect(
    page.getByRole("region", { name: "故事来源分页", exact: true }),
  ).toHaveAttribute("aria-busy", "false");
}
async function fill(
  page: Page,
  name = "故事.md",
  buffer = Buffer.from("# 雨夜\r\n原始故事"),
  mimeType = "text/markdown",
) {
  await page
    .getByLabel("故事文件", { exact: true })
    .setInputFiles({ name, mimeType, buffer });
  await page.getByLabel("来源说明", { exact: true }).fill("本人原创初稿");
  await page.getByLabel("授权说明", { exact: true }).fill("本人拥有使用权");
  await page.getByRole("combobox", { name: "使用权确认", exact: true }).click();
  await page
    .getByRole("option", {
      name: "我确认拥有导入和使用此故事的权利",
      exact: true,
    })
    .click();
}
const save = (page: Page) =>
  page.getByRole("button", { name: "保存故事来源", exact: true });

for (const mobile of [false, true]) {
  test(`STORY-05：${mobile ? "移动端" : "桌面端"}安全预览、刷新与独立来源`, async ({
    page,
  }, testInfo) => {
    if (mobile) await page.setViewportSize({ width: 390, height: 844 });
    const { headers, path, episode } = await setup(page);
    await expect(save(page)).toBeDisabled();
    const text =
      "# 雨夜\r\n<script>window.storyExecuted = true</script>\r\n![远程图片](https://example.invalid/image.png)\r\n" +
      "长文本".repeat(150);
    let external = 0;
    await page.route("https://example.invalid/**", (route) => {
      external++;
      return route.abort();
    });
    await fill(page, "原稿.MD", Buffer.from("\ufeff" + text));
    await save(page).click();
    await expect(
      page.getByText("故事来源已保存，未启动生成。", { exact: true }),
    ).toBeVisible();
    await expect(page.getByLabel("原始故事文本", { exact: true })).toHaveText(
      text,
    );
    await expect(
      page.getByRole("region", { name: "故事来源预览", exact: true }),
    ).toBeFocused();
    expect(await page.evaluate(() => "storyExecuted" in window)).toBe(false);
    expect(external).toBe(0);
    expect(
      await page.getByLabel("原始故事文本").locator("script, img").count(),
    ).toBe(0);
    await page.screenshot({
      path: testInfo.outputPath(
        mobile ? "sources-mobile.png" : "sources-desktop.png",
      ),
      fullPage: true,
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBe(true);
    const original = (
      await (await page.request.get(path, { headers })).json()
    )[0];
    const download = await page.request.get(`${path}/${original.id}/original`, {
      headers,
    });
    expect(await download.body()).toEqual(Buffer.from("\ufeff" + text));
    await page.reload();
    await openSources(page);
    await page
      .getByRole("button", { name: "导入 1 · 原稿.MD", exact: true })
      .click();
    await expect(page.getByLabel("原始故事文本")).toHaveText(text);
    await fill(page, "第二稿.txt", Buffer.from("新的独立来源"), "text/plain");
    await save(page).click();
    await expect(page.getByLabel("原始故事文本")).toHaveText("新的独立来源");
    await page
      .getByRole("button", { name: "导入 1 · 原稿.MD", exact: true })
      .click();
    await expect(page.getByLabel("原始故事文本")).toHaveText(text);
    expect(
      await (await page.request.get(path, { headers })).json(),
    ).toHaveLength(2);
    expect(
      await (
        await page.request.get(`/api/v1/episodes/${episode.id}/runs`, {
          headers,
        })
      ).json(),
    ).toHaveLength(0);
  });
}

test("STORY-05：编码失败保留输入，显式 GB18030 重试", async ({ page }) => {
  const { headers, path } = await setup(page);
  await fill(
    page,
    "旧稿.txt",
    Buffer.from([0xd6, 0xd0, 0xce, 0xc4]),
    "text/plain",
  );
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("无法按所选编码解码");
  await expect(page.getByLabel("来源说明", { exact: true })).toHaveValue(
    "本人原创初稿",
  );
  expect(
    await page
      .getByLabel("故事文件", { exact: true })
      .evaluate((el: HTMLInputElement) => el.files?.[0].name),
  ).toBe("旧稿.txt");
  await page.getByRole("combobox", { name: "文件编码", exact: true }).click();
  await page
    .getByRole("option", { name: "GB18030（旧版中文文本）", exact: true })
    .click();
  await save(page).click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("中文");
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    1,
  );
});

test("STORY-05：提交回执丢失后重试只导入一次", async ({ page }) => {
  const { headers, path } = await setup(page);
  let first = true;
  const keys: string[] = [];
  await page.route(`**${path}`, async (route) => {
    if (route.request().method() !== "POST") return route.continue();
    keys.push(route.request().postDataJSON().import_key);
    if (!first) return route.continue();
    first = false;
    const response = await route.fetch();
    expect(response.status()).toBe(201);
    await route.abort("failed");
  });
  await fill(page);
  await save(page).click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(save(page)).toBeEnabled();
  await save(page).click();
  await expect(page.getByLabel("原始故事文本")).toContainText("原始故事");
  expect(keys).toHaveLength(2);
  expect(keys[1]).toBe(keys[0]);
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    1,
  );
});

test("STORY-05：保存成功后目录失败仍显示精确回执，可刷新恢复", async ({
  page,
}) => {
  const { headers, path } = await setup(page);
  let failed = false;
  await page.route(`**${path}?*`, (route) => {
    failed = true;
    return route.abort();
  });
  await fill(page);
  await save(page).click();
  await expect(
    page.getByText("故事来源已保存；目录刷新失败，请重试加载，无需再次导入。", {
      exact: true,
    }),
  ).toBeVisible();
  expect(failed).toBe(true);
  await expect(page.getByLabel("原始故事文本")).toContainText("原始故事");
  await expect(save(page)).toBeDisabled();
  await page.unroute(`**${path}?*`);
  await page.getByRole("button", { name: "刷新故事来源", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "导入 1 · 故事.md", exact: true }),
  ).toBeVisible();
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    1,
  );
});

test("STORY-05：类型与大小错误不发送请求，伪装文件由服务端拒绝", async ({
  page,
}) => {
  const { headers, path } = await setup(page);
  let posts = 0;
  page.on("request", (request) => {
    if (request.method() === "POST" && request.url().endsWith(path)) posts++;
  });
  await fill(page, "附件.docx", Buffer.from("PK"), "application/octet-stream");
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("仅支持 TXT");
  await fill(page, "过大.txt", Buffer.alloc(262145, 65), "text/plain");
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("不能超过 256 KiB");
  expect(posts).toBe(0);
  await fill(page, "伪装.txt", Buffer.from("%PDF-1.7"), "text/plain");
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("不是支持的纯文本");
  expect(posts).toBe(1);
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    0,
  );
});

test("STORY-05：归档项目来源可读且隐藏导入表单", async ({ page }) => {
  const { headers, project, path } = await setup(page);
  await fill(page);
  await save(page).click();
  await expect(page.getByLabel("原始故事文本")).toBeVisible();
  expect(
    (
      await page.request.patch(`/api/v1/projects/${project.id}`, {
        headers,
        data: { archived: true },
      })
    ).status(),
  ).toBe(200);
  await page.reload();
  await openSources(page);
  await expect(
    page.getByText("当前为只读模式，可查看已保存来源。", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("form", { name: "导入故事文件", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "导入 1 · 故事.md", exact: true })
    .click();
  await expect(page.getByLabel("原始故事文本")).toContainText("原始故事");
  expect(await (await page.request.get(path, { headers })).json()).toHaveLength(
    1,
  );
});

test("STORY-05：切换来源时迟到响应不覆盖当前预览，失败可重试", async ({
  page,
}) => {
  const { path } = await setup(page);
  await fill(page, "第一稿.md", Buffer.from("第一份"));
  await save(page).click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("第一份");
  await fill(page, "第二稿.md", Buffer.from("第二份"));
  await save(page).click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("第二份");
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let started!: () => void;
  const seen = new Promise<void>((resolve) => {
    started = resolve;
  });
  await page.route(`**${path}/*`, async (route) => {
    const response = await route.fetch();
    const body = await response.json();
    if (body.import_no === 1) {
      started();
      await gate;
    }
    await route.fulfill({ response });
  });
  await page
    .getByRole("button", { name: "导入 1 · 第一稿.md", exact: true })
    .click();
  await seen;
  await page
    .getByRole("button", { name: "导入 2 · 第二稿.md", exact: true })
    .click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("第二份");
  release();
  await page.unrouteAll({ behavior: "wait" });
  await expect(page.getByLabel("原始故事文本")).toHaveText("第二份");
  await page.route(`**${path}/*`, (route) => route.abort());
  await page
    .getByRole("button", { name: "导入 1 · 第一稿.md", exact: true })
    .click();
  await expect(page.getByRole("alert")).toBeVisible();
  await expect(page.getByLabel("原始故事文本")).toHaveCount(0);
  await page.unroute(`**${path}/*`);
  await page.getByRole("button", { name: "重试读取来源", exact: true }).click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("第一份");
});

test("STORY-05：超过首批的来源可以继续加载并精确读取", async ({ page }) => {
  const { headers, path } = await setup(page);
  for (let number = 1; number <= 101; number++) {
    const result = await page.request.post(path, {
      headers,
      data: {
        import_key: crypto.randomUUID(),
        original_name: `来源${number}.txt`,
        media_type: "text/plain",
        encoding: "utf-8",
        content_base64: Buffer.from(`独立来源 ${number}`).toString("base64"),
        source_note: "分页验收的原创样本",
        rights_note: "本人拥有使用权",
        rights_confirmed: true,
      },
    });
    expect(result.status()).toBe(201);
  }
  await page.getByRole("button", { name: "刷新故事来源", exact: true }).click();
  const pagination = page.getByRole("region", {
    name: "故事来源分页",
    exact: true,
  });
  await expect(pagination.getByRole("status")).toContainText("已加载 100 条");
  await expect(
    page.getByRole("button", { name: "导入 1 · 来源1.txt", exact: true }),
  ).toHaveCount(0);
  await page
    .getByRole("button", { name: "加载更多故事来源", exact: true })
    .click();
  await expect(pagination.getByRole("status")).toContainText(
    "已加载 101 条 · 已全部加载",
  );
  await page
    .getByRole("button", { name: "导入 1 · 来源1.txt", exact: true })
    .click();
  await expect(page.getByLabel("原始故事文本")).toHaveText("独立来源 1");
});
