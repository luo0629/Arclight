import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "episode-" + crypto.randomUUID() + "@example.com",
      password: "episode-editor-test",
      display_name: "剧集编辑验收",
    },
  });
  expect(response.status()).toBe(201);
  const owner = await response.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const projectResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "剧集信息验收" },
  });
  expect(projectResponse.status()).toBe(201);
  const project = await projectResponse.json();
  const episodeResponse = await page.request.post(
    "/api/v1/projects/" + project.id + "/episodes",
    {
      headers,
      data: { episode_no: 1, title: "星光初稿", target_duration_ms: 30000 },
    },
  );
  expect(episodeResponse.status()).toBe(201);
  const episode = await episodeResponse.json();
  return { headers, project, episode, path: "/api/v1/episodes/" + episode.id };
}

async function edit(page: Page, title: string, duration: string) {
  const dialog = page.getByRole("dialog", { name: "编辑剧集信息" });
  await dialog.getByLabel("剧集标题", { exact: true }).fill(title);
  await dialog.getByLabel("目标时长（秒）", { exact: true }).fill(duration);
  await dialog.getByRole("button", { name: "保存剧集", exact: true }).click();
}

test("TXT-02：Owner 保存毫秒精度，目录、标题、画布同步，设置页按剧集编辑并持久化", async ({
  page,
}, testInfo) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { headers, project, path } = await setup(page);
  const second = await page.request.post(
    "/api/v1/projects/" + project.id + "/episodes",
    { headers, data: { episode_no: 2, title: "第二集原稿" } },
  );
  expect(second.status()).toBe(201);
  await page.goto("/");
  await page.getByRole("button", { name: "编辑剧集", exact: true }).click();
  await page.screenshot({
    path: testInfo.outputPath("episode-editor-desktop.png"),
    fullPage: true,
  });
  const saved = page.waitForResponse(
    (response) =>
      response.url().endsWith(path) && response.request().method() === "PATCH",
  );
  await edit(page, "  星光新标题  ", "31.125");
  const response = await saved;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual({
    title: "星光新标题",
    target_duration_ms: 31125,
  });
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 星光新标题" }),
  ).toBeVisible();
  await expect(
    page.getByRole("button", { name: "编辑剧集", exact: true }),
  ).toBeFocused();
  await expect(
    page.getByRole("navigation", { name: "剧集列表" }),
  ).toContainText("31.125 秒目标时长");
  await expect(page.locator(".paper-node.episode")).toContainText("星光新标题");
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await expect(page.locator(".version-list")).toContainText("星光新标题");
  await page.reload();
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 星光新标题" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  const secondRow = page
    .locator(".episode-settings li")
    .filter({ hasText: "第二集原稿" });
  await secondRow.getByRole("button", { name: "编辑剧集" }).click();
  await expect(page.getByLabel("剧集标题", { exact: true })).toHaveValue(
    "第二集原稿",
  );
  await edit(page, "第二集终稿", "1800");
  await expect(page.locator(".episode-settings")).toContainText(
    "1800 秒目标时长",
  );
  await expect(page.locator(".episode-settings")).toContainText("星光新标题");
  await page
    .getByRole("navigation", { name: "剧集列表" })
    .getByRole("button", { name: /第二集终稿/ })
    .click();
  await expect(
    page.getByRole("heading", { name: "第 2 集 · 第二集终稿" }),
  ).toBeVisible();
  await expect(page.locator(".paper-node.episode")).toContainText("第二集终稿");
  const persisted = await page.request.get(path, { headers });
  expect((await persisted.json()).target_duration_ms).toBe(31125);
  expect(crashes).toEqual([]);
});

test("TXT-02：输入边界、取消和 Esc、失败保留草稿、防重复提交及移动端弹窗", async ({
  page,
}, testInfo) => {
  const { headers, path } = await setup(page);
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto("/");
  const trigger = page.getByRole("button", { name: "编辑剧集", exact: true });
  await trigger.click();
  const dialog = page.getByRole("dialog", { name: "编辑剧集信息" });
  const title = dialog.getByLabel("剧集标题", { exact: true });
  const duration = dialog.getByLabel("目标时长（秒）", { exact: true });
  await expect(title).toBeFocused();
  let patches = 0;
  page.on("request", (request) => {
    if (request.url().endsWith(path) && request.method() === "PATCH") patches++;
  });
  await title.fill("不会保存");
  await page.keyboard.press("Escape");
  await expect(dialog).toHaveCount(0);
  await expect(trigger).toBeFocused();
  await trigger.click();
  await expect(title).toHaveValue("星光初稿");
  await title.fill("取消的标题");
  await dialog.getByRole("button", { name: "取消", exact: true }).click();
  await trigger.click();
  await edit(page, "   ", "30");
  await expect(dialog.getByRole("alert")).toContainText("不能只填空格");
  await title.fill("边界验收");
  for (const value of ["", "2.499", "1800.001", "30.0001"]) {
    await duration.fill(value);
    await dialog.getByRole("button", { name: "保存剧集" }).click();
    expect(
      await duration.evaluate(
        (input: HTMLInputElement) => input.validity.valid,
      ),
    ).toBe(false);
  }
  expect(patches).toBe(0);
  await expect(title).toHaveAttribute("maxlength", "160");
  await page.route("**" + path, (route) =>
    route.request().method() === "PATCH"
      ? route.abort("failed")
      : route.continue(),
  );
  await edit(page, "网络失败后继续保存", "2.5");
  await expect(dialog.getByRole("alert")).toContainText("网络连接失败");
  await expect(title).toHaveValue("网络失败后继续保存");
  await expect(duration).toHaveValue("2.5");
  await page.screenshot({
    path: testInfo.outputPath("episode-editor-mobile.png"),
    fullPage: true,
  });
  const box = await dialog.boundingBox();
  expect(box!.x).toBeGreaterThanOrEqual(0);
  expect(box!.x + box!.width).toBeLessThanOrEqual(390);
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await page.unroute("**" + path);
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  await page.route("**" + path, async (route) => {
    if (route.request().method() === "PATCH") await gate;
    await route.continue();
  });
  try {
    await dialog.getByRole("button", { name: "保存剧集" }).click();
    await expect(
      dialog.getByRole("button", { name: "保存中…" }),
    ).toBeDisabled();
    await expect(
      dialog.getByRole("button", { name: "取消", exact: true }),
    ).toBeDisabled();
    await page.keyboard.press("Escape");
    await expect(dialog).toBeVisible();
    expect(patches).toBe(2);
  } finally {
    release();
  }
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 网络失败后继续保存" }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(path, { headers })).json())
      .target_duration_ms,
  ).toBe(2500);
});

