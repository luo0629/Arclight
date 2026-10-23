import { openWorkspacePanels } from "./workspace-helpers";
import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page, config: Record<string, unknown> = {}) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: "profile-" + crypto.randomUUID() + "@example.com",
      password: "production-profile-test",
      display_name: "项目配置验收",
    },
  });
  expect(registered.status()).toBe(201);
  const owner = await registered.json();
  const headers = { Authorization: "Bearer " + owner.access_token };
  const created = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "视觉风格验收", ...config },
  });
  expect(created.status()).toBe(201);
  const project = await created.json();
  const path = "/api/v1/projects/" + project.id;
  const episode = await page.request.post(path + "/episodes", {
    headers,
    data: { episode_no: 1, title: "星光森林" },
  });
  expect(episode.status()).toBe(201);
  return { project, path, headers, episode: await episode.json() };
}
async function openSettings(page: Page) {
  await page.goto("/");
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(
    page.getByRole("heading", { name: "视觉风格与画幅", exact: true }),
  ).toBeVisible();
}
async function choose(page: Page, name: string) {
  await page.getByRole("combobox", { name: "画幅预设" }).click();
  await page.getByRole("option", { name, exact: true }).click();
}
const save = (page: Page) =>
  page.getByRole("button", { name: "保存风格与画幅", exact: true });

test("TXT-03：Owner 键盘选择画幅、保存风格与帧率、保留扩展字段、重载与还原", async ({
  page,
}, testInfo) => {
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  const { headers, path } = await setup(page, {
    style_guide: {
      description: "原始水彩",
      palette: ["暖黄", "森林绿"],
      rules: { locked: true },
    },
    output_profile: {
      width: 1080,
      height: 1920,
      fps: 30,
      codec: "h264",
      audio: { sample_rate: 48000 },
    },
  });
  await openSettings(page);
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toHaveValue(
    "原始水彩",
  );
  await expect(page.getByLabel("宽度（像素）", { exact: true })).toHaveValue(
    "1080",
  );
  const selector = page.getByRole("combobox", { name: "画幅预设" });
  await selector.focus();
  await page.keyboard.press("Space");
  await expect(
    page.getByRole("option", { name: "竖屏 9:16 · 1080 × 1920", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("ArrowDown");
  await expect(
    page.getByRole("option", { name: "横屏 16:9 · 1920 × 1080", exact: true }),
  ).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(selector).toBeFocused();
  await expect(page.getByLabel("宽度（像素）", { exact: true })).toHaveValue(
    "1920",
  );
  await expect(page.getByLabel("高度（像素）", { exact: true })).toHaveValue(
    "1080",
  );
  await page
    .getByLabel("视觉风格说明", { exact: true })
    .fill("  水彩绘本，柔和线条，避免写实血腥。  ");
  await page.getByLabel("帧率（fps）", { exact: true }).fill("29.97");
  await page
    .locator(".production-settings")
    .screenshot({ path: testInfo.outputPath("project-profile-desktop.png") });
  const responsePromise = page.waitForResponse(
    (response) =>
      response.url().endsWith(path) && response.request().method() === "PATCH",
  );
  await save(page).click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  expect(response.request().postDataJSON()).toEqual({
    style_guide: {
      description: "水彩绘本，柔和线条，避免写实血腥。",
      palette: ["暖黄", "森林绿"],
      rules: { locked: true },
    },
    output_profile: {
      width: 1920,
      height: 1080,
      fps: 29.97,
      codec: "h264",
      audio: { sample_rate: 48000 },
    },
  });
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  await page.getByText("查看已保存的完整配置（只读）", { exact: true }).click();
  await expect(page.locator(".saved-production-config pre")).toContainText(
    "29.97",
  );
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toHaveValue(
    "水彩绘本，柔和线条，避免写实血腥。",
  );
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("帧率（fps）", { exact: true })).toHaveValue(
    "29.97",
  );
  await expect(selector).toContainText("横屏 16:9");
  await choose(page, "方形 1:1 · 1080 × 1080");
  await page.getByLabel("视觉风格说明", { exact: true }).fill("未保存草稿");
  await page.getByRole("button", { name: "还原已保存配置" }).click();
  await expect(selector).toContainText("横屏 16:9");
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toHaveValue(
    "水彩绘本，柔和线条，避免写实血腥。",
  );
  await page.getByLabel("视觉风格说明", { exact: true }).fill("");
  await save(page).click();
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  const persisted = await (await page.request.get(path, { headers })).json();
  expect(persisted.style_guide).toEqual({
    description: "",
    palette: ["暖黄", "森林绿"],
    rules: { locked: true },
  });
  expect(persisted.output_profile.codec).toBe("h264");
  expect(crashes).toEqual([]);
});

test("TXT-03：空配置、自定义尺寸、数值边界与移动端比例预览", async ({
  page,
}, testInfo) => {
  const { path, headers } = await setup(page, { output_profile: {} });
  await page.setViewportSize({ width: 390, height: 844 });
  await openSettings(page);
  const width = page.getByLabel("宽度（像素）", { exact: true });
  const height = page.getByLabel("高度（像素）", { exact: true });
  const fps = page.getByLabel("帧率（fps）", { exact: true });
  await expect(width).toHaveValue("");
  await expect(
    page.getByRole("figure", { name: "画幅比例预览" }),
  ).toContainText("尚未设置画幅");
  let patches = 0;
  page.on("request", (request) => {
    if (request.url().endsWith(path) && request.method() === "PATCH") patches++;
  });
  await save(page).click();
  expect(
    await width.evaluate((input: HTMLInputElement) => input.validity.valid),
  ).toBe(false);
  await choose(page, "方形 1:1 · 1080 × 1080");
  await fps.fill("23.976");
  for (const value of ["0", "8193", "1080.5", ""]) {
    await width.fill(value);
    await save(page).click();
    expect(
      await width.evaluate((input: HTMLInputElement) => input.validity.valid),
    ).toBe(false);
  }
  await width.fill("2048");
  for (const value of ["0", "8193", "858.5", ""]) {
    await height.fill(value);
    await save(page).click();
    expect(
      await height.evaluate((input: HTMLInputElement) => input.validity.valid),
    ).toBe(false);
  }
  await height.fill("858");
  for (const value of ["0", "120.001", "23.9761", ""]) {
    await fps.fill(value);
    await save(page).click();
    expect(
      await fps.evaluate((input: HTMLInputElement) => input.validity.valid),
    ).toBe(false);
  }
  expect(patches).toBe(0);
  await fps.fill("23.976");
  await expect(page.getByRole("combobox", { name: "画幅预设" })).toContainText(
    "自定义尺寸",
  );
  await expect(
    page.getByLabel("视觉风格说明", { exact: true }),
  ).toHaveAttribute("maxlength", "10000");
  await expect(
    page.getByRole("figure", { name: "画幅比例预览" }),
  ).toContainText("2048 × 858 像素");
  await page
    .locator(".production-settings")
    .screenshot({ path: testInfo.outputPath("project-profile-mobile.png") });
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= window.innerWidth,
    ),
  ).toBe(true);
  await save(page).click();
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  const persisted = await (await page.request.get(path, { headers })).json();
  expect(persisted.output_profile).toEqual({
    width: 2048,
    height: 858,
    fps: 23.976,
  });
  expect(persisted.style_guide).toEqual({});
  await width.fill("1");
  await height.fill("8192");
  await fps.fill("120");
  await save(page).click();
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(path, { headers })).json()).output_profile,
  ).toEqual({ width: 1, height: 8192, fps: 120 });
});

