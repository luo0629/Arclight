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
          estimated_duration_ms: 30000,

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
              duration_ms: 30000,
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
            estimated_duration_ms: 30000,

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
  return {
    headers,
    project,
    episode,
    script,
    session,
    board: await board.json(),
  };
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
  await expect(page.getByLabel("剧集列表", { exact: true })).not.toBeVisible();
  await page.getByRole("button", { name: "展开目录", exact: true }).click();
  await expect(page.getByLabel("剧集列表", { exact: true })).toBeVisible();
  const overlaid = await page.locator(".canvas-wrap").boundingBox();
  expect(overlaid!.width).toBe(before!.width);
  await page.getByRole("button", { name: "关闭项目目录", exact: true }).click();
  const after = await page.locator(".canvas-wrap").boundingBox();
  expect(after!.width).toBe(before!.width);
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
  await page.getByRole("button", { name: "故事创作", exact: true }).click();
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

test("新版：官方供应商只填密钥、自定义地址与目标变更保护", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  const supplier = dialog.getByRole("radio", {
    name: "DeepSeek",
    exact: true,
  });
  const endpoint = dialog.getByLabel("服务地址", { exact: true });
  const key = dialog.getByLabel("API Key", { exact: true });
  await supplier.click();
  await expect(endpoint).toHaveValue("https://api.deepseek.com");
  await expect(endpoint).toHaveAttribute("readonly", "");
  await expect(dialog.getByLabel("模型名称", { exact: true })).not.toHaveValue(
    "",
  );
  await expect(
    dialog.getByText("用于新文字任务", { exact: true }),
  ).toBeVisible();
  await key.fill("official-browser-test-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByRole("status").filter({ hasText: "已保存到当前账号" }),
  ).toContainText("已保存到当前账号");
  await expect(key).toHaveValue("");
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(supplier).toBeChecked();
  await expect(endpoint).toHaveValue("https://api.deepseek.com");
  await key.fill("draft-that-must-be-cleared");
  await dialog
    .getByRole("radio", { name: "自定义服务商", exact: true })
    .click();
  await expect(key).toHaveValue("");
  await endpoint.fill("https://independent-supplier.example/v1");
  await dialog
    .getByLabel("供应商名称（选填）", { exact: true })
    .fill("我的供应商");
  await dialog.getByLabel("模型名称", { exact: true }).fill("my-text-model");
  await dialog
    .getByRole("combobox", { name: "结构化输出格式", exact: true })
    .click();
  await page.getByRole("option", { name: "JSON 对象", exact: true }).click();
  await expect(
    dialog.getByText("服务商或地址已更改，请填写对应密钥", { exact: true }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("alert")).toContainText("请重新填写 API Key");
  await key.fill("custom-browser-test-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByRole("status").filter({ hasText: "已保存到当前账号" }),
  ).toContainText("已保存到当前账号");
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "自定义服务商", exact: true }),
  ).toBeChecked();
  await expect(endpoint).toHaveValue("https://independent-supplier.example/v1");
  await expect(
    dialog.getByLabel("供应商名称（选填）", { exact: true }),
  ).toHaveValue("我的供应商");
  await expect(
    dialog.getByRole("combobox", { name: "结构化输出格式", exact: true }),
  ).toContainText("JSON 对象");
  await expect(key).toHaveValue("");
  expect(
    await page.evaluate(() => JSON.stringify([localStorage, sessionStorage])),
  ).not.toContain("browser-test-key");
  await noOverflow(page);
});

