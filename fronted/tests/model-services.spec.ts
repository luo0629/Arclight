import { test, expect, type Page } from "@playwright/test";

async function openSettings(page: Page) {
  const registered = await page.request.post("/api/v1/auth/register", {
    data: {
      email: `${crypto.randomUUID()}@example.com`,
      password: "service-presets-test",
      display_name: "服务配置验收",
    },
  });
  expect(registered.status()).toBe(201);
  const session = await registered.json();
  const headers = { Authorization: `Bearer ${session.access_token}` };
  const project = await page.request.post("/api/v1/projects", {
    headers,
    data: { name: "我的故事" },
  });
  expect(project.status()).toBe(201);
  await page.goto("/");
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeVisible();
  return { dialog, headers };
}

test("检测连接使用未保存的输入，显示结果且编辑后清除（手机）", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { dialog, headers } = await openSettings(page);
  await expect(
    dialog.getByRole("radio", { name: "自定义服务商", exact: true }),
  ).toBeChecked();
  const check = dialog.getByRole("button", { name: "检测连接", exact: true });
  const model = dialog.getByLabel("模型 ID", { exact: true });
  await expect(check).toBeDisabled();
  await dialog
    .getByLabel("Base URL", { exact: true })
    .fill("https://probe.example/v1");
  await model.fill("draft-model");
  const key = dialog.getByLabel("API Key", { exact: true });
  await key.fill("ephemeral-browser-key");
  await expect(check).toBeEnabled();

  let release = () => {};
  const pending = new Promise<void>((resolve) => {
    release = resolve;
  });
  const requests: unknown[] = [];
  await page.route(
    "**/api/v1/me/model-services/text/connection",
    async (route) => {
      expect(route.request().method()).toBe("POST");
      requests.push(route.request().postDataJSON());
      if (requests.length === 1) {
        await pending;
        await route.fulfill({
          json: { status: "model_found", message: "服务可达，已查到该模型。" },
        });
      } else {
        await route.fulfill({
          json: {
            status: "failed",
            message: "服务拒绝访问，请检查 API Key 和账号权限。",
          },
        });
      }
    },
  );
  try {
    await check.click();
    await expect(
      dialog.getByRole("button", { name: "检测中…" }),
    ).toBeDisabled();
  } finally {
    release();
  }
  await expect(dialog.getByRole("status")).toContainText("已查到该模型");
  expect(requests).toEqual([
    {
      supplier: "custom",
      protocol: "openai_chat",
      base_url: "https://probe.example/v1",
      model: "draft-model",
      api_key: "ephemeral-browser-key",
      expected_revision: 0,
    },
  ]);
  await expect(key).toHaveValue("ephemeral-browser-key");
  const stored = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect((await stored.json()).services).toEqual([]);
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(
    true,
  );
  await model.fill("other-model");
  await expect(dialog.getByRole("status")).toHaveCount(0);
  await check.click();
  await expect(dialog.getByRole("alert")).toContainText("服务拒绝访问");
  await dialog.getByRole("radio", { name: "图片模型" }).click();
  await expect(dialog.getByText("服务拒绝访问", { exact: false })).toHaveCount(
    0,
  );
});

