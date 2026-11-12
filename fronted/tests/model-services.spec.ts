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
  await expect(dialog.getByLabel("模型 ID", { exact: true })).toHaveValue("");
  await dialog.getByLabel("模型 ID", { exact: true }).fill("exact-deepseek-id");
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
  expect((await fetched.json()).services[0]).toMatchObject({
    supplier: "deepseek",
    model: "exact-deepseek-id",
    base_url: "https://api.deepseek.com",
    mode: "custom",
    has_key: true,
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
      secondDialog.getByRole("radio", { name: "服务器默认服务", exact: true }),
    ).toBeChecked();
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
  await dialog
    .getByRole("combobox", { name: "结构化输出格式", exact: true })
    .click();
  await page.getByRole("option", { name: "JSON 对象", exact: true }).click();
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
  await expect(
    dialog.getByRole("combobox", { name: "结构化输出格式", exact: true }),
  ).toContainText("JSON 对象");
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

test("清除密钥先保留草稿可撤销，保存后恢复服务器配置", async ({ page }) => {
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
    dialog.getByText("保存后清除个人密钥，新任务使用服务器默认服务。"),
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
    "新任务使用服务器默认服务",
  );
  await page.reload();
  await page.getByRole("button", { name: "模型服务配置", exact: true }).click();
  await expect(
    dialog.getByRole("radio", { name: "服务器默认服务", exact: true }),
  ).toBeChecked();
  const saved = await page.request.get("/api/v1/me/model-services", {
    headers,
  });
  expect(
    (await saved.json()).services.find(
      (row: { kind: string }) => row.kind === "text",
    ),
  ).toMatchObject({ mode: "server", has_key: false });
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
      "/api/v1/me/model-services?catalog_version=2",
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