test("新版：配置保存失败保留输入，读取失败可重试，手机390px无横向溢出", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await setup(page);
  await expect(
    page.getByRole("radio", { name: "创作画布", exact: true }),
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
  test.setTimeout(120000);
  const shot = async (name: string, resetScroll = true) => {
    if (resetScroll)
      await page
        .locator(".project-main.is-management, .episode-studio")
        .evaluateAll((elements) =>
          elements.forEach((element) => (element.scrollTop = 0)),
        );
    await noOverflow(page);
    await page.evaluate(() => document.fonts.ready);
    await page.screenshot({
      path: "../artifacts/web/final-" + name + ".png",
      fullPage: !(await page.getByRole("dialog").isVisible()),
      animations: "disabled",
    });
  };
  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto("/");
  await expect(
    page.getByRole("heading", { name: /让一个小灵感，\s*长成好故事。/ }),
  ).toBeVisible();
  await shot("login-desktop");
  const { headers, episode, project } = await setup(page, true);
  await expect(page.locator(".paper-node.storyboard")).toBeVisible();
  await shot("canvas-desktop");
  await page.getByRole("button", { name: "展开目录", exact: true }).click();
  await shot("directory-desktop");
  await page.getByRole("button", { name: "关闭项目目录", exact: true }).click();
  await page.getByRole("button", { name: "全屏画布", exact: true }).click();
  await shot("canvas-focus-desktop");
  await page.keyboard.press("Escape");
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
  await page.getByRole("radio", { name: "素材库", exact: true }).click();
  await shot("library-desktop");
  await page.getByRole("radio", { name: "镜头", exact: true }).click();
  await shot("library-shots-desktop");
  await page.locator(".library-shot").first().click();
  await expect(
    page.getByRole("dialog").getByRole("button", { name: "打开所属分镜" }),
  ).toBeVisible();
  await expect(
    page.getByRole("dialog").getByText("正在载入完整分镜…"),
  ).toHaveCount(0);
  await shot("shot-viewer-desktop");
  await page.keyboard.press("Escape");
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
  // 设定受活动工作流保护，截图后正常取消任务。
  const runs = await (
    await page.request.get("/api/v1/episodes/" + episode.id + "/runs", {
      headers,
    })
  ).json();
  for (const run of runs) {
    if (["QUEUED", "WAITING_REVIEW"].includes(run.status)) {
      const cancelled = await page.request.post(
        "/api/v1/runs/" + run.id + "/cancel",
        { headers },
      );
      expect(cancelled.status()).toBe(200);
    }
  }

  await page.getByRole("radio", { name: "故事来源", exact: true }).click();
  await page.getByRole("button", { name: "导入故事文件", exact: true }).click();
  await page.getByLabel("故事文件", { exact: true }).setInputFiles({
    name: "森林邮局·原创故事.md",
    mimeType: "text/markdown",
    buffer: Buffer.from(
      "# 森林邮局\n\n小鹿在晨光中推开树屋的门，桌上是一封没有收件人的信。\n\n她和萤火虫沿着溪流寻找春天，把这封信送给第一朵迎春花。",
    ),
  });
  await page.getByLabel("来源说明", { exact: true }).fill("青禾的原创故事初稿");
  await page
    .getByLabel("授权说明", { exact: true })
    .fill("本人原创，拥有完整使用权。");
  await page.getByRole("combobox", { name: "使用权确认", exact: true }).click();
  await page
    .getByRole("option", {
      name: "我确认拥有导入和使用此故事的权利",
      exact: true,
    })
    .click();
  await shot("source-import-desktop");
  await page.getByRole("button", { name: "保存故事来源", exact: true }).click();
  await expect(page.getByLabel("原始故事文本")).toContainText("森林邮局");
  await expect(page.locator('[data-slot="sheet-overlay"]')).toHaveCount(0);
  await shot("story-sources-desktop");
  await page.getByRole("radio", { name: "故事提炼", exact: true }).click();
  await page
    .getByRole("combobox", { name: "故事输入方式", exact: true })
    .click();
  await page.getByRole("option", { name: "已导入来源", exact: true }).click();
  await page
    .getByRole("combobox", { name: "选择故事来源", exact: true })
    .click();
  await page.getByRole("option", { name: /森林邮局.*原创故事/ }).click();
  await shot("story-analysis-input-desktop");
  await page.getByRole("button", { name: "开始提炼", exact: true }).click();
  await expect(page.getByRole("button", { name: /^提炼 v1/ })).toBeVisible({
    timeout: 20000,
  });
  await page.getByRole("button", { name: /^提炼 v1/ }).click();
  await expect(
    page
      .getByRole("region", { name: "提炼结果详情" })
      .getByRole("heading", { name: "摘要", exact: true }),
  ).toBeVisible();
  await page.locator(".studio-analysis-input > summary").click();
  await shot("story-analysis-result-desktop");

  await page
    .getByRole("button", { name: "复制为新集草稿", exact: true })
    .click();
  await expect(
    page.getByRole("dialog", { name: "复制剧集为草稿", exact: true }),
  ).toBeVisible();
  await shot("episode-copy-desktop");
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "故事设定", exact: true }).click();
  await page.getByRole("button", { name: "新建设定", exact: true }).click();
  await page.getByLabel("设定名称", { exact: true }).fill("森林的来信");
  await page
    .getByLabel("设定说明", { exact: true })
    .fill(
      "一座藏在树屋里的邮局。故事发生在温柔的春日森林，小鹿与萤火虫为每一封信寻找归处。",
    );
  await page
    .getByLabel("规则（每行一条，最多 10 条）", { exact: true })
    .fill("保持温暖、自然的色调\n每封信都来自真实的心意");
  await page.getByRole("button", { name: "保存故事设定", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "森林的来信 · 修订 0", exact: true }),
  ).toBeVisible();
  await shot("story-bible-desktop");

  await page.getByRole("button", { name: "创作伙伴", exact: true }).click();
  await shot("members-desktop");
  await page
    .getByRole("button", { name: "项目设置与归档", exact: true })
    .click();
  await shot("project-settings-desktop");
  await page
    .getByRole("heading", { name: "项目业务设定", exact: true })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await shot("story-settings-desktop", false);
  await page
    .getByRole("heading", { name: "视觉风格与画幅", exact: true })
    .evaluate((element) => element.scrollIntoView({ block: "start" }));
  await shot("production-settings-desktop", false);
  await page.getByRole("button", { name: "故事创作", exact: true }).click();

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

  // 使用独立项目验收删除与回收站，不影响上面的故事和任务。
  const disposableResult = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "待整理的故事草稿" },
  });
  expect(disposableResult.status()).toBe(201);
  const disposable = await disposableResult.json();
  expect(
    (
      await page.request.patch("/api/v1/projects/" + disposable.id, {
        headers,
        data: { archived: true },
      })
    ).ok(),
  ).toBeTruthy();
  await page.goto(`/projects/${disposable.id}?section=settings`);
  await expect(page.getByLabel("项目名称", { exact: true })).toHaveValue(
    disposable.name,
  );
  await page.getByRole("button", { name: "删除项目", exact: true }).click();
  const deletion = page.getByRole("dialog", {
    name: "确认删除项目",
    exact: true,
  });
  await deletion.getByLabel("输入完整项目名称").fill(disposable.name);
  await shot("project-deletion-desktop");
  await deletion
    .getByRole("button", { name: "确认移入回收站", exact: true })
    .click();
  await expect(deletion).toHaveCount(0);
  await page.getByRole("button", { name: "展开目录", exact: true }).click();
  await page.getByRole("button", { name: "项目回收站", exact: true }).click();
  await expect(
    page
      .getByRole("dialog", { name: "项目回收站", exact: true })
      .getByRole("heading", { name: disposable.name, exact: true }),
  ).toBeVisible();
  await shot("project-trash-desktop");
  await page.keyboard.press("Escape");
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto(`/projects/${project.id}/episodes/${episode.id}?view=list`);
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