test("预设只填密钥和模型 ID，刷新保留且账号独立", async ({
  page,
  browser,
  baseURL,
}) => {
  const { dialog, headers } = await openSettings(page);
  const crashes: string[] = [];
  page.on("pageerror", (error) => crashes.push(error.message));
  await dialog.getByRole("radio", { name: "DeepSeek", exact: true }).click();
  await expect(dialog.getByLabel("Base URL", { exact: true })).toHaveCount(0);
  const model = dialog.getByLabel("模型 ID", { exact: true });
  await expect(model).toHaveValue("");
  await expect(
    dialog.getByRole("combobox", { name: "推荐模型", exact: true }),
  ).toBeVisible();
  await model.fill("exact-deepseek-id");
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("my-private-deepseek-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByText(
      "已保存到当前账号。新建的对应任务将使用此配置，已有任务保留原配置。",
      {
        exact: true,
      },
    ),
  ).toBeVisible();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  const fetched = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(await fetched.text()).not.toContain("my-private-deepseek-key");
  const configuration = await fetched.json();
  expect(configuration).not.toHaveProperty("text_runtime");
  expect(configuration.services[0]).not.toHaveProperty("mode");
  expect(configuration.services[0]).toMatchObject({
    supplier: "deepseek",
    model: "exact-deepseek-id",
    base_url: "https://api.deepseek.com",
    has_key: true,
    capability_check: { status: "unverified", tools: false },
  });
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  expect(
    await page.evaluate(() => JSON.stringify([localStorage, sessionStorage])),
  ).not.toContain("my-private-deepseek-key");
  await page.screenshot({
    path: "../artifacts/web/model-services-presets-desktop.png",
    fullPage: true,
    animations: "disabled",
  });
  const secondContext = await browser.newContext({ baseURL });
  try {
    const secondPage = await secondContext.newPage();
    const { dialog: secondDialog } = await openSettings(secondPage);
    await expect(
      secondDialog.getByRole("radio", { name: "自定义服务商", exact: true }),
    ).toBeChecked();
    await expect(
      secondDialog.getByRole("radio", { name: "服务器默认服务", exact: true }),
    ).toHaveCount(0);
    await secondDialog
      .getByRole("radio", { name: "DeepSeek", exact: true })
      .click();
    await expect(
      secondDialog.getByLabel("API Key", { exact: true }),
    ).toHaveAttribute("placeholder", "粘贴此服务商的 API Key");
    await expect(
      secondDialog.getByText("密钥已加密保存", { exact: true }),
    ).toHaveCount(0);
  } finally {
    await secondContext.close();
  }
  expect(crashes).toEqual([]);
});

test("文字、图片和视频按后端供应商预设填写模型并保存", async ({ page }) => {
  const { dialog, headers } = await openSettings(page);
  const path = "/api/v1/me/model-services";
  const catalog = await (await page.request.get(path, { headers })).json();
  const model = dialog.getByLabel("模型 ID", { exact: true });
  for (const [kind, tab, supplier] of [
    ["text", "文字模型", "kimi"],
    ["text", "文字模型", "minimax"],
    ["text", "文字模型", "openrouter"],
    ["image", "图片模型", "volcengine"],
    ["video", "视频模型", "google"],
  ]) {
    const preset = catalog.suppliers.find(
      (row: { kind: string; id: string }) =>
        row.kind === kind && row.id === supplier,
    );
    const suggestion = (
      kind === "text" ? catalog.text_models : catalog.visual_models
    ).find(
      (row: { kind?: string; supplier: string }) =>
        (row.kind === undefined || row.kind === kind) &&
        row.supplier === supplier,
    );
    expect(preset).toBeDefined();
    expect(suggestion).toBeDefined();
    await dialog.getByRole("radio", { name: tab, exact: true }).click();
    await dialog.getByRole("radio", { name: preset.name, exact: true }).click();
    await expect(model).toHaveValue("");
    await expect(
      dialog.getByText(preset.base_url, { exact: true }),
    ).toBeVisible();
    await dialog
      .getByRole("combobox", { name: "推荐模型", exact: true })
      .click();
    await page
      .getByRole("option", { name: suggestion.name, exact: true })
      .click();
    await expect(model).toHaveValue(suggestion.model);
    const key = dialog.getByLabel("API Key", { exact: true });
    await expect(key).toHaveValue("");
    await key.fill("synthetic-preset-browser-key");
    await dialog
      .getByRole("button", { name: "保存此服务", exact: true })
      .click();
    await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
    const saved = await page.request.get(path, { headers });
    const profiles = (await saved.json()).services;
    const profile = profiles.find((row: { kind: string }) => row.kind === kind);
    expect(profile).toMatchObject({
      supplier,
      model: suggestion.model,
      protocol: preset.protocol,
      base_url: preset.base_url,
      has_key: true,
    });
    if (kind === "text") {
      expect(profile.capability_check).toMatchObject({
        status: "unverified",
        tools: false,
        can_check: true,
      });
    }
  }
});