test("TXT-02：Viewer 无编辑入口，Editor 可编辑，剧集和项目归档保持只读", async ({
  page,
  browser,
  baseURL,
}) => {
  const { headers, project, path } = await setup(page);
  const context = await browser.newContext({ baseURL });
  try {
    const email = "episode-member-" + crypto.randomUUID() + "@example.com";
    const registration = await context.request.post("/api/v1/auth/register", {
      data: {
        email,
        password: "episode-member-test",
        display_name: "剧集伙伴",
      },
    });
    expect(registration.status()).toBe(201);
    const member = await registration.json();
    const memberHeaders = { Authorization: "Bearer " + member.access_token };
    const membersPath = "/api/v1/projects/" + project.id + "/members";
    expect(
      (
        await page.request.post(membersPath, {
          headers,
          data: { email, role: "viewer" },
        })
      ).status(),
    ).toBe(201);
    const partner = await context.newPage();
    await partner.goto("/");
    await expect(
      partner.getByText("你当前拥有只读权限", { exact: true }),
    ).toBeVisible();
    await expect(partner.getByRole("button", { name: "编辑剧集" })).toHaveCount(
      0,
    );
    expect(
      (
        await context.request.patch(path, {
          headers: memberHeaders,
          data: { title: "越权编辑" },
        })
      ).status(),
    ).toBe(403);
    expect(
      (
        await page.request.patch(membersPath + "/" + member.user.id, {
          headers,
          data: { role: "editor" },
        })
      ).ok(),
    ).toBeTruthy();
    await partner.reload();
    await partner.getByRole("button", { name: "编辑剧集" }).click();
    await edit(partner, "伙伴修改的标题", "45");
    await expect(
      partner.getByRole("heading", { name: "第 1 集 · 伙伴修改的标题" }),
    ).toBeVisible();
    await expect(
      partner.getByRole("button", { name: "项目设置与归档" }),
    ).toHaveCount(0);
    expect(
      (
        await page.request.patch(path, { headers, data: { archived: true } })
      ).ok(),
    ).toBeTruthy();
    await partner.reload();
    await expect(
      partner.getByRole("button", { name: "编辑剧集" }),
    ).toBeDisabled();
    await page.goto("/");
    await page.getByRole("button", { name: "项目设置与归档" }).click();
    await expect(page.getByRole("button", { name: "编辑剧集" })).toBeDisabled();
    expect(
      (
        await context.request.patch(path, {
          headers: memberHeaders,
          data: { title: "归档修改" },
        })
      ).status(),
    ).toBe(409);
    expect(
      (
        await page.request.patch(path, { headers, data: { archived: false } })
      ).ok(),
    ).toBeTruthy();
    expect(
      (
        await page.request.patch("/api/v1/projects/" + project.id, {
          headers,
          data: { archived: true },
        })
      ).ok(),
    ).toBeTruthy();
    await partner.reload();
    await expect(
      partner.getByRole("button", { name: "编辑剧集" }),
    ).toBeDisabled();
    await page.reload();
    await page.getByRole("button", { name: "项目设置与归档" }).click();
    await expect(page.getByRole("button", { name: "编辑剧集" })).toBeDisabled();
    expect(
      (
        await context.request.patch(path, {
          headers: memberHeaders,
          data: { title: "项目归档修改" },
        })
      ).status(),
    ).toBe(409);
  } finally {
    await context.close();
  }
});

test("TXT-02：真实活动任务拒绝修改，保留草稿，取消任务后可重试", async ({
  page,
}) => {
  const { headers, path } = await setup(page);
  const started = await page.request.post(path + "/script-runs", {
    headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
    data: { story: "女孩在森林里发现了一颗会说话的星星。" },
  });
  expect(started.status()).toBe(202);
  const run = await started.json();
  await expect
    .poll(
      async () =>
        (
          await (
            await page.request.get("/api/v1/runs/" + run.run_id, { headers })
          ).json()
        ).status,
    )
    .toBe("WAITING_REVIEW");
  await page.goto("/");
  await page.getByRole("button", { name: "编辑剧集" }).click();
  await edit(page, "任务结束后的新标题", "42");
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText(
    "请先结束或取消正在进行的工作流",
  );
  await expect(dialog.getByLabel("剧集标题", { exact: true })).toHaveValue(
    "任务结束后的新标题",
  );
  expect((await (await page.request.get(path, { headers })).json()).title).toBe(
    "星光初稿",
  );
  expect(
    (
      await page.request.post("/api/v1/runs/" + run.run_id + "/cancel", {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
  await dialog.getByRole("button", { name: "保存剧集" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 任务结束后的新标题" }),
  ).toBeVisible();
});