test("TXT-03：非文本旧风格原样保留，不同项目配置不串用", async ({ page }) => {
  const style = {
    description: { text: "旧结构风格", locked: true },
    palette: ["红", "蓝"],
  };
  const { project, path, headers } = await setup(page, { style_guide: style });
  await openSettings(page);
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toBeDisabled();
  await expect(page.getByText(/已有风格说明不是文本格式/)).toBeVisible();
  await choose(page, "横屏 16:9 · 1920 × 1080");
  await save(page).click();
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  expect(
    (await (await page.request.get(path, { headers })).json()).style_guide,
  ).toEqual(style);
  const otherResponse = await page.request.post("/api/v1/projects", {
    headers,
    data: {
      name: "另一本创作本",
      style_guide: { description: "像素风格" },
      output_profile: { width: 720, height: 720, fps: 24 },
    },
  });
  expect(otherResponse.status()).toBe(201);
  const other = await otherResponse.json();
  await page.reload();
  await openWorkspacePanels(page);
  await page.getByRole("combobox", { name: "选择项目" }).selectOption(other.id);
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toHaveValue(
    "像素风格",
  );
  await expect(page.getByLabel("宽度（像素）", { exact: true })).toHaveValue(
    "720",
  );
  await page
    .getByRole("combobox", { name: "选择项目" })
    .selectOption(project.id);
  await page.getByRole("button", { name: "项目设置与归档" }).click();
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toBeDisabled();
  await expect(page.getByLabel("宽度（像素）", { exact: true })).toHaveValue(
    "1920",
  );
});