test("工作室：全屏画布、覆盖面板、素材分类与独立镜头", async ({
  page,
}, testInfo) => {
  await page.setViewportSize({ width: 1440, height: 1000 });
  const { headers, episode, script } = await setup(page);
  const base = await page.locator(".canvas-wrap").boundingBox();
  await page.getByRole("button", { name: "全屏画布", exact: true }).click();
  const focused = await page.locator(".episode-studio").boundingBox();
  expect(focused).toEqual({ x: 0, y: 0, width: 1440, height: 1000 });
  await expect(page.locator(".canvas-wrap")).toHaveCSS("width", "1440px");
  await page.getByRole("button", { name: "展开内容面板", exact: true }).click();
  await expect(page.locator(".canvas-wrap")).toHaveCSS("width", "1440px");
  await page.getByRole("button", { name: "关闭内容面板", exact: true }).click();
  await page.keyboard.press("Escape");
  expect((await page.locator(".canvas-wrap").boundingBox())!.height).toBe(
    base!.height,
  );
  await page.getByRole("radio", { name: "素材库", exact: true }).click();
  await expect(page.locator(".canvas-wrap")).toHaveCount(0);
  await page.getByRole("radio", { name: "已批准", exact: true }).click();
  await expect(page.locator(".library-row")).toHaveCount(1);
  await expect(page.locator(".library-row")).toContainText(
    script.content_json.title,
  );
  await page.getByLabel("搜索素材", { exact: true }).fill("找不到的标题");
  await expect(
    page.getByText("没有找到匹配的素材", { exact: true }),
  ).toBeVisible();
  await page.getByLabel("搜索素材", { exact: true }).fill("");
  await page.getByRole("radio", { name: "镜头", exact: true }).click();
  await page.locator(".library-shot").first().click();
  const viewer = page.getByRole("dialog", { name: "镜头 01", exact: true });
  await expect(viewer).toBeVisible();
  await expect(viewer.getByLabel("镜头画面描述")).toContainText("小鹿展开信纸");
  await expect(
    viewer.getByRole("button", { name: "下一个镜头" }),
  ).toBeDisabled();
  await page.screenshot({
    path: testInfo.outputPath("shot-viewer-desktop.png"),
  });
  await page.setViewportSize({ width: 390, height: 844 });
  await noOverflow(page);
  await page.screenshot({
    path: testInfo.outputPath("shot-viewer-mobile.png"),
  });
  await viewer
    .getByRole("button", { name: "打开所属分镜", exact: true })
    .click();
  await expect(page.getByRole("dialog")).toHaveCount(0);
  await expect(
    page.getByRole("heading", { name: "分镜 v1", exact: true }),
  ).toBeVisible();
  expect(
    (
      await page.request.get("/api/v1/episodes/" + episode.id + "/canvas", {
        headers,
      })
    ).ok(),
  ).toBeTruthy();
});

