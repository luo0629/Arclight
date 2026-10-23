import { test, expect, type Page } from "@playwright/test";

async function setup(page: Page, withHistory = false) {
  const response = await page.request.post("/api/v1/auth/register", {
    data: {
      email: crypto.randomUUID() + "@example.com",
      password: "redesign-test-password",
      display_name: "青禾",
    },
  });
  expect(response.status()).toBe(201);
  const session = await response.json();
  const headers = { Authorization: "Bearer " + session.access_token };
  const project = await (
    await page.request.post("/api/v1/projects", {
      headers,
      data: { name: "森林邮局" },
    })
  ).json();
  const episode = await (
    await page.request.post("/api/v1/projects/" + project.id + "/episodes", {
      headers,
      data: { episode_no: 1, title: "寄往春天的信", target_duration_ms: 30000 },
    })
  ).json();
  const script = await (
    await page.request.post("/api/v1/episodes/" + episode.id + "/scripts", {
      headers,
      data: {
        expected_version_no: 0,
        content: {
          title: "寄往春天的信",
          summary:
            "小鹿在森林邮局发现一封没有收件人的信，和萤火虫一起寻找春天。",
          characters: ["小鹿", "萤火虫"],
          scenes: [
            {
              scene_no: 1,
              title: "森林邮局",
              action: "晨光透过树叶，小鹿轻轻推开邮局的门。",
              dialogue: [],
            },
          ],
        },
      },
    })
  ).json();
  expect(script.id).toBeTruthy();
  expect(
    (
      await page.request.post(
        "/api/v1/episodes/" + episode.id + "/scripts/" + script.id + "/review",
        {
          headers,
          data: { decision: "APPROVE", comment: "保留温柔的森林色调" },
        },
      )
    ).ok(),
  ).toBeTruthy();
  const board = await page.request.post(
    "/api/v1/episodes/" + episode.id + "/storyboards",
    {
      headers,
      data: {
        expected_version_no: 0,
        script_version_id: script.id,
        content: {
          shots: [
            {
              shot_no: 1,
              scene_no: 1,
              duration_ms: 5000,
              visual_description:
                "树屋邮局的窗沿上落着一片新叶，小鹿展开信纸。",
              characters: ["小鹿"],
              shot_size: "medium",
              camera_movement: "static",
              subtitle: "",
            },
          ],
        },
      },
    },
  );
  expect(board.ok()).toBeTruthy();
  if (withHistory) {
    const revision = await page.request.post(
      "/api/v1/episodes/" + episode.id + "/scripts",
      {
        headers,
        data: {
          expected_version_no: 1,
          base_version_id: script.id,
          content: {
            ...script.content_json,
            title: "寄往春天的信 · 第二稿",
            summary: "小鹿和萤火虫沿着溪流，把信送给了第一朵迎春花。",
          },
        },
      },
    );
    expect(revision.status()).toBe(201);
  }
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: "第 1 集 · 寄往春天的信" }),
  ).toBeVisible();
  return { headers, project, episode, script };
}
async function noOverflow(page: Page) {
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
}

test("新版：画布为核心，目录与内容面板独立开关，创作及管理有独立入口", async ({
  page,
}) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  await setup(page);
  await expect(page.locator(".canvas-wrap")).toBeVisible();
  await expect(page.getByLabel("内容与属性", { exact: true })).toHaveCount(0);
  const before = await page.locator(".canvas-wrap").boundingBox();
  expect(before!.width).toBeGreaterThan(1100);
  expect(before!.height).toBeGreaterThan(600);
  expect(before!.y + before!.height).toBeLessThanOrEqual(1000);
  await page.getByRole("button", { name: "收起目录", exact: true }).click();
  await expect(page.getByLabel("剧集列表", { exact: true })).not.toBeVisible();
  const after = await page.locator(".canvas-wrap").boundingBox();
  expect(after!.width).toBeGreaterThan(before!.width + 180);
  await page.locator(".paper-node.script").first().click();
  await expect(page.getByLabel("内容与属性", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "关闭内容面板", exact: true }).click();
  await expect(page.getByLabel("内容与属性", { exact: true })).toHaveCount(0);
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await expect(page.getByLabel("故事灵感", { exact: true })).toBeVisible();
  await expect(page.locator(".canvas-wrap")).toHaveCount(0);
  await page.getByLabel("故事灵感", { exact: true }).fill("一封寄给春天的信");
  await page.getByRole("radio", { name: "创作画布", exact: true }).click();
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await expect(page.getByLabel("故事灵感", { exact: true })).toHaveValue(
    "一封寄给春天的信",
  );
  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await expect(page.locator(".canvas-wrap")).toHaveCount(0);
  await page.getByRole("button", { name: "创作工作区", exact: true }).click();
  await expect(page.locator(".canvas-wrap")).toBeVisible();
  await noOverflow(page);
});