test("OpenAI 推荐模型保存后直接使用已登记能力", async ({ page }) => {
  const { dialog, headers } = await openSettings(page);
  await dialog.getByRole("radio", { name: "OpenAI", exact: true }).click();
  const model = dialog.getByLabel("模型 ID", { exact: true });
  await expect(model).toHaveValue("");
  await dialog.getByRole("combobox", { name: "推荐模型", exact: true }).click();
  await page.getByRole("option", { name: "GPT-5.4 mini", exact: true }).click();
  await expect(model).toHaveValue("gpt-5.4-mini");
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("synthetic-catalog-test-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();

  const capabilities = dialog.getByRole("region", { name: "智能创作能力" });
  await expect(capabilities).toContainText("模型支持智能创作");
  await expect(capabilities).toContainText("支持工具调用");
  await expect(capabilities).toContainText("支持参考图");
  await expect(
    dialog.getByRole("button", { name: "同意并验证模型" }),
  ).toHaveCount(0);

  const fetched = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(fetched.ok()).toBeTruthy();
  const [profile] = (await fetched.json()).services;
  expect(profile).toMatchObject({
    supplier: "openai",
    model: "gpt-5.4-mini",
    base_url: "https://api.openai.com/v1",
    protocol: "openai_chat",
    capability_check: {
      status: "ready",
      tools: true,
      max_input_images: 8,
      can_check: false,
    },
  });
});

test("切换供应商不复用旧密钥，自定义支持手填及兼容协议", async ({ page }) => {
  const { dialog } = await openSettings(page);
  await dialog.getByRole("radio", { name: "OpenAI", exact: true }).click();
  await dialog.getByLabel("模型 ID", { exact: true }).fill("exact-openai-id");
  await dialog.getByLabel("API Key", { exact: true }).fill("openai-only-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(
    dialog.getByText("密钥已加密保存", { exact: true }),
  ).toBeVisible();
  await dialog
    .getByRole("radio", { name: "智谱 BigModel", exact: true })
    .click();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await dialog.getByLabel("模型 ID", { exact: true }).fill("exact-zhipu-id");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  expect(
    await dialog
      .getByLabel("API Key", { exact: true })
      .evaluate((element: HTMLInputElement) => element.validity.valueMissing),
  ).toBeTruthy();
  await dialog.getByLabel("API Key", { exact: true }).fill("qwen-only-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await dialog
    .getByRole("radio", { name: "自定义服务商", exact: true })
    .click();
  await expect(dialog.getByLabel("Base URL", { exact: true })).toBeEditable();
  await dialog
    .getByLabel("Base URL", { exact: true })
    .fill("https://my-provider.example/v1");
  await dialog.getByLabel("模型 ID", { exact: true }).fill("my-story-model");
  await dialog.getByLabel("API Key", { exact: true }).fill("custom-only-key");
  await expect(
    dialog.getByRole("combobox", { name: "结构化输出格式", exact: true }),
  ).toHaveCount(0);
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "自定义服务商", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("Base URL", { exact: true })).toHaveValue(
    "https://my-provider.example/v1",
  );
  await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
    "my-story-model",
  );
});

test("保存文字模型后由用户选择验证能力，并继续使用返回的配置修订", async ({
  page,
}) => {
  const { dialog, headers } = await openSettings(page);
  const verify = dialog.getByRole("button", { name: "同意并验证模型" });
  await expect(verify).toHaveCount(0);

  await page.route("**/api/v1/me/model-services/text", async (route) => {
    expect(route.request().method()).toBe("PUT");
    expect(route.request().postDataJSON()).not.toHaveProperty(
      "response_format",
    );
    await route.continue();
  });
  await dialog
    .getByLabel("Base URL", { exact: true })
    .fill("https://probe.example/v1");
  const model = dialog.getByLabel("模型 ID", { exact: true });
  await model.fill("custom-agent-model");
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("test-capability-key");
  await expect(verify).toHaveCount(0);
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();

  const capabilities = dialog.getByRole("region", { name: "智能创作能力" });
  await expect(capabilities).toContainText("智能创作能力尚未验证");
  await expect(verify).toHaveAccessibleDescription(
    "验证只发送合成测试内容，供应商可能收费。",
  );

  const response = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(response.ok()).toBeTruthy();
  const [profile] = (await response.json()).services;
  const checks: unknown[] = [];
  await page.route(
    "**/api/v1/me/model-services/text/capabilities",
    async (route) => {
      expect(route.request().method()).toBe("POST");
      checks.push(route.request().postDataJSON());
      await route.fulfill({
        json: {
          ...profile,
          revision: profile.revision + 1,
          capability_check: {
            status: "partial",
            tools: false,
            max_input_images: 0,
            can_check: false,
          },
        },
      });
    },
  );

  await verify.click();
  await expect(capabilities).toContainText("仅通过部分能力检查");
  await expect(verify).toHaveCount(0);
  expect(checks).toEqual([
    { expected_revision: profile.revision, confirm_charges: true },
  ]);

  const servicePath = "**/api/v1/me/model-services/text";
  await page.unroute(servicePath);
  await page.route(servicePath, async (route) => {
    const request = route.request();
    expect(request.method()).toBe("PUT");
    expect(request.postDataJSON()).toMatchObject({
      model: "other-model",
      expected_revision: profile.revision + 1,
    });
    await route.fulfill({
      json: {
        ...profile,
        model: "other-model",
        revision: profile.revision + 2,
        capability_check: profile.capability_check,
      },
    });
  });

  await model.fill("other-model");
  await expect(capabilities).toHaveCount(0);
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(capabilities).toContainText("智能创作能力尚未验证");
});