test("TXT-03：网络失败和真实活动任务 409 保留草稿，取消后重试，保存中禁止重复操作", async ({
  page,
}) => {
  const { path, headers, episode } = await setup(page);
  await openSettings(page);
  await page
    .getByLabel("视觉风格说明", { exact: true })
    .fill("重试后保存的水彩风格");
  await choose(page, "横屏 16:9 · 1920 × 1080");
  await page.route("**" + path, (route) =>
    route.request().method() === "PATCH"
      ? route.abort("failed")
      : route.continue(),
  );
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText("网络连接失败");
  await expect(page.getByLabel("视觉风格说明", { exact: true })).toHaveValue(
    "重试后保存的水彩风格",
  );
  await page.unroute("**" + path);
  const started = await page.request.post(
    "/api/v1/episodes/" + episode.id + "/script-runs",
    {
      headers: { ...headers, "Idempotency-Key": crypto.randomUUID() },
      data: { story: "女孩走进森林，寻找落下的星星。" },
    },
  );
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
  await save(page).click();
  await expect(page.getByRole("alert")).toContainText(
    "请先结束或取消正在进行的工作流",
  );
  expect(
    (await (await page.request.get(path, { headers })).json()).style_guide,
  ).toEqual({});
  expect(
    (
      await page.request.post("/api/v1/runs/" + run.run_id + "/cancel", {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
  let release!: () => void;
  const gate = new Promise<void>((resolve) => {
    release = resolve;
  });
  let patches = 0;
  await page.route("**" + path, async (route) => {
    if (route.request().method() === "PATCH") {
      patches++;
      await gate;
    }
    await route.continue();
  });
  try {
    await save(page).click();
    await expect(
      page.getByRole("button", { name: "保存中…", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("combobox", { name: "画幅预设" }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "保存设置", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "归档项目", exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("button", { name: "还原已保存配置" }),
    ).toBeDisabled();
    expect(patches).toBe(1);
  } finally {
    release();
  }
  await expect(
    page.getByText("风格与画幅已保存", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("alert")).toHaveCount(0);
  const persisted = await (await page.request.get(path, { headers })).json();
  expect(persisted.style_guide.description).toBe("重试后保存的水彩风格");
  expect(persisted.output_profile.width).toBe(1920);
});

test("TXT-03：仅 Owner 可修改配置，归档只读与恢复后继续编辑", async ({
  page,
  browser,
  baseURL,
}) => {
  const { path, headers } = await setup(page);
  const context = await browser.newContext({ baseURL });
  try {
    const email = "profile-member-" + crypto.randomUUID() + "@example.com";
    const registered = await context.request.post("/api/v1/auth/register", {
      data: {
        email,
        password: "profile-member-test",
        display_name: "配置伙伴",
      },
    });
    expect(registered.status()).toBe(201);
    const member = await registered.json();
    const memberHeaders = { Authorization: "Bearer " + member.access_token };
    expect(
      (
        await page.request.post(path + "/members", {
          headers,
          data: { email, role: "viewer" },
        })
      ).status(),
    ).toBe(201);
    const partner = await context.newPage();
    for (const role of ["viewer", "editor"]) {
      if (role === "editor")
        expect(
          (
            await page.request.patch(path + "/members/" + member.user.id, {
              headers,
              data: { role },
            })
          ).ok(),
        ).toBeTruthy();
      await partner.goto("/");
      await openWorkspacePanels(partner);
      await expect(
        partner.getByText(
          role === "viewer" ? "你当前拥有只读权限" : "你可以编辑与创建任务",
          { exact: true },
        ),
      ).toBeVisible();
      await expect(
        partner.getByRole("button", { name: "项目设置与归档" }),
      ).toHaveCount(0);
      expect(
        (
          await context.request.patch(path, {
            headers: memberHeaders,
            data: {
              style_guide: { description: "不应保存" },
              output_profile: { width: 1, height: 1, fps: 1 },
            },
          })
        ).status(),
      ).toBe(403);
    }
    await openSettings(page);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "归档项目", exact: true }).click();
    await expect(
      page.getByRole("button", { name: "恢复项目", exact: true }),
    ).toBeVisible();
    await expect(
      page.getByLabel("视觉风格说明", { exact: true }),
    ).toBeDisabled();
    await expect(
      page.getByRole("combobox", { name: "画幅预设" }),
    ).toBeDisabled();
    await expect(
      page.getByLabel("宽度（像素）", { exact: true }),
    ).toBeDisabled();
    await expect(save(page)).toBeDisabled();
    expect(
      (
        await page.request.patch(path, {
          headers,
          data: { style_guide: { description: "归档不应保存" } },
        })
      ).status(),
    ).toBe(409);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "恢复项目", exact: true }).click();
    await expect(save(page)).toBeEnabled();
    await page
      .getByLabel("视觉风格说明", { exact: true })
      .fill("恢复后继续编辑");
    await save(page).click();
    await expect(
      page.getByText("风格与画幅已保存", { exact: true }),
    ).toBeVisible();
  } finally {
    await context.close();
  }
});