test("新版：服务配置真实持久化、切区保留、密钥不回显、不发外部请求", async ({
  page,
}) => {
  await setup(page);
  let outsideCalls = 0;
  await page.route("https://api.openai.com/**", (route) => {
    outsideCalls++;
    return route.abort();
  });
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("heading", { name: "模型与服务" }),
  ).toBeVisible();
  await dialog
    .getByLabel("服务地址", { exact: true })
    .fill("https://api.openai.com/v1");
  await dialog.getByLabel("模型名称", { exact: true }).fill("forest-writer");
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("configuration-only-test-secret");
  await dialog.getByRole("radio", { name: "图片模型" }).click();
  await dialog.getByLabel("模型名称", { exact: true }).fill("forest-image");
  await dialog.getByRole("radio", { name: "文字模型" }).click();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue(
    "configuration-only-test-secret",
  );
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await dialog.getByRole("radio", { name: "图片模型" }).click();
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue(
    "forest-image",
  );
  await dialog
    .getByLabel("服务地址", { exact: true })
    .fill("https://images.example/v1");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("本次只保存预设");
  await page.keyboard.press("Escape");
  await expect(
    page.getByRole("button", { name: "模型服务配置", exact: true }),
  ).toBeFocused();
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue(
    "forest-writer",
  );
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await expect(
    dialog.getByText("密钥已加密保存", { exact: true }),
  ).toBeVisible();
  expect(
    await page.evaluate(() => JSON.stringify([localStorage, sessionStorage])),
  ).not.toContain("configuration-only-test-secret");
  await dialog.getByLabel("模型名称", { exact: true }).fill("unsaved");
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByText("有尚未保存的服务配置，要放弃并关闭吗？"),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑配置" }).click();
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue(
    "unsaved",
  );
  await dialog.getByRole("button", { name: "关闭模型配置" }).click();
  await dialog.getByRole("button", { name: "放弃配置并关闭" }).click();
  expect(outsideCalls).toBe(0);
});

test("新版：配置保存失败保留输入，读取失败可重试，手机390px无横向溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await expect(
    page.getByRole("radio", { name: "版本列表", exact: true }),
  ).toBeChecked();
  await expect(
    page.getByRole("button", { name: "展开目录", exact: true }),
  ).toBeVisible();
  await noOverflow(page);
  await page.route("**/api/v1/me/model-services", async (route) =>
    route.fulfill({
      status: 503,
      contentType: "application/json",
      body: JSON.stringify({ error: { message: "配置读取暂时失败" } }),
    }),
  );
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByRole("alert")).toContainText("配置读取暂时失败");
  await page.unroute("**/api/v1/me/model-services");
  await dialog.getByRole("button", { name: "重试读取配置" }).click();
  await dialog.getByRole("radio", { name: "视频模型" }).click();
  await dialog.getByLabel("模型名称", { exact: true }).fill("my-video-model");
  await page.route("**/api/v1/me/model-services/video", (route) =>
    route.fulfill({
      status: 409,
      contentType: "application/json",
      body: JSON.stringify({ error: { message: "配置冲突，请重新打开" } }),
    }),
  );
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("配置冲突");
  await expect(dialog.getByLabel("模型名称", { exact: true })).toHaveValue(
    "my-video-model",
  );
  await noOverflow(page);
});

test("新版页面截图：桌面各功能、专注画布、服务配置与手机", async ({ page }) => {
  const shot = async (name: string) => {
    await noOverflow(page);
    await page.screenshot({
      path: "test-results/final-" + name + ".png",
      fullPage: !(await page.getByRole("dialog").isVisible()),
      animations: "disabled",
    });
  };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await expect(page.locator(".login-card")).toBeVisible();
  await shot("login-desktop");
  const { headers, episode } = await setup(page, true);
  await expect(page.locator(".paper-node.storyboard")).toBeVisible();
  await shot("canvas-desktop");
  await page.getByRole("button", { name: "收起目录", exact: true }).click();
  await shot("canvas-focus-desktop");
  await page.locator(".paper-node.script").first().click();
  await shot("canvas-details-desktop");
  await page.getByRole("button", { name: "关闭内容面板", exact: true }).click();
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await page
    .getByLabel("故事灵感", { exact: true })
    .fill(
      "小鹿在森林邮局发现了一封没有收件人的信。萤火虫说，也许它是寄给春天的。她们决定沿着溪流，寻找第一朵花。 ",
    );
  await shot("create-desktop");
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "寄往春天的信 · 第二稿" })
    .click();
  await shot("versions-desktop");
  await page.getByRole("button", { name: "查看版本差异", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByText("共 2 处字段差异", { exact: true }),
  ).toBeVisible();
  await shot("comparison-desktop");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await page.getByRole("button", { name: "请编剧开始创作" }).click();
  await expect(
    page.getByText("等待审核：剧本版本", { exact: false }),
  ).toBeVisible({ timeout: 20000 });
  await shot("tasks-desktop");
  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await shot("members-desktop");
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  await shot("project-settings-desktop");
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByLabel("模型名称", { exact: true }),
  ).toBeVisible();
  await shot("service-text-desktop");
  await page.getByRole("radio", { name: "图片模型" }).click();
  await shot("service-image-desktop");
  await page.getByRole("radio", { name: "视频模型" }).click();
  await shot("service-video-desktop");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.reload();
  await page.getByRole("radio", { name: "版本列表", exact: true }).click();
  await page
    .locator(".version-row")
    .filter({ hasText: "寄往春天的信 · 第二稿" })
    .click();
  await shot("versions-mobile");
  await page.getByRole("button", { name: "查看版本差异", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByText("共 2 处字段差异", { exact: true }),
  ).toBeVisible();
  await shot("comparison-mobile");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    page.getByRole("dialog").getByLabel("模型名称", { exact: true }),
  ).toBeVisible();
  await shot("service-mobile");
  await page.keyboard.press("Escape");
  await page.getByRole("radio", { name: "创作画布", exact: true }).click();
  await expect(page.locator(".canvas-wrap")).toBeVisible();
  if (
    await page
      .getByRole("button", { name: "关闭内容面板", exact: true })
      .isVisible()
  )
    await page
      .getByRole("button", { name: "关闭内容面板", exact: true })
      .click();
  await shot("canvas-mobile");
  // 快照只在本地隔离 Mock 中产生，不运行真实 Provider。
  expect(
    (
      await page.request.get("/api/v1/episodes/" + episode.id + "/runs", {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
});