test("媒体配置单独保存，切分类保留草稿，手机无溢出且关闭保护可见", async ({
  page,
}) => {
  await page.setViewportSize({ width: 390, height: 844 });
  const { dialog } = await openSettings(page);
  await dialog
    .getByRole("radio", { name: "智谱 BigModel", exact: true })
    .click();
  await dialog.getByLabel("API Key", { exact: true }).fill("text-draft");
  await dialog.getByRole("radio", { name: "视频模型", exact: true }).click();
  await dialog
    .getByRole("radio", { name: "自定义服务商", exact: true })
    .click();
  await expect(dialog.getByLabel("Base URL", { exact: true })).toBeEditable();
  await dialog
    .getByLabel("Base URL", { exact: true })
    .fill("https://video-supplier.example/v1");
  await dialog.getByLabel("模型 ID", { exact: true }).fill("my-video-model");
  await dialog.getByLabel("API Key", { exact: true }).fill("video-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText(
    "新建的对应任务将使用此配置",
  );
  await dialog.getByRole("radio", { name: "文字模型", exact: true }).click();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue(
    "text-draft",
  );
  await page.keyboard.press("Escape");
  await expect(
    dialog.getByRole("button", { name: "继续编辑配置" }),
  ).toBeVisible();
  await dialog.getByRole("button", { name: "继续编辑配置" }).click();
  expect(
    await page.evaluate(
      () => document.documentElement.scrollWidth <= innerWidth,
    ),
  ).toBeTruthy();
  await dialog.getByRole("radio", { name: "视频模型", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "视频模型", exact: true }),
  ).toBeChecked();
  await expect(
    dialog.getByRole("radio", { name: "文字模型", exact: true }),
  ).not.toBeChecked();
  await page.screenshot({
    path: "../artifacts/web/model-services-presets-mobile.png",
    fullPage: true,
    animations: "disabled",
  });
});

test("清除密钥先保留草稿可撤销，保存后保留服务配置", async ({ page }) => {
  const { dialog, headers } = await openSettings(page);
  await dialog.getByRole("radio", { name: "DeepSeek", exact: true }).click();
  await dialog.getByLabel("模型 ID", { exact: true }).fill("configured-model");
  await dialog
    .getByLabel("API Key", { exact: true })
    .fill("removable-test-key");
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
  await dialog.getByRole("button", { name: "清除已存密钥" }).click();
  await expect(
    dialog.getByText(
      "保存后清除 API Key，并保留服务配置。新任务需要个人服务。",
    ),
  ).toBeVisible();
  expect(
    (
      await (
        await page.request.get("/api/v1/me/model-services", { headers })
      ).json()
    ).services.find((row: { kind: string }) => row.kind === "text").has_key,
  ).toBe(true);
  await dialog.getByRole("button", { name: "保留原密钥" }).click();
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await dialog.getByRole("button", { name: "清除已存密钥" }).click();
  await dialog.getByRole("button", { name: "保存此服务", exact: true }).click();
  await expect(dialog.getByRole("status")).toContainText(
    "重新配置此服务后才能用于新任务",
  );
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "DeepSeek", exact: true }),
  ).toBeChecked();
  await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
    "configured-model",
  );
  await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
  await expect(
    dialog.getByRole("button", { name: "检测连接", exact: true }),
  ).toBeDisabled();
  const saved = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(
    (await saved.json()).services.find(
      (row: { kind: string }) => row.kind === "text",
    ),
  ).toMatchObject({
    supplier: "deepseek",
    model: "configured-model",
    base_url: "https://api.deepseek.com",
    has_key: false,
  });
});

type SpeechModelPreset = {
  supplier: string;
  model: string;
  name: string;
  description: string;
};