test("素材库：未提交草稿按来源恢复，关闭页面后仍保留", async ({
  page,
  context,
}) => {
  const { episode } = await setup(page);
  await page.getByRole("button", { name: "开始创作", exact: true }).click();
  await page.getByLabel("故事灵感", { exact: true }).fill("存在本机的森林来信");
  await page.getByRole("button", { name: "自己写剧本", exact: true }).click();
  await page.getByLabel("剧本标题", { exact: true }).fill("未完成的森林来信");
  await page.getByRole("radio", { name: "素材库", exact: true }).click();
  await page.getByRole("radio", { name: "草稿", exact: true }).click();
  await expect(page.locator(".library-row")).toHaveCount(2);
  await page
    .locator(".library-row")
    .filter({ hasText: "剧本起稿" })
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(page.getByLabel("剧本标题", { exact: true })).toHaveValue(
    "未完成的森林来信",
  );
  const next = await context.newPage();
  await next.goto("/");
  await next.getByRole("radio", { name: "素材库", exact: true }).click();
  await next.getByRole("radio", { name: "草稿", exact: true }).click();
  await expect(next.locator(".library-row")).toHaveCount(2);
  await next
    .locator(".library-row")
    .filter({ hasText: "故事灵感" })
    .getByRole("button", { name: "继续编辑" })
    .click();
  await expect(next.getByLabel("故事灵感", { exact: true })).toHaveValue(
    "存在本机的森林来信",
  );
  expect(episode.id).toBeTruthy();
});