for (const viewport of [
  { name: "桌面", width: 1280, height: 900 },
  { name: "手机", width: 390, height: 844 },
]) {
  test(`TTS 模型预设按供应商选择、保存和手填兼容（${viewport.name}）`, async ({
    page,
  }, testInfo) => {
    await page.setViewportSize(viewport);
    const { dialog, headers } = await openSettings(page);
    const catalogResponse = await page.request.get(
      "/api/v1/me/model-services",
      { headers },
    );
    expect(catalogResponse.ok()).toBeTruthy();
    const catalog: {
      suppliers: { id: string; kind: string; name: string }[];
      speech_models: SpeechModelPreset[];
    } = await catalogResponse.json();
    await dialog.getByRole("radio", { name: "语音服务", exact: true }).click();
    let selected: SpeechModelPreset | undefined;
    for (const supplier of ["qwen", "siliconflow"]) {
      const provider = catalog.suppliers.find(
        (row) => row.kind === "speech" && row.id === supplier,
      );
      expect(provider).toBeDefined();
      const models = catalog.speech_models.filter(
        (row) => row.supplier === supplier,
      );
      expect(models.length).toBeGreaterThan(0);
      await dialog
        .getByRole("radio", { name: provider!.name, exact: true })
        .click();
      await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
        "",
      );
      const selector = dialog.getByRole("combobox", {
        name: "TTS 模型预设",
        exact: true,
      });
      await selector.click();
      await expect(page.getByRole("option")).toHaveCount(models.length + 1);
      await expect(
        page.getByRole("option", { name: "用户自定义 TTS 模型", exact: true }),
      ).toBeVisible();
      selected = models[models.length - 1];
      await page
        .getByRole("option", { name: selected.name, exact: true })
        .click();
      await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
        selected.model,
      );
      await expect(
        dialog.getByText(selected.description, { exact: true }),
      ).toBeVisible();
    }
    expect(selected).toBeDefined();
    await dialog
      .getByLabel("API Key", { exact: true })
      .fill("tts-preset-test-key");
    await dialog
      .getByRole("button", { name: "保存此服务", exact: true })
      .click();
    await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
    await page.reload();
    await page
      .getByRole("button", { name: "模型服务配置", exact: true })
      .click();
    await dialog.getByRole("radio", { name: "语音服务", exact: true }).click();
    await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
      selected!.model,
    );
    await expect(
      dialog.getByRole("combobox", { name: "TTS 模型预设", exact: true }),
    ).toContainText(selected!.name);
    await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
    await page.screenshot({
      path: testInfo.outputPath(`tts-model-presets-${viewport.width}.png`),
      fullPage: true,
      animations: "disabled",
    });
    await dialog.locator("form").evaluate((element) =>
      element.scrollIntoView({
        block: "start",
        inline: "nearest",
        behavior: "instant",
      }),
    );
    await expect(dialog.getByLabel("API Key", { exact: true })).toBeInViewport({
      ratio: 1,
    });
    await expect(dialog.getByLabel("模型 ID", { exact: true })).toBeInViewport({
      ratio: 1,
    });
    await expect(
      dialog.getByText(selected!.description, { exact: true }),
    ).toBeInViewport({ ratio: 1 });
    await page.screenshot({
      path: testInfo.outputPath(`tts-model-presets-form-${viewport.width}.png`),
      fullPage: true,
      animations: "disabled",
    });
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
    await dialog
      .getByLabel("模型 ID", { exact: true })
      .fill("account-specific-tts-model");
    await expect(
      dialog.getByRole("combobox", { name: "TTS 模型预设", exact: true }),
    ).toContainText("选择预设模型");
    await dialog
      .getByRole("button", { name: "保存此服务", exact: true })
      .click();
    await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
    const saved = await page.request.get("/api/v1/me/model-services", {
      headers,
    });
    expect(
      (await saved.json()).services.find(
        (row: { kind: string }) => row.kind === "speech",
      ),
    ).toMatchObject({
      supplier: "siliconflow",
      model: "account-specific-tts-model",
      has_key: true,
      revision: 2,
    });
    const openAI = catalog.suppliers.find(
      (row) => row.kind === "speech" && row.id === "openai",
    );
    expect(openAI).toBeDefined();
    await dialog
      .getByRole("radio", { name: openAI!.name, exact: true })
      .click();
    const selector = dialog.getByRole("combobox", {
      name: "TTS 模型预设",
      exact: true,
    });
    await selector.click();
    await expect(page.getByRole("option")).toHaveCount(1);
    await page.keyboard.press("Escape");
    await dialog
      .getByLabel("模型 ID", { exact: true })
      .fill("my-openai-tts-model");
    await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
      "my-openai-tts-model",
    );
    await selector.click();
    await page
      .getByRole("option", { name: "用户自定义 TTS 模型", exact: true })
      .click();
    await expect(
      dialog.getByRole("radio", { name: "自定义服务商", exact: true }),
    ).toBeChecked();
    await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
    await dialog
      .getByLabel("Base URL", { exact: true })
      .fill("https://speech-provider.example/v1");
    await dialog
      .getByLabel("模型 ID", { exact: true })
      .fill("my-custom-tts-model");
    await dialog
      .getByRole("combobox", { name: "API 协议", exact: true })
      .click();
    await expect(
      page.getByRole("option", { name: "阿里云 Qwen Audio TTS", exact: true }),
    ).toBeVisible();
    await page
      .getByRole("option", { name: "OpenAI 语音", exact: true })
      .click();
    await dialog
      .getByLabel("API Key", { exact: true })
      .fill("custom-tts-test-key");
    await dialog
      .getByRole("button", { name: "保存此服务", exact: true })
      .click();
    await expect(dialog.getByRole("status")).toContainText("已保存到当前账号");
    await page.reload();
    await page
      .getByRole("button", { name: "模型服务配置", exact: true })
      .click();
    await dialog.getByRole("radio", { name: "语音服务", exact: true }).click();
    await expect(selector).toContainText("用户自定义 TTS 模型");
    await expect(dialog.getByLabel("Base URL", { exact: true })).toHaveValue(
      "https://speech-provider.example/v1",
    );
    await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue(
      "my-custom-tts-model",
    );
    await expect(dialog.getByLabel("API Key", { exact: true })).toHaveValue("");
    await expect(
      dialog.getByRole("combobox", { name: "API 协议", exact: true }),
    ).toContainText("OpenAI 语音");
    const custom = await page.request.get("/api/v1/me/model-services", {
      headers,
    });
    expect(await custom.text()).not.toContain("custom-tts-test-key");
    expect(
      (await custom.json()).services.find(
        (row: { kind: string }) => row.kind === "speech",
      ),
    ).toMatchObject({
      supplier: "custom",
      base_url: "https://speech-provider.example/v1",
      protocol: "openai_tts",
      model: "my-custom-tts-model",
      has_key: true,
      revision: 3,
    });
    await page.screenshot({
      path: testInfo.outputPath(`tts-custom-model-${viewport.width}.png`),
      fullPage: true,
      animations: "disabled",
    });
    await dialog.locator("form").evaluate((element) =>
      element.scrollIntoView({
        block: "start",
        inline: "nearest",
        behavior: "instant",
      }),
    );
    await expect(dialog.getByLabel("API Key", { exact: true })).toBeInViewport({
      ratio: 1,
    });
    await expect(dialog.getByLabel("Base URL", { exact: true })).toBeInViewport(
      {
        ratio: 1,
      },
    );
    if (viewport.width >= 640) {
      await expect(
        dialog.getByLabel("模型 ID", { exact: true }),
      ).toBeInViewport({
        ratio: 1,
      });
      await expect(
        dialog.getByRole("combobox", { name: "API 协议", exact: true }),
      ).toBeInViewport({ ratio: 1 });
    }
    await page.screenshot({
      path: testInfo.outputPath(`tts-custom-model-form-${viewport.width}.png`),
      fullPage: true,
      animations: "disabled",
    });
    if (viewport.width < 640) {
      await dialog.getByLabel("Base URL", { exact: true }).evaluate((element) =>
        element.closest("label")!.scrollIntoView({
          block: "start",
          inline: "nearest",
          behavior: "instant",
        }),
      );
      await expect(
        dialog.getByLabel("Base URL", { exact: true }),
      ).toBeInViewport({
        ratio: 1,
      });
      await expect(
        dialog.getByLabel("模型 ID", { exact: true }),
      ).toBeInViewport({
        ratio: 1,
      });
      await expect(
        dialog.getByRole("combobox", { name: "API 协议", exact: true }),
      ).toBeInViewport({ ratio: 1 });
      await page.screenshot({
        path: testInfo.outputPath(
          `tts-custom-model-fields-${viewport.width}.png`,
        ),
        fullPage: true,
        animations: "disabled",
      });
    }
    expect(
      await page.evaluate(
        () => document.documentElement.scrollWidth <= innerWidth,
      ),
    ).toBeTruthy();
  });
}
